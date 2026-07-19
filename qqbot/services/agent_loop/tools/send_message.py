"""SendMessageTool —— 向当前 scope 的会话**同步**发送一条消息。

send_message 和其它工具一样是同步的：execute() 里经 get_bot() / call_action 调
napcat（send_group_msg / send_private_msg），成功**返回** ToolOutcome（带 napcat
分配的 message_id），失败也**返回** ToolOutcome.failure（不 raise）。所以
`<tool-call name="send_message">` **直接反映"是否真发出去了"**——两态语义下
status="complete" + `<result>`（带 message_id）= 已送达，complete + `<error>` =
没发出（带原因），与 websearch / kick 等完全一致；不存在"入队成功但实际没送达"
的脱节。

命名：工具层叫 `send_message`（发一条消息），与 OneBot 消息段层的 `reply`
（引用某一条消息）区分——二者不再共用 `reply` 一词；消息段 `{"type":"reply"}`
的名字不变。见 开发文档/v2.0/工具设计/send_message工具黑盒设计.md §1。

设计动机：群聊里大多数消息并非对小奏说，把 send_message 暴露为工具能让 LLM 每
tick 自然做"要不要说话"的决定——和调 websearch / search_history 一样 opt-in。

历史：v1 的 ReplyAction → agent.reply_emitted → ReplySendWorker 异步投递那条链路
已**废弃**；本工具不再写 reply_emitted/delivered/failed、不再依赖 ReplySendWorker。
别人引用 bot 自己发言的识别改由投影从本工具 tool_result 里的 message_id + self_id
折出（见 projection._build_author_index）。

依赖注入：系统依赖由 ToolWorker 在 run() 的 context 统一注入；本工具只用
scope_key（校验 target 不跨 scope）+ get_bot()（经 bot_registry 取 Bot 实例）。

契约：任务与决策契约.md §6（send_message 为同步工具，tool-call 直接表达送达结果）。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import call_action, get_bot

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "send_message.md")


class SendMessageTool(BaseTool):
    """实现 Tool 协议。无构造依赖：scope_key 由 ToolWorker 注入；Bot 实例从
    bot_registry 取 —— 与 kick / respond_to_group_join_request 等同步工具同构。
    """

    name = "send_message"
    description = (
        "Send a message into the current scope's chat (group or private). "
        "In group chat, most messages are NOT addressed to you — call this "
        "tool only when you've decided to actively speak (e.g. you were "
        "@-mentioned, your past reply was quoted, the scope is private, or "
        "a clearly answerable question went unanswered). Skipping this tool "
        "and emitting `idle` is the correct choice for any tick where no one "
        "is addressing you. The arguments mirror the OneBot V11 send-message "
        "payload: content (list of segments) and target "
        "(kind/group_id|user_id matching the current scope). "
        "Sending is synchronous: a tool-call at status=\"complete\" with a "
        "<result> means the message actually went out (the result carries "
        "its message_id) — it is already said, never re-send it; complete "
        "with an <error> means it did NOT go out — read the error and "
        "decide whether to retry or drop."
    )
    usage_prompt = _USAGE_PROMPT
    # 仅 group / private 可见可调：system scope 没有聊天面（target 必须匹配
    # scope，system 下必然 target_scope_mismatch），同时让用法文档里的角色卡
    # （Voice 节）随 usage_docs 的 scope 过滤天然不进 system loop 的 prompt。
    allowed_scopes = ("group", "private")
    # required_permission / required_bot_role 用 BaseTool 默认值（GUEST /
    # 不限 bot 角色）：发言不分群员等级，小奏普通群员也能说话。
    arguments_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "array",
                "description": (
                    "OneBot V11 segment list. Each item is "
                    "{\"type\": \"text|at|reply|face|...\", \"data\": {...}}. "
                    "See the tool's usage doc for the full grammar."
                ),
                "items": {"type": "object"},
            },
            "target": {
                "type": "object",
                "description": (
                    "{\"kind\": \"group\", \"group_id\": <int>} for group "
                    "scope, or {\"kind\": \"private\", \"user_id\": <int>} "
                    "for private scope. MUST match the current "
                    "<agent-input scope=\"...\">."
                ),
                "properties": {
                    "kind": {"type": "string", "enum": ["group", "private"]},
                    "group_id": {"type": "integer"},
                    "user_id": {"type": "integer"},
                },
                "required": ["kind"],
            },
        },
        "required": ["content", "target"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # GUEST + allowed_scopes=("group","private")：enforce_access 的 scope
        # 闸门在此拦下 system loop 硬调与 scope_key 缺失（折
        # tool_unavailable_in_scope）。全程无 raise。
        if fail := await self.enforce_access(context):
            return fail

        # 防御层：allowed_scopes 非空时 scope_key 缺失已被上面的 scope 闸门
        # 拦下，此检查仅在将来放开 allowed_scopes 时兜底。
        scope_key = context.get("scope_key")
        if not scope_key or not isinstance(scope_key, str):
            return _invalid_args(
                "missing_required_field",
                "send_message requires scope_key from caller context",
                field="scope_key",
                user_fixable=False,
            )

        if fail := _validate_arguments(arguments):
            return fail

        # §6：content 段白名单 + 结构 + 顺序 + 每段字段前置校验（不把非法段推给
        # 上游碰运气，给 LLM 精确的 segment_index/segment_type 而非一段中文 wording）。
        if fail := _validate_content(arguments.get("content")):
            return fail

        # §7：target 结构错 → invalid_arguments；结构对但发错会话 →
        # target_scope_mismatch（会话路由错误，与纯参数错分开，见设计 §7.3）。
        target = arguments.get("target")
        if fail := _validate_target(target, scope_key=scope_key):
            return fail

        bot, fail = get_bot()
        if fail:
            return fail

        # 同步发送：napcat 动作失败由 call_action 折成 upstream_action_failed
        # （带 retcode + upstream_wording + stream）→ tool-call 直接 failed。
        # kind / group_id|user_id 已由 _validate_target 保证合法、可 int() 化。
        kind = target["kind"]
        content = arguments["content"]
        if kind == "group":
            action = "send_group_msg"
            result, fail = await call_action(
                bot, action, group_id=int(target["group_id"]), message=content
            )
        else:  # private
            action = "send_private_msg"
            result, fail = await call_action(
                bot, action, user_id=int(target["user_id"]), message=content
            )
        if fail:
            return fail

        # §8.3 强约束：上游 status=ok 但没有 message_id **不算成功**——没有它，投影
        # 无法把这条折成"bot 自己的发言"（别人引用时 from_self="true" 丢失），撤回/引用也
        # 失去锚点。视作 upstream_action_failed(reason_code=missing_message_id)。
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
        logger.info("[send_message] sent to {} message_id={}", scope_key, message_id)
        # message_id + self_id 供投影 _build_author_index 折出"bot 自己的发言"，
        # 让别人 <reply to_message_id="..."> 引用 bot 时能标 from_self="true" +
        # from_qq=self_id。self_id 键名保留（napcat/OneBot 原词，且
        # bot_role_observed 等多个生产方共用），xml_format.md 注明 = bot_qq。
        return ToolOutcome.success(
            {
                "message_id": message_id,
                "self_id": self_id,
                "target": target,
                "sent": True,
            }
        )


def _extract_message_id(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("message_id")
    if isinstance(result, int):
        return result
    return None


# §6.1 当前允许的出站段类型（白名单）。其它一律 unsupported_segment_type——不放行
# 给上游碰运气，给 LLM 精确的 segment_index/segment_type 而非一段 napcat 中文 wording。
_ALLOWED_SEGMENT_TYPES = frozenset({"text", "at", "reply", "face"})


def _invalid_args(
    reason_code: str,
    message: str,
    *,
    field: str = "content",
    segment_index: int | None = None,
    segment_type: Any = None,
    user_fixable: bool = True,
) -> ToolOutcome:
    """构造结构类 ``invalid_arguments`` 失败（带弱语义字段，见设计 §9.2）。

    retryable/transient 恒 False（重发同样参数必再失败）；user_fixable 默认 True
    （改参数即可）。segment_index/segment_type 仅在段级错误时带上。
    """
    extra: dict[str, Any] = {
        "field": field,
        "reason_code": reason_code,
        "retryable": False,
        "transient": False,
        "user_fixable": user_fixable,
    }
    if segment_index is not None:
        extra["segment_index"] = segment_index
    if segment_type is not None:
        extra["segment_type"] = segment_type
    return ToolOutcome.failure("invalid_arguments", message, **extra)


def _validate_content(content: Any) -> ToolOutcome | None:
    """§6：校验 content 段数组。返回 None=通过，否则 invalid_arguments 失败 outcome。

    - 非空数组；至少一个可见负载（不能只有空白文本）
    - 只放行 text/at/reply/face；其它段 → unsupported_segment_type
    - reply 段至多一个、且必须在 content[0]
    - 每段字段按 §6.4 校验
    注：§6.3「@all 与普通 at 混用」是**软规则**，交 prompt 约束，不在此拦。
    """
    if not isinstance(content, list) or not content:
        return _invalid_args(
            "content_empty", "send_message.content must be a non-empty list"
        )
    reply_count = 0
    has_visible = False
    for i, seg in enumerate(content):
        if not isinstance(seg, dict):
            return _invalid_args(
                "bad_field_type", f"content[{i}] must be an object", segment_index=i
            )
        seg_type = seg.get("type")
        if seg_type not in _ALLOWED_SEGMENT_TYPES:
            return _invalid_args(
                "unsupported_segment_type",
                f"content[{i}].type={seg_type!r} is not supported; "
                "allowed: text/at/reply/face",
                segment_index=i,
                segment_type=str(seg_type) if seg_type is not None else None,
            )
        data = seg.get("data")
        if not isinstance(data, dict):
            return _invalid_args(
                "bad_field_type",
                f"content[{i}].data must be an object",
                segment_index=i,
                segment_type=seg_type,
            )
        if seg_type == "text":
            text = data.get("text")
            if not isinstance(text, str):
                return _invalid_args(
                    "bad_field_type",
                    f"content[{i}].data.text must be a string",
                    segment_index=i,
                    segment_type="text",
                )
            if text.strip():
                has_visible = True
        elif seg_type == "at":
            if fail := _validate_at(data, i):
                return fail
            has_visible = True
        elif seg_type == "reply":
            reply_count += 1
            if reply_count > 1:
                return _invalid_args(
                    "duplicate_reply_segment",
                    "at most one reply segment is allowed",
                    segment_index=i,
                    segment_type="reply",
                )
            if i != 0:
                return _invalid_args(
                    "reply_segment_not_first",
                    "the reply segment must be content[0]",
                    segment_index=i,
                    segment_type="reply",
                )
            rid = data.get("id")
            if rid is None or not str(rid).strip():
                return _invalid_args(
                    "missing_required_field",
                    f"content[{i}] reply segment requires a non-empty data.id",
                    segment_index=i,
                    segment_type="reply",
                )
            has_visible = True
        elif seg_type == "face":
            fid = data.get("id")
            if fid is None or not str(fid).strip():
                return _invalid_args(
                    "missing_required_field",
                    f"content[{i}] face segment requires a non-empty data.id",
                    segment_index=i,
                    segment_type="face",
                )
            has_visible = True
    if not has_visible:
        return _invalid_args(
            "content_all_blank",
            "send_message.content has no visible payload (only blank text)",
        )
    return None


def _validate_at(data: dict, i: int) -> ToolOutcome | None:
    """§6.4 at 段：data.qq 是 string/number；``"all"``（@全体）或可归一化为正整数 QQ。"""
    qq = data.get("qq")
    if isinstance(qq, bool) or not isinstance(qq, (str, int)):
        return _invalid_args(
            "bad_field_type",
            f"content[{i}].data.qq must be a string or number",
            segment_index=i,
            segment_type="at",
        )
    qs = str(qq).strip()
    if qs == "all":
        return None
    if not qs.isdigit() or int(qs) <= 0:
        return _invalid_args(
            "bad_field_type",
            f"content[{i}].data.qq must be 'all' or a positive QQ id, got {qq!r}",
            segment_index=i,
            segment_type="at",
        )
    return None


def _validate_arguments(arguments: Any) -> ToolOutcome | None:
    """顶层参数兼容闸：字段已下架后，明确拒绝继续传旧字段。"""
    if not isinstance(arguments, dict):
        return _invalid_args(
            "bad_field_type",
            "send_message arguments must be an object",
            field="arguments",
        )
    if "related_image_hashes" in arguments:
        return _invalid_args(
            "unsupported_field",
            "send_message.related_image_hashes is no longer supported",
            field="related_image_hashes",
        )
    return None


def _validate_target(target: Any, *, scope_key: str) -> ToolOutcome | None:
    """§7：target 必须与当前 scope 一致。返回 None=通过，否则失败 outcome。

    结构错（非 object / kind 非法 / id 缺失或类型错）→ ``invalid_arguments``；
    结构对但会话不符（kind 或 id 与 scope 不一致）→ ``target_scope_mismatch``。
    二者对 LLM 的后续处理不同（前者修参数、后者修目标/放弃），故分开，见设计 §7.3。
    """
    if not isinstance(target, dict):
        return _invalid_args(
            "bad_field_type",
            "send_message.target must be an object",
            field="target",
        )
    try:
        scope, group_id, user_id = parse_scope_key(scope_key)
    except ValueError as exc:
        return _invalid_args(
            "bad_field_type",
            f"invalid scope_key {scope_key!r}: {exc}",
            field="scope_key",
            user_fixable=False,
        )
    kind = target.get("kind")
    if kind not in ("group", "private"):
        return _invalid_args(
            "bad_field_type",
            f"send_message.target.kind must be 'group' or 'private', got {kind!r}",
            field="target",
        )
    if kind != scope:
        actual_id = target.get("group_id") if kind == "group" else target.get("user_id")
        return _target_mismatch(
            scope_key,
            kind,
            actual_id,
            f"target.kind={kind!r} does not match current scope {scope_key!r}",
        )
    if scope == "group":
        gid, fail = _coerce_target_id(target.get("group_id"), "group_id")
        if fail is not None:
            return fail
        if gid != group_id:
            return _target_mismatch(
                scope_key,
                "group",
                gid,
                f"target.group_id={gid} does not match current scope {scope_key!r}",
            )
    else:  # private
        uid, fail = _coerce_target_id(target.get("user_id"), "user_id")
        if fail is not None:
            return fail
        if uid != user_id:
            return _target_mismatch(
                scope_key,
                "private",
                uid,
                f"target.user_id={uid} does not match current scope {scope_key!r}",
            )
    return None


def _coerce_target_id(
    value: Any, field: str
) -> tuple[int | None, ToolOutcome | None]:
    """target.group_id/user_id → int（LLM 有时把数字给成字符串）。缺失 →
    missing_required_field；非数值 → bad_field_type（均为结构类 invalid_arguments）。"""
    if value is None:
        return None, _invalid_args(
            "missing_required_field",
            f"target.{field} is required",
            field="target",
        )
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, _invalid_args(
            "bad_field_type",
            f"target.{field} must be an integer, got {value!r}",
            field="target",
        )


def _target_mismatch(
    scope_key: str, actual_kind: Any, actual_id: Any, message: str
) -> ToolOutcome:
    """构造 ``target_scope_mismatch`` 失败（会话路由错误，独立于结构类 invalid_arguments）。"""
    return ToolOutcome.failure(
        "target_scope_mismatch",
        message,
        expected_scope=scope_key,
        actual_target_kind=actual_kind,
        actual_target_id=actual_id,
        retryable=False,
        transient=False,
        user_fixable=True,
    )
