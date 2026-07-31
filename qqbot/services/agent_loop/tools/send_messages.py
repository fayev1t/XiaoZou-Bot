"""SendMessagesTool —— Planner 亲自发言的唯一出口（2026-07-31 删除 Replyer）。

链路（重构提案-删除Replyer.md §2/§4）：`reply` 保存分析并等待；到点后
``runtime.reply_task_completed`` 唤醒 Planner；Planner 结合最新时间流决定
idle / 查证 / 调本工具把话真正发出去。本工具**始终可调用**——运行时不检查
是否存在完成事件，也不校验内容是否符合某份 analysis；"正常在完成事件之后
发言"是提示词纪律，不是工具权限（§0.5，有意的软约束，不得补授权门闩）。

它是一个**普通 ToolWorker 工具**（§4.3/§4.5）：走通用租约与 terminal 机制，
不写任何领域/runtime 事件，不查询或修改 ReplyTask，也不为自己新增 fence 或
finalizer——发送结果作为结构化 receipts 放进 ToolOutcome，`agent.tool_result
| tool_failed` 就是发送的唯一持久事实，其 `<tool-call>` 行（args + 结果回执）
即时间线上的发言记录（2026-07-31 实施后调整：不再派生独立 `<my-reply>` 行，
同一句话两处渲染是复读诱饵；`<my-reply>` 仅渲染旧链路历史事件）。因此它与
其它不可逆工具共有同一个已知窗口：OneBot 已成功、terminal 未写成、进程退出
→ 租约到期后可能重新执行（§4.5 明确接受，观测后按独立基础设施提案统一
解决）。

结果语义（status 随 receipts 一起落 terminal payload）：

- ``sent``     全部气泡确认发出 → ``tool_result``；
- ``partial``  部分 sent、部分 failed → ``tool_failed``（携带完整逐条
  receipts，已 sent 的气泡是既成事实，不得重发）；
- ``failed``   全部明确未发出 → ``tool_failed``；
- ``uncertain`` 至少一条送达与否无法确认 → ``tool_failed``（可能已发出，
  禁止"保险再发一遍"）。

依赖注入：scope_key / session_factory 来自 ToolWorker 的 run() context；
目标群取自 scope_key，模型不传 target（跨群隔离，§4.1）。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.outbound_messages import (
    delivery_status,
    first_error_reason,
    invalid_args,
    preflight_memes,
    redact_runtime_value,
    send_all,
    validate_messages,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import get_bot

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "send_messages.md")


class SendMessagesTool(BaseTool):
    name = "send_messages"
    # 私聊没有 AgentLoop（Supervisor 丢弃 private:*），system scope 没有聊天
    # 目标——不照抄旧 send_message.py 的 ("group", "private")。
    allowed_scopes = ("group",)
    description = (
        "Actually send your words into this group chat, as 1-4 ordered "
        "bubbles (at most one of them a saved meme). This is the only way "
        "anything you write becomes visible. The normal flow is: store your "
        "analysis with `reply`, wait for <reply-task-completed>, re-read the "
        "latest timeline, then either stay silent, investigate further, or "
        "call this tool with the final wording you choose now. The result's "
        "per-bubble receipts on this call's own row are the record of what "
        "actually reached QQ. sent means said — never re-send it; uncertain "
        "means the messages MAY already be out — never re-send the same "
        "intent as a new call either."
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": (
                    "Ordered bubbles. Each item is either "
                    '{"kind":"chat","content":[<OneBot v11 segments>]} with '
                    "text/at/reply/face segments (each field inside \"data\"), "
                    'or {"kind":"meme","image_hash":"<sha256 copied from '
                    "<saved-memes>>\"}."
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["messages"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if not isinstance(arguments, dict):
            return invalid_args(
                "arguments_not_object",
                "send_messages arguments must be a JSON object",
                field="arguments",
            )
        if fail := await self.enforce_access(context):
            return fail
        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        if not isinstance(scope_key, str) or not scope_key:
            return ToolOutcome.failure(
                "internal_tool_error", "send_messages requires scope_key"
            )

        extras = sorted(set(arguments) - {"messages"})
        if extras:
            return invalid_args(
                "unexpected_argument",
                f"send_messages received unknown argument(s): {', '.join(extras)}",
            )

        # ── 静态校验：形状、段白名单、气泡与 meme 数量（无副作用）。
        prepared, fail = validate_messages(arguments.get("messages"))
        if fail is not None:
            return fail

        # ── 动态 preflight：meme 是否仍在收藏、媒体是否可读（仍无副作用）。
        if session_factory is None and any(
            item["kind"] == "meme" for item in prepared
        ):
            return ToolOutcome.failure(
                "internal_tool_error",
                "send_messages requires session_factory to send a meme",
            )
        loaded, error = await preflight_memes(session_factory, prepared)
        if error is not None:
            reason_code, message = error
            return invalid_args(reason_code, message)

        bot, fail = get_bot()
        if fail:
            return fail

        # ── OneBot 逐条发送 → 逐条 receipts → status 折叠（§4.3）。
        receipts = await send_all(bot, scope_key, loaded)
        status = delivery_status(receipts)
        public = redact_runtime_value(receipts)
        message_ids = [
            item["message_id"]
            for item in public
            if item.get("status") == "sent" and item.get("message_id") is not None
        ]
        if status == "sent":
            return ToolOutcome.success(
                {
                    "status": "sent",
                    "message_ids": message_ids,
                    "sent_messages": public,
                }
            )
        reason = first_error_reason(receipts) or (
            "delivery result is unknown for at least one bubble"
            if status == "uncertain"
            else "no bubble was delivered"
        )
        logger.warning(
            "[send_messages] {} delivery {}: {}", scope_key, status, reason
        )
        return ToolOutcome.failure(
            "upstream_action_failed",
            reason,
            status=status,
            message_ids=message_ids,
            sent_messages=public,
            retryable=False,
            transient=False,
            # partial/uncertain 不是"改参数重试"能修的；failed 可另组新调用。
            user_fixable=status == "failed",
        )
