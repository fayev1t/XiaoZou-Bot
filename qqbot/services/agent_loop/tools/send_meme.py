"""SendMemeTool —— 把收藏过的表情包按 hash 发进当前 scope 的会话。

与 send_message 同为**同步**发送工具（任务与决策契约 §6）：execute() 里查
agent_memes 确认该 hash 收录过（**收藏是发送的权限边界**——磁盘上有但没
收录的图不能发，否则群友随手发过的任意照片都能被当表情包甩出来。收藏夹是
全局共享的，见 meme_store 模块 docstring：任何会话收录过即可发）→
读盘 → base64:// 编进 OneBot image 段（不用 file:// —— 不赌 napcat 与本进程
共享文件系统，容器化部署下 file:// 必挂）→ send_group_msg / send_private_msg。
成功**返回** ToolOutcome（带 message_id），失败也**返回** failure（不 raise）。

目标从 scope_key 解析，**无 target 参数** —— 隔离契约 §9：群操作目标一律取
当前 AgentLoop 的 scope，不让 LLM 跨会话投递（比 send_message 的 target 校验
更进一步：连参数都不给）。收藏夹共享**不**放宽这条：图发去哪仍只由当前
loop 的 scope 决定。

结果契约对齐 send_message §8.3：上游 ok 但无 message_id **不算成功** —— 投影
_build_author_index 认 send_meme 的 tool_result（message_id + self_id），把这
条折成"bot 自己的发言"；别人引用 bot 发的表情包时才有 from_self="true"。

失败语义新增 error_kind（见 表情包工具黑盒设计.md §错误语义）：
  unknown_meme        该 hash 从未被收录——先 save_meme 或从
                      <saved-memes> 里选一个存在的 hash。
  media_file_missing  收藏在、文件没了（media 目录被外部清理，违反"收藏钉住
                      文件"的契约）——不可自纠，防御位。
"""

from __future__ import annotations

import base64
from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.meme_store import get_meme
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._meme_common import (
    coerce_image_hash,
    media_path_for_hash,
)
from qqbot.services.agent_loop.tools._onebot_common import call_action, get_bot

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "send_meme.md")


class SendMemeTool(BaseTool):
    """实现 Tool 协议。GUEST + 无 bot 角色要求：发表情包就是发言，与
    send_message 同级；"什么场合该不该甩图"是 usage 文档的软规则。"""

    name = "send_meme"
    description = (
        "Send one saved meme (表情包) from your meme collection into the "
        "current scope, as a standalone image message. Pass image_hash = "
        'the hash= value of a <meme> entry in <saved-memes> (or of an '
        "<image> you just saved via save_meme). Only saved memes can be "
        "sent — an arbitrary timeline image hash that was never saved "
        "returns unknown_meme. Sending is synchronous: complete + <result> "
        "(with message_id) means it actually went out — never re-send it; "
        "complete + <error> means it did not."
    )
    usage_prompt = _USAGE_PROMPT
    # 与 send_message 同：只有聊天 scope 有"发言面"。
    allowed_scopes = ("group", "private")
    arguments_schema = {
        "type": "object",
        "properties": {
            "image_hash": {
                "type": "string",
                "description": (
                    "64-char sha256 hex of a SAVED meme, copied verbatim "
                    'from <saved-memes>/<meme hash="..."> (or from the '
                    "save_meme result)."
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

        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        if not scope_key or not isinstance(scope_key, str) or session_factory is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "send_meme unavailable: missing scope_key/session_factory context",
            )
        try:
            scope, group_id, user_id = parse_scope_key(scope_key)
        except ValueError as exc:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"invalid scope_key {scope_key!r}: {exc}",
                field="scope_key",
                user_fixable=False,
            )

        # 收藏边界：只发收录过的（全局收藏夹，任何会话收录过即命中；从未
        # 收录的 timeline 图片仍然拒发）。
        meme = await get_meme(session_factory, file_hash)
        if meme is None:
            return ToolOutcome.failure(
                "unknown_meme",
                f"hash {file_hash} is not a saved meme; pick a "
                "hash from <saved-memes>, or save the image first via "
                "save_meme",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        path = media_path_for_hash(file_hash)
        try:
            data = path.read_bytes()
        except OSError as exc:
            # 收藏在、文件没了：media 目录被外部清理，违反契约。不可自纠。
            logger.warning(
                "[send_meme] media file missing for saved meme {}: {}",
                file_hash,
                exc,
            )
            return ToolOutcome.failure(
                "media_file_missing",
                f"meme {file_hash} is saved but its media file is gone from "
                "disk (media dir was cleaned externally); it cannot be sent",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=False,
            )

        bot, fail = get_bot()
        if fail:
            return fail

        # base64:// 内联（napcat 标准入参形态），单图纯发、不混文字——要配字
        # 让模型另调一次 send_message，边界最清楚。
        content = [
            {
                "type": "image",
                "data": {"file": f"base64://{base64.b64encode(data).decode('ascii')}"},
            }
        ]
        if scope == "group":
            action = "send_group_msg"
            result, fail = await call_action(
                bot, action, group_id=int(group_id), message=content  # type: ignore[arg-type]
            )
        else:  # private（allowed_scopes 保证只剩这两种）
            action = "send_private_msg"
            result, fail = await call_action(
                bot, action, user_id=int(user_id), message=content  # type: ignore[arg-type]
            )
        if fail:
            return fail

        # §8.3 对齐 send_message：无 message_id 不算成功——投影折"bot 自己的
        # 发言"、别人引用时标 from_self 都靠它。
        message_id = _extract_message_id(result)
        if message_id is None:
            return ToolOutcome.failure(
                "upstream_action_failed",
                f"{action}: upstream returned ok but no message_id",
                action=action,
                retcode=0,
                upstream_status="ok",
                reason_code="missing_message_id",
                retryable=False,
                transient=False,
                user_fixable=False,
            )

        self_id = str(getattr(bot, "self_id", "") or "") or None
        logger.info(
            "[send_meme] sent {} to {} message_id={}",
            file_hash,
            scope_key,
            message_id,
        )
        return ToolOutcome.success(
            {
                "message_id": message_id,
                "self_id": self_id,
                "file_hash": file_hash,
                "sent": True,
            }
        )


def _extract_message_id(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("message_id")
    if isinstance(result, int):
        return result
    return None
