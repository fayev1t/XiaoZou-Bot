"""ReplyTask 到点后的单次组稿与逐条发送执行器。"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from typing import Any, Awaitable, Callable

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.delivery_claims import (
    has_delivery_claim,
    try_claim_once_strict,
)
from qqbot.services.agent_loop.event_writer import parse_scope_key, write_runtime_event
from qqbot.services.agent_loop.meme_store import get_meme
from qqbot.services.agent_loop.reply_task import (
    ReplyTaskState,
    load_open_reply_task,
    load_open_reply_tasks,
    load_recent_reply_tasks,
    scope_lock,
)
from qqbot.services.agent_loop.replyer import Replyer, ReplyerError
from qqbot.services.agent_loop.tools._meme_common import media_path_for_hash
from qqbot.services.agent_loop.tools._onebot_common import call_action, get_bot
from qqbot.services.agent_loop.tools.send_message import (
    _extract_message_id,
    _validate_content,
)

logger = get_logger(__name__)


class ReplyExecutor:
    def __init__(
        self,
        *,
        session_factory: Any,
        projector: Any,
        wake_scope: Callable[[str], Awaitable[None]],
        replyer: Replyer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._projector = projector
        self._wake_scope = wake_scope
        self._replyer = replyer or Replyer()
        self._handles: dict[str, asyncio.TimerHandle] = {}
        self._running: set[asyncio.Task[None]] = set()
        self._stopped = False

    async def start(self) -> None:
        self._stopped = False
        recent = await load_recent_reply_tasks(self._session_factory)
        recovered: set[str] = set()
        for task in recent:
            claimed_without_event = task.state == "open" and await has_delivery_claim(
                self._session_factory, task.latest_event_id, "reply_flush"
            )
            if task.state != "claimed" and not claimed_without_event:
                continue
            reason = "process restarted after flush claim; not retrying"
            if claimed_without_event:
                reason = (
                    "process restarted after durable claim but before claim event; "
                    "not retrying"
                )
            await self._write_uncertain_recovery(task, reason)
            recovered.add(task.reply_task_id)
            await self._wake_for_attention(task.scope_key)
        for task in (
            task
            for task in recent
            if task.state == "open" and task.reply_task_id not in recovered
        ):
            if task.flush_at <= china_now():
                await write_runtime_event(
                    self._session_factory,
                    event_type="runtime.reply_task_overdue",
                    scope_key=task.scope_key,
                    visibility="agent_visible",
                    correlation_id=task.correlation_id or new_event_id(),
                    causation_id=task.latest_event_id,
                    payload={
                        "reply_task_id": task.reply_task_id,
                        "revision": task.revision,
                        "flush_at": task.flush_at.isoformat(),
                    },
                )
                await self._wake_for_attention(task.scope_key)
            else:
                self._schedule(task.reply_task_id, task.revision, task.flush_at)

    async def stop(self) -> None:
        self._stopped = True
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()
        running = list(self._running)
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running.clear()

    async def notify(
        self,
        scope_key: str,
        reply_task_id: str,
        revision: int,
        flush_at: datetime,
        event_id: str,
    ) -> None:
        del scope_key, event_id
        self._schedule(reply_task_id, revision, flush_at)

    def _schedule(
        self, reply_task_id: str, revision: int, flush_at: datetime
    ) -> None:
        if self._stopped:
            return
        old = self._handles.pop(reply_task_id, None)
        if old is not None:
            old.cancel()
        delay = max((flush_at - china_now()).total_seconds(), 0.0)
        loop = asyncio.get_running_loop()
        self._handles[reply_task_id] = loop.call_later(
            delay, self._launch, reply_task_id, revision
        )

    def _launch(self, reply_task_id: str, revision: int) -> None:
        if self._stopped:
            return
        task = asyncio.create_task(
            self._fire(reply_task_id, revision),
            name=f"reply_flush:{reply_task_id}:{revision}",
        )
        self._running.add(task)
        task.add_done_callback(self._fire_done)

    def _fire_done(self, task: asyncio.Task[None]) -> None:
        self._running.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("[reply_executor] flush task crashed: {}", error)

    async def _fire(self, reply_task_id: str, revision: int) -> None:
        self._handles.pop(reply_task_id, None)
        task = await self._find_open(reply_task_id)
        if task is None:
            return
        async with scope_lock(task.scope_key):
            current = await load_open_reply_task(
                self._session_factory, task.scope_key
            )
            if current is None or current.reply_task_id != reply_task_id:
                return
            if current.revision != revision:
                self._schedule(
                    current.reply_task_id, current.revision, current.flush_at
                )
                return
            if current.flush_at > china_now():
                self._schedule(
                    current.reply_task_id, current.revision, current.flush_at
                )
                return
            if not await try_claim_once_strict(
                self._session_factory, current.latest_event_id, "reply_flush"
            ):
                return
            correlation_id = current.correlation_id or new_event_id()
            claimed_id = await write_runtime_event(
                self._session_factory,
                event_type="runtime.reply_flush_claimed",
                scope_key=current.scope_key,
                visibility="runtime_only",
                correlation_id=correlation_id,
                causation_id=current.latest_event_id,
                payload={
                    "reply_task_id": current.reply_task_id,
                    "revision": current.revision,
                    "claimed_at": china_now().isoformat(),
                },
            )
        await self._compose_and_send(current, claimed_id, correlation_id)

    async def _find_open(self, reply_task_id: str) -> ReplyTaskState | None:
        for task in await load_open_reply_tasks(self._session_factory):
            if task.reply_task_id == reply_task_id:
                return task
        return None

    async def _compose_and_send(
        self, task: ReplyTaskState, claimed_id: str, correlation_id: str
    ) -> None:
        cutoff_event_id: str | None = None
        status = "failed"
        sent_messages: list[dict] = []
        reason: str | None = None
        try:
            context = await self._projector.build_context(
                scope_key=task.scope_key,
                correlation_id=correlation_id,
                tick_seq=0,
                now=china_now(),
            )
            if context.timeline:
                cutoff_event_id = context.timeline[-1].event_id
            if task.mode == "verbatim":
                plan = {
                    "messages": [
                        {"kind": "chat", "content": item["content"]}
                        for item in task.verbatim_messages
                    ],
                    "empty_reason": None,
                }
            else:
                plan = await self._replyer.compose(
                    task, context, context.saved_memes
                )
            messages = plan.get("messages") or []
            if not messages:
                status = "empty"
                reason = str(plan.get("empty_reason") or "empty reply")
            else:
                prepared, error = await self._preflight(messages)
                if error:
                    raise ReplyerError(error)
                sent_messages = await self._send_all(task.scope_key, prepared)
                status = _delivery_status(sent_messages)
        except Exception as exc:
            logger.warning(
                "[reply_executor] task {} failed: {}", task.reply_task_id, exc
            )
            status = "failed"
            sent_messages = []
            reason = f"{type(exc).__name__}: {exc}"[:500]

        # 无论组稿/发送结果如何，只尝试写一次 final。若持久化本身处在“已提交但
        # 调用方收到异常”的不确定区，第二次写会制造两个相互冲突的最终事实；留给
        # 启动恢复从 durable claim 补 uncertain 才符合 at-most-once。
        try:
            await self._write_flushed(
                task,
                claimed_id,
                correlation_id,
                status=status,
                sent_messages=sent_messages,
                cutoff_event_id=cutoff_event_id,
                reason=reason,
            )
        except Exception as exc:
            logger.error(
                "[reply_executor] final persistence failed for {}: {}",
                task.reply_task_id,
                exc,
            )
            return
        # 2026-07-22 起无论 status 都唤醒（含 sent）：flush 才是新架构里"话已
        # 说完"的时刻，这次唤醒对应 send_message 时代批次收口唤醒的锚点搬迁。
        # final 先落库、唤醒在后，醒来的拍必能看到 <my-reply>；要不要续说由
        # 模型按 prompt 判断（多段任务续发下一段，无事则 idle）——程序不替
        # 模型决定何时思考（模型+prompt 优先，同 loop.py 批次门闩拆除注记）。
        await self._wake_for_attention(task.scope_key)

    async def _wake_for_attention(self, scope_key: str) -> None:
        """最终事件已经写定后 best-effort 唤醒；失败不得制造第二条 final。"""
        try:
            await self._wake_scope(scope_key)
        except Exception as exc:
            logger.warning(
                "[reply_executor] wake after final failed for {}: {}",
                scope_key,
                exc,
            )

    async def _preflight(
        self, messages: list[dict]
    ) -> tuple[list[dict], str | None]:
        if len(messages) > 4:
            return [], "replyer returned more than 4 messages"
        prepared: list[dict] = []
        meme_count = 0
        for index, item in enumerate(messages):
            kind = item.get("kind")
            if kind == "chat":
                content = item.get("content")
                if fail := _validate_content(content):
                    return [], f"chat[{index}] invalid: {fail.error_message}"
                prepared.append({"kind": "chat", "content": content})
            elif kind == "meme":
                meme_count += 1
                if meme_count > 1:
                    return [], "at most one meme is allowed"
                image_hash = item.get("image_hash")
                if not isinstance(image_hash, str):
                    return [], f"meme[{index}] image_hash is invalid"
                meme = await get_meme(self._session_factory, image_hash)
                if meme is None:
                    return [], f"meme[{index}] is no longer saved"
                try:
                    data = media_path_for_hash(image_hash).read_bytes()
                except OSError as exc:
                    return [], f"meme[{index}] media missing: {exc}"
                prepared.append(
                    {"kind": "meme", "image_hash": image_hash, "data": data}
                )
            else:
                return [], f"messages[{index}] has unknown kind"
        return prepared, None

    async def _send_all(
        self, scope_key: str, prepared: list[dict]
    ) -> list[dict]:
        scope, group_id, user_id = parse_scope_key(scope_key)
        bot, fail = get_bot()
        if fail:
            return [
                _failed_receipt(index, item, fail.error_kind, fail.error_message)
                for index, item in enumerate(prepared)
            ]
        receipts: list[dict] = []
        for index, item in enumerate(prepared):
            if item["kind"] == "chat":
                content = item["content"]
            else:
                content = [
                    {
                        "type": "image",
                        "data": {
                            "file": "base64://"
                            + base64.b64encode(item["data"]).decode("ascii")
                        },
                    }
                ]
            public_item = {k: v for k, v in item.items() if k != "data"}
            try:
                if scope == "group":
                    result, action_fail = await call_action(
                        bot,
                        "send_group_msg",
                        group_id=int(group_id),
                        message=content,
                    )
                else:
                    result, action_fail = await call_action(
                        bot,
                        "send_private_msg",
                        user_id=int(user_id),
                        message=content,
                    )
            except Exception as exc:
                receipts.append(
                    _uncertain_receipt(
                        index,
                        public_item,
                        "upstream_delivery_uncertain",
                        f"{type(exc).__name__}: {exc}"[:500],
                    )
                )
                continue
            if action_fail:
                receipts.append(
                    _failed_receipt(
                        index,
                        public_item,
                        action_fail.error_kind,
                        action_fail.error_message,
                        action_fail.extra,
                    )
                )
                continue
            message_id = _extract_message_id(result)
            if message_id is None:
                receipts.append(
                    _uncertain_receipt(
                        index,
                        public_item,
                        "missing_message_id",
                        "upstream returned ok but no message_id",
                        result,
                    )
                )
                continue
            receipts.append(
                {
                    "index": index,
                    **public_item,
                    "status": "sent",
                    "message_id": message_id,
                    "self_id": str(getattr(bot, "self_id", "") or "") or None,
                    "receipt": _public_receipt(result),
                }
            )
        return receipts

    async def _write_flushed(
        self,
        task: ReplyTaskState,
        claimed_id: str,
        correlation_id: str,
        *,
        status: str,
        sent_messages: list[dict],
        cutoff_event_id: str | None,
        reason: str | None = None,
    ) -> None:
        public_messages = _redact_runtime_value(sent_messages)
        payload: dict[str, Any] = {
            "reply_task_id": task.reply_task_id,
            "revision": task.revision,
            "status": status,
            "timeline_cutoff_event_id": cutoff_event_id,
            "message_ids": [
                item["message_id"]
                for item in public_messages
                if item.get("status") == "sent" and item.get("message_id") is not None
            ],
            "sent_messages": public_messages,
        }
        if reason:
            payload["reason"] = _redact_runtime_value(reason)
        await write_runtime_event(
            self._session_factory,
            event_type="runtime.reply_flushed",
            scope_key=task.scope_key,
            visibility="agent_visible",
            correlation_id=correlation_id,
            # agent-visible 最终事实直接锚到最后一次落稿/并稿 tool_called；
            # runtime-only claim 只是去重协调，不应成为模型可见因果链的入口。
            causation_id=task.source_tool_call_event_id or claimed_id,
            payload=payload,
        )

    async def _write_uncertain_recovery(
        self, task: ReplyTaskState, reason: str
    ) -> None:
        await write_runtime_event(
            self._session_factory,
            event_type="runtime.reply_flushed",
            scope_key=task.scope_key,
            visibility="agent_visible",
            correlation_id=task.correlation_id or new_event_id(),
            causation_id=(
                task.source_tool_call_event_id or task.latest_event_id
            ),
            payload={
                "reply_task_id": task.reply_task_id,
                "revision": task.revision,
                "status": "uncertain",
                "message_ids": [],
                "sent_messages": [],
                "reason": reason,
            },
        )


def _failed_receipt(
    index: int,
    item: dict,
    error_kind: str | None,
    error_message: str | None,
    extra: dict | None = None,
) -> dict:
    return {
        "index": index,
        **{k: v for k, v in item.items() if k != "data"},
        "status": "failed",
        "error": {
            "kind": error_kind,
            "message": _redact_runtime_value(error_message),
            **_public_receipt(extra),
        },
    }


def _uncertain_receipt(
    index: int,
    item: dict,
    error_kind: str,
    error_message: str,
    receipt: Any = None,
) -> dict:
    return {
        "index": index,
        **{k: v for k, v in item.items() if k != "data"},
        "status": "uncertain",
        "receipt": _public_receipt(receipt),
        "error": {
            "kind": error_kind,
            "message": _redact_runtime_value(error_message),
        },
    }


def _delivery_status(receipts: list[dict]) -> str:
    statuses = [item.get("status") for item in receipts]
    if statuses and all(status == "sent" for status in statuses):
        return "sent"
    if any(status == "sent" for status in statuses):
        return "partial"
    if any(status == "uncertain" for status in statuses):
        return "uncertain"
    return "failed"


def _public_receipt(value: Any) -> dict:
    """保留可审计回执，但禁止 OneBot 回显的图片正文进入事件流。"""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _redact_runtime_value(item) for key, item in value.items()
    }


def _redact_runtime_value(value: Any) -> Any:
    if isinstance(value, str):
        if "base64://" in value:
            return "<base64-redacted>"
        return value
    if isinstance(value, bytes):
        return "<binary-redacted>"
    if isinstance(value, dict):
        return {
            str(key): _redact_runtime_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_runtime_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)
