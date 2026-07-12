"""MemeTool —— 表情包收藏的一站式工具：action 分发 save / send / delete /
recaption 四个动作（表情包工具黑盒设计.md）。

2026-07-12 起由 save_meme / send_meme 两工具（2026-07-03）+ 当晚先行拆分的
delete_meme / recaption_meme 合并而来（应用户拍板"能力全集合在一个 meme
工具中"）：catalog 只暴露一个工具，模型按 `action` 选操作；旧工具名不再
注册，append-only 事件表里历史 tool_called 行的 `send_meme` 等名字原样保留
（投影 author index 兼容旧名，见 projection._build_author_index）。

四个动作共享同一身份标识：`image_hash`（sha256，与 timeline
<image hash="..."/>、收藏夹 <meme hash="..."> 同一值空间，LLM 原样照抄）。

  save       收录 timeline 里出现过的图片进全局收藏夹。定位 EventIngest
             已落盘的文件（内容寻址复用，不复制）→ 已收录直接 already_saved
             → 经 context 注入的 caption_image 看图生成中文描述（planner 可
             用 context_note 补聊天语境）→ 落 agent_memes。**支持批量**
             （2026-07-12，待办 #5）：image_hash 传数组（≤MAX_SAVE_BATCH
             张，去重保序）逐张走单张流程、逐项回执；只要有一张
             saved/already_saved 即 success，全部失败折 batch_save_failed。
             结构类错误（数组里有非法 hash / 超上限 / 空数组）整调拒绝，
             不进入逐张处理——与 send_message 的段级严格校验同风格。
  send       把收藏过的表情包发进当前 scope。**收藏是发送的权限边界**——
             磁盘上有但没收录的图不能发。无 target 参数：目标从 scope_key
             解析（隔离契约 §9，连跨会话投递的参数面都不给）。base64://
             内联单图（不赌 napcat 与本进程共享文件系统）。同步发送，结果
             对齐 send_message §8.3：ok 但无 message_id 不算成功——投影
             _build_author_index 折"bot 自己的发言"靠它。
  delete     把一条收藏移出收藏夹。**只删元数据、不动磁盘文件**（文件是
             EventIngest 的内容寻址缓存，归将来媒体 GC 管，黑盒设计 §7）；
             回执被删条目的描述，确认话术能点名绑定对象。
  recaption  给已收藏的表情包重新生成描述。**描述仍由 caption 链看图生成，
             模型不直接写**（save 的铁律不破）：模型能换的只有 context_note，
             未提供则沿用收录时留档的旧语境（"留档备将来重生成"的兑现点）。
             caption 失败不落表、旧描述保留。

共享语义：收藏夹全 bot 一份、所有聊天 scope 共用（隔离契约 §9.2 第 6 条
例外，见 meme_store 模块 docstring）——任何会话收录/删除，其余会话都可见。

失败语义（全程无 raise，error_kind 见黑盒设计 §8）：
  invalid_arguments    action 非法（bad_action）/ hash 非 64 位 hex
                       （bad_image_hash，批量时带 batch_index）/
                       context_note 非字符串（context_note_not_str）或给了
                       不消费它的动作（context_note_not_applicable）/
                       批量结构错（batch_not_supported：非 save 动作传数组；
                       empty_batch / too_many_images）。
  batch_save_failed    save 批量：无一张 saved/already_saved（逐项明细在
                       results；retryable=任一项 retryable）。
  image_not_found      save：hash 合法但盘上无此文件（抄错 / 未下载成功）。
  unknown_meme         send/delete/recaption：该 hash 不在收藏夹（未收录 /
                       已删除 / recaption 落表时被并发删除）。
  media_file_missing   send/recaption：收藏在、文件没了（违反 §7 钉住约束）。
  caption_failed       save/recaption：描述生成失败（retryable；recaption
                       旧描述保留）。
  internal_tool_error  session_factory / caption_image 未接线。
  另沿用 tool_unavailable_in_scope / no_bot_available /
  upstream_action_failed（含 missing_message_id）。

依赖注入：session_factory / caption_image / tool_call_event_id 全部来自
ToolWorker 统一注入的 run() context，无构造依赖。
"""

from __future__ import annotations

import base64
from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.meme_store import (
    delete_meme,
    get_meme,
    insert_meme,
    update_meme_description,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._meme_common import (
    coerce_image_hash,
    media_path_for_hash,
    sniff_mime,
)
from qqbot.services.agent_loop.tools._onebot_common import call_action, get_bot

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "meme.md")

# context_note 上限：它是 caption 的辅助输入，不是正文；过长说明模型在把
# 描述塞进 note（描述该由 caption 生成）。
MAX_CONTEXT_NOTE_CHARS = 300

_ACTIONS = ("save", "send", "delete", "recaption")
# context_note 只是 caption 的输入，仅这两个动作消费；其余动作给了 →
# invalid_arguments（大概率是 action 选错了，给精确反馈好过静默忽略）。
_NOTE_ACTIONS = ("save", "recaption")

# save 批量上限：每张都要一次 caption LLM 调用（串行），上限同时约束成本
# 与单拍时长；超限让模型分批（invalid_arguments too_many_images）。
MAX_SAVE_BATCH = 10


class MemeTool(BaseTool):
    """实现 Tool 协议。GUEST + 无 bot 角色要求：save/delete/recaption 只写
    自己的收藏表；send 就是发言，与 send_message 同级——"什么场合该不该
    甩图 / 仅在用户明确要求时收录、删除"都是 usage 文档的软规则，不设硬门禁。
    """

    name = "meme"
    description = (
        "Your meme (表情包) collection in one tool; `action` selects the "
        "operation. 'save' collects an image from this chat into the "
        "collection (the system looks at the image and writes the "
        "searchable description — you do not write it; optional "
        "context_note adds chat context the pixels cannot show; pass an "
        "ARRAY of up to 10 hashes to save several images at once). 'send' "
        "posts one SAVED meme into the current scope as a standalone image "
        "message — a form of speaking; sending is synchronous: complete + "
        "<result> (with message_id) means it actually went out, never "
        "re-send it. 'delete' removes a saved meme from the collection. "
        "'recaption' regenerates a saved meme's description (optional "
        "context_note steers it). Every action takes image_hash, copied "
        'VERBATIM: for save from an <image hash="..."/> tag in the '
        'timeline; for send/delete/recaption from a <meme hash="..."> '
        "entry in <saved-memes>. Only saved memes can be sent."
    )
    usage_prompt = _USAGE_PROMPT
    # 收藏夹与"发言面"都挂在聊天 scope 上；system scope 两者皆无。
    allowed_scopes = ("group", "private")
    arguments_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "send", "delete", "recaption"],
                "description": (
                    "Which operation to perform on the meme collection."
                ),
            },
            "image_hash": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "64-char sha256 hex of the image. For action=save copy "
                    'it verbatim from an <image hash="..."/> tag in the '
                    "timeline; for send/delete/recaption from a "
                    '<meme hash="..."> entry in <saved-memes> (or from an '
                    "earlier save result). action=save also accepts an "
                    "array of up to 10 hashes (batch save); the other "
                    "actions take a single string only."
                ),
            },
            "context_note": {
                "type": "string",
                "description": (
                    "Only for action=save/recaption: optional chat context "
                    "the image itself cannot show (e.g. 这是谁的名场面 / "
                    "本群拿它玩什么梗). Folded into the generated "
                    "description; not shown to users. For recaption, omit "
                    "to reuse the note recorded at save time."
                ),
            },
        },
        "required": ["action", "image_hash"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail

        action = arguments.get("action")
        if action not in _ACTIONS:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"action must be one of save/send/delete/recaption, "
                f"got {action!r}",
                field="action",
                reason_code="bad_action",
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        raw_hash = arguments.get("image_hash")
        # 批量形态（数组）只对 save 开放：send/delete/recaption 天然单对象
        # （一次发/删/改一张），传数组多半是想批量收录却选错了 action。
        batch_hashes: list[Any] | None = None
        if isinstance(raw_hash, list):
            if action != "save":
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"action={action!r} takes a single image_hash string; "
                    "only action=save accepts an array (batch save)",
                    field="image_hash",
                    reason_code="batch_not_supported",
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
            batch_hashes = raw_hash
            file_hash = None
        else:
            file_hash, fail = coerce_image_hash(raw_hash)
            if fail or file_hash is None:
                return fail or ToolOutcome.failure(
                    "invalid_arguments", "image_hash is required"
                )

        note_raw = arguments.get("context_note")
        if note_raw is not None:
            if action not in _NOTE_ACTIONS:
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"context_note is not accepted by action={action!r}; "
                    "it only feeds description generation (save/recaption)",
                    field="context_note",
                    reason_code="context_note_not_applicable",
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
            if not isinstance(note_raw, str):
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
                "meme unavailable: missing scope_key/session_factory context",
            )

        if action == "save" and batch_hashes is not None:
            return await self._save_batch(
                batch_hashes, context_note, scope_key, session_factory, context
            )
        if file_hash is None:
            # 逻辑上不可达（批量只对 save 开放且已在上面分发），纯窄化守卫。
            return ToolOutcome.failure(
                "invalid_arguments", "image_hash is required"
            )
        if action == "save":
            return await self._save(
                file_hash, context_note, scope_key, session_factory, context
            )
        if action == "send":
            return await self._send(file_hash, scope_key, session_factory)
        if action == "delete":
            return await self._delete(file_hash, session_factory)
        return await self._recaption(
            file_hash, context_note, session_factory, context
        )

    # ── action=save ──

    async def _save(
        self,
        file_hash: str,
        context_note: str | None,
        scope_key: str,
        session_factory: Any,
        context: dict,
    ) -> ToolOutcome:
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
                    "action": "save",
                    "file_hash": file_hash,
                    "already_saved": True,
                    "description": existing.description,
                }
            )

        captioner = context.get("caption_image")
        if captioner is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "meme save unavailable: caption_image not injected",
            )
        mime = sniff_mime(data)
        try:
            description = await captioner(data, mime, context_note)
        except Exception as exc:  # noqa: BLE001 —— caption 失败折结构化 outcome
            logger.warning("[meme.save] caption failed for {}: {}", file_hash, exc)
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
                    "action": "save",
                    "file_hash": file_hash,
                    "already_saved": True,
                    "description": racer.description if racer else description,
                }
            )
        logger.info(
            "[meme.save] saved {} into {} ({} chars)",
            file_hash,
            scope_key,
            len(description),
        )
        return ToolOutcome.success(
            {
                "action": "save",
                "file_hash": file_hash,
                "saved": True,
                "description": description,
            }
        )

    # ── action=save 批量形态 ──

    async def _save_batch(
        self,
        raw_hashes: list,
        context_note: str | None,
        scope_key: str,
        session_factory: Any,
        context: dict,
    ) -> ToolOutcome:
        """批量收录：结构整调校验（空/超限/任一 hash 非法都整体拒绝、不进入
        逐张处理）→ 去重保序 → 逐张复用 `_save` 单张流程 → 逐项回执。
        content 级失败（image_not_found / caption_failed …）只影响该项；
        无一张 saved/already_saved → batch_save_failed。context_note 作用于
        整批（每张 caption 都收到同一份语境）。"""
        if not raw_hashes:
            return ToolOutcome.failure(
                "invalid_arguments",
                "image_hash array is empty; pass 1..%d hashes"
                % MAX_SAVE_BATCH,
                field="image_hash",
                reason_code="empty_batch",
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        if len(raw_hashes) > MAX_SAVE_BATCH:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"image_hash array has {len(raw_hashes)} entries, max is "
                f"{MAX_SAVE_BATCH}; split into smaller batches",
                field="image_hash",
                reason_code="too_many_images",
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        hashes: list[str] = []
        for i, value in enumerate(raw_hashes):
            normalized, fail = coerce_image_hash(value)
            if fail or normalized is None:
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"image_hash[{i}] must be 64 hex chars (sha256), "
                    f"got {value!r}; copy each hash= value verbatim",
                    field="image_hash",
                    reason_code="bad_image_hash",
                    batch_index=i,
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
            if normalized not in hashes:  # 同批重复 hash 静默去重（保序）
                hashes.append(normalized)

        results: list[dict] = []
        saved_count = 0
        already_count = 0
        failed_count = 0
        any_retryable = False
        for file_hash in hashes:
            outcome = await self._save(
                file_hash, context_note, scope_key, session_factory, context
            )
            if outcome.ok:
                item = {k: v for k, v in outcome.result.items() if k != "action"}
                if item.get("already_saved"):
                    already_count += 1
                else:
                    saved_count += 1
            else:
                item = {
                    "file_hash": file_hash,
                    "error_kind": outcome.error_kind,
                    "error": outcome.error_message,
                }
                if outcome.extra.get("retryable"):
                    item["retryable"] = True
                    any_retryable = True
                failed_count += 1
            results.append(item)

        if saved_count + already_count == 0:
            # 无一成功：整体折失败，逐项明细随 extra 带回（retryable = 任一
            # 项 retryable，如 caption_failed；纯 hash 抄错类则重试无意义）。
            return ToolOutcome.failure(
                "batch_save_failed",
                f"none of the {len(hashes)} images could be saved; "
                "see per-item results",
                results=results,
                retryable=any_retryable,
                transient=any_retryable,
                user_fixable=not any_retryable,
            )
        logger.info(
            "[meme.save] batch into {}: {} saved, {} already, {} failed",
            scope_key,
            saved_count,
            already_count,
            failed_count,
        )
        return ToolOutcome.success(
            {
                "action": "save",
                "batch": True,
                "results": results,
                "saved_count": saved_count,
                "already_saved_count": already_count,
                "failed_count": failed_count,
            }
        )

    # ── action=send ──

    async def _send(
        self, file_hash: str, scope_key: str, session_factory: Any
    ) -> ToolOutcome:
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
                "action=save",
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
                "[meme.send] media file missing for saved meme {}: {}",
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
            "[meme.send] sent {} to {} message_id={}",
            file_hash,
            scope_key,
            message_id,
        )
        return ToolOutcome.success(
            {
                "action": "send",
                "message_id": message_id,
                "self_id": self_id,
                "file_hash": file_hash,
                "sent": True,
            }
        )

    # ── action=delete ──

    async def _delete(
        self, file_hash: str, session_factory: Any
    ) -> ToolOutcome:
        # 前查为了两件事：未收录给精确的 unknown_meme（而不是"删了 0 条"的
        # 含混成功）；命中时把描述带回结果，确认话术能点名删的是哪张。
        meme = await get_meme(session_factory, file_hash)
        if meme is None:
            return ToolOutcome.failure(
                "unknown_meme",
                f"hash {file_hash} is not a saved meme; nothing to delete — "
                "copy the hash from a <meme> entry in <saved-memes>",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        # 并发窗口：前查之后别的删除先到 → rowcount=0。结果状态与本次意图
        # 一致（该 hash 已不在收藏夹），照常回执 deleted。
        await delete_meme(session_factory, file_hash)
        logger.info("[meme.delete] removed {} from collection", file_hash)
        return ToolOutcome.success(
            {
                "action": "delete",
                "file_hash": file_hash,
                "deleted": True,
                "description": meme.description,
            }
        )

    # ── action=recaption ──

    async def _recaption(
        self,
        file_hash: str,
        new_note: str | None,
        session_factory: Any,
        context: dict,
    ) -> ToolOutcome:
        # 只给收录过的换描述：收藏是本动作的操作边界（与 send 的发送边界
        # 同构）——timeline 里见过但没收录的图没有描述可换。
        meme = await get_meme(session_factory, file_hash)
        if meme is None:
            return ToolOutcome.failure(
                "unknown_meme",
                f"hash {file_hash} is not a saved meme; only saved memes "
                "have a description to regenerate — copy the hash from a "
                "<meme> entry in <saved-memes>",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        # 语境：新 note 优先；未提供沿用收录时留档的旧 note（"留档备将来
        # 重生成"的兑现点）。
        context_note = new_note if new_note is not None else meme.context_note

        path = media_path_for_hash(file_hash)
        try:
            data = path.read_bytes()
        except OSError as exc:
            # 收藏在、文件没了：违反 §7 钉住约束（media 目录被外部清理）。
            logger.warning(
                "[meme.recaption] media file missing for saved meme {}: {}",
                file_hash,
                exc,
            )
            return ToolOutcome.failure(
                "media_file_missing",
                f"meme {file_hash} is saved but its media file is gone from "
                "disk (media dir was cleaned externally); its description "
                "cannot be regenerated",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=False,
            )

        captioner = context.get("caption_image")
        if captioner is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "meme recaption unavailable: caption_image not injected",
            )
        mime = sniff_mime(data)
        try:
            description = await captioner(data, mime, context_note)
        except Exception as exc:  # noqa: BLE001 —— caption 失败折结构化 outcome
            logger.warning(
                "[meme.recaption] caption failed for {}: {}", file_hash, exc
            )
            return ToolOutcome.failure(
                "caption_failed",
                f"description regeneration failed: {exc}; the old "
                "description is kept unchanged",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )
        description = str(description or "").strip()
        if not description:
            return ToolOutcome.failure(
                "caption_failed",
                "description regeneration returned empty text; the old "
                "description is kept unchanged",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )

        updated = await update_meme_description(
            session_factory,
            file_hash=file_hash,
            description=description,
            context_note=context_note,
        )
        if not updated:
            # 并发窗口：前查之后、落表之前该收藏被 delete 删掉了。
            return ToolOutcome.failure(
                "unknown_meme",
                f"meme {file_hash} was removed from the collection while "
                "regenerating its description",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        logger.info(
            "[meme.recaption] recaptioned {} ({} chars)",
            file_hash,
            len(description),
        )
        return ToolOutcome.success(
            {
                "action": "recaption",
                "file_hash": file_hash,
                "recaptioned": True,
                "description": description,
                "previous_description": meme.description,
            }
        )


def _extract_message_id(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("message_id")
    if isinstance(result, int):
        return result
    return None
