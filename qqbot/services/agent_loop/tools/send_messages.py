"""SendMessagesTool —— Planner 亲自发言的唯一出口（2026-07-31 删除 Replyer）。

链路（重构提案-删除Replyer.md §2/§4）：`reply` 起一段等待；到点后
``runtime.reply_task_completed`` 唤醒 Planner；Planner 结合最新时间流决定
idle / 查证 / 调本工具把话真正发出去。本工具**始终可调用**——运行时不检查
是否存在完成事件，也不查 ReplyTask；"正常在完成事件之后发言"是提示词纪律，
不是工具权限（§0.5，有意的软约束，不得补授权门闩）。**但这条实现事实只写在
代码里**：2026-08-01 起 `send_messages.md` 不再向模型主动交底"没人拦你"——
工具用法文档没有义务告诉模型某条纪律缺少强制力。

它是一个普通 Program Effect：执行器先写 ``agent.tool_called`` 意图，再调用
本工具，最后把结构化 receipts 写进 ``agent.tool_result | tool_failed``。它不
查询或修改 ReplyTask，也不新增 fence / finalizer。若 OneBot 已出手但 terminal
尚未写成时进程退出，启动收口器会把半截调用标成 ``interrupted`` / ``uncertain``，
**永不自动重放**。其 `<工具>send_messages` 行块（气泡 + 回执）就是时间线上的
唯一发言记录；不再派生第二条发言行，`<旧发言>` 只兼容历史链路。

结果语义（status 随 receipts 一起落 terminal payload）：

- ``sent``     全部气泡拿到 OneBot 成功响应与 message_id → ``tool_result``；
  这不是用户端最终可见性的证明；
- ``partial``  部分 sent、部分明确 failed → ``tool_failed``（携带完整逐条
  receipts，已 sent 的气泡是既成事实，不得重发）；
- ``failed``   全部明确未发出 → ``tool_failed``；
- ``uncertain`` 至少一条送达与否无法确认 → ``tool_failed``（可能已发出，
  禁止"保险再发一遍"）。

依赖注入：scope_key / session_factory 来自 ProgramExecutor 的 run() context；
目标群取自 scope_key，模型不传 target（跨群隔离，§4.1）。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.outbound_messages import (
    MAX_OUTBOUND_MESSAGES,
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

# ── 气泡 schema（2026-08-01）：`items` 原本只是 {"type": "object"}，两种气泡
# 唯一同框的地方是 messages.description 那段散文，chat 占前 2/3、meme 挂在分号
# 后面——模型写 JSON 时最强的结构先验是 schema，而 schema 里表情包等于不存在。
# 现按 task.py 的既有写法展开成 kind 判别的两分支，把"平级"在模型真正读的那份
# 结构里说出来。schema 纯文档用途（tool_registry 模块头），真正的校验始终是
# outbound_messages.validate_messages——两边形状逐字对齐，不得出现 schema 放行
# 而校验拒绝的错位。
#
# 段级不写 additionalProperties：validate_content 只看 type/data，不拒绝多余
# 键，schema 不能比校验更严。reply 段至多一个且必须在 content[0]，JSON Schema
# 表达不了，留在 send_messages.md。
_SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "enum": ["text", "at", "reply", "face"],
            "description": "段类型。",
        },
        "data": {
            "type": "object",
            "description": "该段类型的字段，全部位于 data 内。",
        },
    },
    "required": ["type", "data"],
}

# 两分支各自的 required / additionalProperties 就是 validate_messages 里
# extras = set(item) - {...} 那两处检查的形状。
_CHAT_BUBBLE_SCHEMA = {
    "properties": {
        "kind": {
            "const": "chat",
            "description": "固定为 chat，表示一条聊天气泡。",
        },
        "content": {
            "type": "array",
            "minItems": 1,
            "description": "OneBot V11 段数组。",
            "items": _SEGMENT_SCHEMA,
        },
    },
    "required": ["kind", "content"],
    "additionalProperties": False,
}

_MEME_BUBBLE_SCHEMA = {
    "properties": {
        "kind": {
            "const": "meme",
            "description": "固定为 meme，表示一个表情包气泡。",
        },
        "image_hash": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{12,64}$",
            "description": (
                "表情包收藏 <meme> 行中的哈希，12 位前缀原样照抄（也接受完整 64 位）。"
            ),
        },
    },
    "required": ["kind", "image_hash"],
    "additionalProperties": False,
}


class SendMessagesTool(BaseTool):
    name = "send_messages"
    program_kind = "effect"
    max_call_sites = 2
    # 私聊没有 AgentLoop（Supervisor 丢弃 private:*），system scope 没有聊天
    # 目标——不照抄旧 send_message.py 的 ("group", "private")。
    allowed_scopes = ("group",)
    description = (
        "向当前群发送一条或多条有序气泡。messages 中每项可为 OneBot V11 聊天气泡或"
        "收藏表情包气泡。标准发言链路先由 reply 表示正在输入并启动等待，再在 "
        "<reply-task-completed> 出现后调用本工具；运行时不强制该顺序。返回值中的"
        "逐气泡回执是送达状态记录；status=uncertain 表示至少一条气泡可能已经"
        "送达。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OUTBOUND_MESSAGES,
                "description": (
                    "按发送顺序排列的气泡数组，一条或多条均可。每项是一个 "
                    "chat 气泡或一个 meme 气泡，两者平级、可任意穿插。"
                ),
                "items": {"oneOf": [_CHAT_BUBBLE_SCHEMA, _MEME_BUBBLE_SCHEMA]},
            },
        },
        "required": ["messages"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["sent"]},
            "message_ids": {
                "type": "array",
                "items": {"type": ["integer", "string"]},
            },
            "sent_messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "kind": {"type": "string"},
                        "content": {"type": "array", "items": {}},
                        "image_hash": {"type": ["string", "null"]},
                        "status": {"type": "string"},
                        "message_id": {"type": ["integer", "string", "null"]},
                        "self_id": {"type": ["string", "null"]},
                        "receipt": {"type": "object", "properties": {}},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "required": ["status", "message_ids", "sent_messages"],
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

        # ── 静态校验：形状、段白名单、气泡条数上限（无副作用；meme 不限量）。
        prepared, fail = validate_messages(arguments.get("messages"))
        if fail is not None:
            return fail

        # ── 动态 preflight：meme 是否仍在收藏、媒体是否可读（仍无副作用）。
        if session_factory is None and any(item["kind"] == "meme" for item in prepared):
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

        # ── OneBotGateway 逐条发送 → 逐条 receipts → status 折叠（§4.3）。
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
        logger.warning("[send_messages] {} delivery {}: {}", scope_key, status, reason)
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
