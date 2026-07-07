"""SaveMemeTool —— 把 timeline 里出现过的一张图片收录进全局表情包收藏夹。

流程（表情包工具黑盒设计.md §save_meme）：
  image_hash（sha256，LLM 从 timeline 的 <image hash="..."/> 原样抄来）→ 定位
  EventIngest 已落盘的 runtime_data/media/img/<h[:2]>/<h>（内容寻址复用，
  **不复制文件**）→ 该 hash 已收录则直接返回 already_saved（不重复 caption，
  不覆盖）→ 读盘 + magic bytes 嗅探 mime → 经 context 注入的 caption_image
  单独调一次多模态 LLM 生成中文描述（planner 可用 context_note 补充聊天
  语境——纯看图写不出"这是谁的名场面/怎么用"）→ 落 agent_memes。
  下一 tick 起收藏出现在 <saved-memes>，send_meme 凭同一 hash 发送。

共享语义：收藏夹全 bot 一份、所有聊天 scope 共用（隔离契约 §9.2 第 6 条
例外，见 meme_store 模块 docstring）——任何群/私聊收录的表情包，其余会话
都能看到并发送。LLM 的 context 里只出现本 scope 见过的图片 hash，收录入口
天然限于本 scope 的时间线；hash 对应文件不在盘上（编造/抄错）→
image_not_found。

失败语义（全程无 raise，新增 error_kind 见设计文档 §错误语义）：
  image_not_found   hash 格式合法但盘上无此文件——抄错了或图没下载成功。
  caption_failed    描述生成失败（LLM 调用异常/空输出），retryable —— 描述是
                    收录的核心产出，不落无描述的残记录，让 LLM 下拍重试。
  internal_tool_error  caption_image / session_factory 未接线（部署/装配问题，
                    与 wait 缺 wake_scope 的降级同式）。

依赖注入：session_factory / caption_image / tool_call_event_id 全部来自
ToolWorker 统一注入的 run() context，无构造依赖。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.meme_store import get_meme, insert_meme
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._meme_common import (
    coerce_image_hash,
    media_path_for_hash,
    sniff_mime,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "save_meme.md")

# context_note 上限：它是 caption 的辅助输入，不是正文；过长说明模型在把
# 描述塞进 note（描述该由 caption 生成）。
MAX_CONTEXT_NOTE_CHARS = 300


class SaveMemeTool(BaseTool):
    """实现 Tool 协议。GUEST + 无 bot 角色要求：收藏只写自己的表，不对任何
    用户/群做操作；"仅在用户明确要求时保存"是 usage 文档的软规则，不设硬门禁。
    """

    name = "save_meme"
    description = (
        "Save an image that appeared in this chat as a reusable meme "
        "(表情包) into your meme collection. Pass image_hash = the sha256 "
        'hash= value copied VERBATIM from an <image hash="..."/> tag in the '
        "timeline. The system looks at the image itself and writes the "
        "searchable description — you do not write it; optionally pass "
        "context_note with chat context the pixels cannot show (whose famous "
        "scene this is, what in-joke it carries). Save only when a user "
        "explicitly asks to save/collect the image. Once saved it appears "
        "in <saved-memes> and can be sent anytime via send_meme."
    )
    usage_prompt = _USAGE_PROMPT
    # 收藏夹挂在聊天 scope 上；system scope 没有聊天面也没有图片来源。
    allowed_scopes = ("group", "private")
    arguments_schema = {
        "type": "object",
        "properties": {
            "image_hash": {
                "type": "string",
                "description": (
                    "64-char sha256 hex of the image, copied verbatim from "
                    'the <image hash="..."/> tag in the timeline.'
                ),
            },
            "context_note": {
                "type": "string",
                "description": (
                    "Optional chat context the image itself cannot show "
                    "(e.g. 这是谁的名场面 / 本群拿它玩什么梗). Folded into "
                    "the generated description; not shown to users."
                ),
            },
        },
        "required": ["image_hash"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail

        file_hash, fail = coerce_image_hash(arguments.get("image_hash"))
        if fail or file_hash is None:
            return fail or ToolOutcome.failure(
                "invalid_arguments", "image_hash is required"
            )

        note_raw = arguments.get("context_note")
        if note_raw is not None and not isinstance(note_raw, str):
            return ToolOutcome.failure(
                "invalid_arguments",
                "context_note must be a string",
                field="context_note",
                reason_code="context_note_not_str",
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        context_note = (note_raw or "").strip()[:MAX_CONTEXT_NOTE_CHARS] or None

        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        if not scope_key or not isinstance(scope_key, str) or session_factory is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "save_meme unavailable: missing scope_key/session_factory context",
            )

        # 磁盘存在性 = "bot 真的见过这张图"。文件不在 → hash 抄错 / 图当初
        # 没下载成功（<image> 无 hash= 的那类），给 LLM 可自纠的精确失败。
        path = media_path_for_hash(file_hash)
        try:
            data = path.read_bytes()
        except OSError:
            return ToolOutcome.failure(
                "image_not_found",
                f"no downloaded image with hash {file_hash}; copy the hash= "
                'value exactly from an <image hash="..."/> tag in the '
                "timeline (images without hash= were never downloaded and "
                "cannot be saved)",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        # 去重前查（全局收藏夹）：已收录直接回执现有描述，不重复 caption、
        # 不覆盖——别的群先收录的同图也命中这里。
        existing = await get_meme(session_factory, file_hash)
        if existing is not None:
            return ToolOutcome.success(
                {
                    "file_hash": file_hash,
                    "already_saved": True,
                    "description": existing.description,
                }
            )

        captioner = context.get("caption_image")
        if captioner is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "save_meme unavailable: caption_image not injected",
            )
        mime = sniff_mime(data)
        try:
            description = await captioner(data, mime, context_note)
        except Exception as exc:  # noqa: BLE001 —— caption 失败折结构化 outcome
            logger.warning(
                "[save_meme] caption failed for {}: {}", file_hash, exc
            )
            return ToolOutcome.failure(
                "caption_failed",
                f"description generation failed: {exc}",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )
        description = str(description or "").strip()
        if not description:
            return ToolOutcome.failure(
                "caption_failed",
                "description generation returned empty text",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )

        inserted = await insert_meme(
            session_factory,
            file_hash=file_hash,
            description=description,
            context_note=context_note,
            mime=mime,
            source_event_id=context.get("tool_call_event_id"),
            created_at=china_now(),
        )
        if not inserted:
            # 并发窗口：前查之后、落表之前别的保存先到。回执表内那份描述。
            racer = await get_meme(session_factory, file_hash)
            return ToolOutcome.success(
                {
                    "file_hash": file_hash,
                    "already_saved": True,
                    "description": racer.description if racer else description,
                }
            )
        logger.info(
            "[save_meme] saved {} into {} ({} chars)",
            file_hash,
            scope_key,
            len(description),
        )
        return ToolOutcome.success(
            {
                "file_hash": file_hash,
                "saved": True,
                "description": description,
            }
        )
