"""OneBot 动作类工具的公共小工具。

新增的一批 napcat 动作工具（kick / ban / set_card / get_member_info /
poke / recall / ...）都遵循同一套出站约定，把重复部分收敛到这里：

- **group_id 从 scope_key 注入**：群操作的目标群一律取当前 AgentLoop 的
  scope，绝不让 LLM 用 arguments 传任意 group_id —— 隔离契约 §9。
- **Bot 实例从 bot_registry 取**（`get_bot()`）：与 send_message / respond_to_request 同路。
- **出站统一走 `call_action`**：把 napcat 动作失败（ActionFailed / NetworkError）
  收敛成结构化 `upstream_action_failed`（带 retcode + action + upstream_status /
  upstream_message / upstream_wording / stream + retryable/transient/user_fixable
  弱语义字段，见设计 §9.2）。

**全链路无 raise 控制流**：这些 helper 一律**返回** ``ToolOutcome.failure(...)``
（或 ``(value, ToolOutcome | None)`` 元组）表达可预期失败，工具 execute() 把失败
直接 return 上来，不 raise。唯一例外：``call_action`` 遇到**非动作失败**的预料外
异常（网络栈内部错等）原样上抛，由 ``BaseTool.run`` 兜底成 ``internal_tool_error``。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.tool_registry import ToolOutcome


def require_group_scope(
    context: dict, tool_name: str
) -> tuple[int | None, ToolOutcome | None]:
    """校验当前是 group scope 并取 group_id。

    成功 → ``(group_id, None)``；失败 → ``(None, failure)``：非 group scope →
    ``tool_unavailable_in_scope``；scope_key 缺失 / 非法 → ``invalid_arguments``。
    """
    scope_key = context.get("scope_key")
    if not scope_key or not isinstance(scope_key, str):
        return None, ToolOutcome.failure(
            "invalid_arguments",
            f"{tool_name} requires scope_key from caller context",
        )
    try:
        scope, group_id, _ = parse_scope_key(scope_key)
    except ValueError as exc:
        return None, ToolOutcome.failure(
            "invalid_arguments", f"invalid scope_key {scope_key!r}: {exc}"
        )
    if scope != "group" or group_id is None:
        return None, ToolOutcome.failure(
            "tool_unavailable_in_scope",
            f"{tool_name} is only available in group scope, got "
            f"scope_key={scope_key!r}",
            actual_scope=scope,
        )
    return group_id, None


def get_bot() -> tuple[Any, ToolOutcome | None]:
    """取一个已连接的 Bot 实例。成功 → ``(bot, None)``；取不到 →
    ``(None, ToolOutcome.failure("no_bot_available", ...))``。"""
    bot = bot_registry.get_any()
    if bot is None:
        return None, ToolOutcome.failure("no_bot_available", "no_bot_available")
    return bot, None


def coerce_int(value: Any, field: str) -> tuple[int | None, ToolOutcome | None]:
    """把 arguments 里的 user_id 等转 int。成功 → ``(int, None)``；非法 →
    ``(None, ToolOutcome.failure("invalid_arguments", ...))``。

    LLM 有时把数字给成字符串（"12345"），统一收口转换，失败给清晰结果。
    """
    if value is None:
        return None, ToolOutcome.failure(
            "invalid_arguments", f"{field} is required"
        )
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, ToolOutcome.failure(
            "invalid_arguments", f"{field} must be an integer, got {value!r}"
        )


_TRUE_STRINGS = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "n", "off"})


def coerce_bool(
    value: Any, field: str, *, default: bool | None = None
) -> tuple[bool | None, ToolOutcome | None]:
    """把 arguments 里的布尔值稳妥转 bool。成功 → ``(bool, None)``；非法 →
    ``(None, ToolOutcome.failure("invalid_arguments", ...))``。

    为什么不直接 ``bool(arguments.get(...))``：``bool()`` 把任意非空字符串都判 True，
    于是 LLM 传来的 ``"false"`` / ``"0"`` / ``"no"`` 会被**静默取反**成 True（语义
    错误，且工具契约是"黑盒收 dict 自校验"，这属于实际 bug）。这里统一收口：
      - 真正的 ``bool`` → 原样；
      - 识别的字符串（``true/false/1/0/yes/no/y/n/on/off``，大小写/空白不敏感）
        与整数 ``0/1`` → 对应 bool；
      - ``value`` 缺省（None，键未给）：``default`` 非 None → 用 ``default``；``default``
        亦为 None → 视作必填，返回 ``invalid_arguments``；
      - 其它（``"maybe"`` / ``2`` / list / dict ...）→ ``invalid_arguments``（不猜）。
    """
    if value is None:
        if default is None:
            return None, ToolOutcome.failure(
                "invalid_arguments", f"{field} is required"
            )
        return default, None
    if isinstance(value, bool):
        return value, None
    # 注意 bool 是 int 子类，已在上面拦掉；这里只剩纯 int。
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value), None
        return None, ToolOutcome.failure(
            "invalid_arguments",
            f"{field} must be a boolean, got {value!r}",
        )
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_STRINGS:
            return True, None
        if v in _FALSE_STRINGS:
            return False, None
    return None, ToolOutcome.failure(
        "invalid_arguments",
        f"{field} must be a boolean (true/false), got {value!r}",
    )


async def call_action(
    bot: Any, action: str, **params: Any
) -> tuple[Any, ToolOutcome | None]:
    """统一出站：调 napcat 的 OneBot V11 动作。

    成功 → ``(napcat 返回值, None)``（查询类工具用返回值；动作类忽略即可）；
    动作失败 → ``(None, ToolOutcome.failure("upstream_action_failed", <人类原因>,
    action=..., retcode=..., upstream_status/upstream_message/upstream_wording/
    stream=..., retryable/transient/user_fixable=False))``。

    - 优先走 bot 上自动生成的同名方法（``bot.set_group_kick(...)``，与既有单测
      stub 一致）；下划线前缀的扩展动作（``_send_group_notice``）回退 ``call_api``。
    - napcat ``status="failed"`` 时 nonebot 抛 ``ActionFailed``，完整响应在
      ``exc.info``（含 ``retcode`` / ``message`` / ``wording`` / ``status`` /
      ``stream``）。``error_message`` 取最佳人类可读原因（wording > message >
      str(exc)）；``message`` / ``wording`` / ``status`` / ``stream`` 原样透传进
      extra（``upstream_*`` / ``stream``）供 LLM 精确判因，不只靠 wording 猜语义。
    - **非动作失败**的预料外异常（网络栈内部错等）原样上抛 —— 由 ``BaseTool.run``
      兜底 ``internal_tool_error``（本函数只把"动作失败"折成返回值）。
    """
    method = getattr(bot, action, None)
    try:
        if callable(method):
            return await method(**params), None
        return await bot.call_api(action, **params), None
    except Exception as exc:  # noqa: BLE001 —— 仅拦"动作失败"，其余上抛给 run 兜底
        failure = _as_action_failure(exc)
        if failure is None:
            raise
        # error_message 取最佳人类可读原因（wording > message > str(exc)）；原始
        # message / wording / status / stream 同时透传进 extra（设计 §9.2：上游细节
        # 在共享 helper 层统一补齐，所有 onebot 工具一致）。retryable / transient 恒
        # False —— 上游拒绝重发同样调用通常同样失败；是否值得重试由 prompt 按
        # retcode / upstream_wording 判（不硬编码进 tool result）。
        human = failure.get("wording") or failure.get("message") or str(exc)
        return None, ToolOutcome.failure(
            "upstream_action_failed",
            str(human),
            action=action,
            retcode=failure.get("retcode"),
            upstream_status=failure.get("status"),
            upstream_message=failure.get("message"),
            upstream_wording=failure.get("wording"),
            stream=failure.get("stream"),
            retryable=False,
            transient=False,
            user_fixable=False,
        )


# ── 群角色层级：参数相关的细粒度平台权限**前置**判定 ──────────────────────
#
# enforce_access（tool_registry）判的是"通用门禁"：发起人 tier + bot 自身**静态**
# 角色（required_bot_role）+ scope。但一批 napcat 动作还有**与参数（目标是谁）
# 相关**的层级限制——管理员踢不掉群主、禁言不了另一个管理员、撤不了更高角色的
# 消息、解散群须群主。这些以前全靠 napcat 返回 upstream_action_failed 兜底；下面
# 的 helper 让工具在动手前就按 QQ 语义**前置**判掉，给 LLM 精确的
# permission_denied_bot_role（带 target_role），而非一段 napcat 中文 wording。
#
# 关键保守约定：**目标角色查不到（None）就不在此拦**——交给 napcat 兜底，避免把
# "查询失败/已退群"误判成"越权"（假阴性比放行更糟）。

_ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}


def role_rank(role: str | None) -> int:
    """群角色 → 数值等级（owner=2 > admin=1 > member=0）。未知 / None → -1
    （最低，保守当作"无有效角色"）。"""
    if not isinstance(role, str):
        return -1
    return _ROLE_RANK.get(role.strip().lower(), -1)


async def fetch_member_role(
    bot: Any, group_id: int, user_id: int
) -> str | None:
    """实时查某成员在群里的当前角色（owner/admin/member）。查不到 / napcat 报错
    （如已退群）/ 无 role 字段 → None（调用侧据此**跳过**层级前置判定，交 napcat）。

    ``no_cache=True`` 取最新，与 tool_registry 里发起人 tier 的实时解析同源——目标
    刚被升/降级也能立刻反映，不吃缓存。"""
    try:
        info = await bot.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id), no_cache=True
        )
    except Exception:  # noqa: BLE001 —— 查不到 / napcat 错都当无角色（保守）
        return None
    if not isinstance(info, dict):
        return None
    role = info.get("role")
    return role.strip().lower() if isinstance(role, str) and role.strip() else None


async def fetch_message_author(
    bot: Any, message_id: int
) -> tuple[int | None, str | None]:
    """查某条消息的作者 user_id + 其群角色（撤别人消息的前置判定用）。

    ``get_msg`` 拿 sender：user_id 用于判"是不是 bot 自己发的"；role 优先取
    ``sender.role``（napcat 群消息一般带），缺失返回 None（角色层级交 napcat）。
    查不到 / 报错 → ``(None, None)``，调用侧**跳过**前置判定（保持"撤消息只凭
    message_id"的宽松默认）。"""
    try:
        msg = await bot.get_msg(message_id=int(message_id))
    except Exception:  # noqa: BLE001 —— get_msg 不可用 / 报错都退化为"作者未知"
        return None, None
    if not isinstance(msg, dict):
        return None, None
    sender = msg.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    uid = sender.get("user_id") or msg.get("user_id")
    if uid is None:
        return None, None
    role = sender.get("role")
    role = role.strip().lower() if isinstance(role, str) and role.strip() else None
    try:
        return int(uid), role
    except (TypeError, ValueError):
        return None, role


def enforce_actor_outranks_target(
    tool_name: str,
    action_desc: str,
    bot_role: str | None,
    target_role: str | None,
    target_user_id: int,
) -> ToolOutcome | None:
    """群管理动作的**角色层级**前置判定：bot 只能对**严格低于自己角色**的目标动手。

    admin 可管 member；owner 可管 admin/member；谁都动不了 owner，admin 也动不了
    另一个 admin。违反 → ``permission_denied_bot_role``（先于任何 napcat 动作，带
    ``required_bot_role`` / ``actual_bot_role`` / ``target_role``）。满足、或
    ``target_role`` 未知（None）→ 返回 None（放行；未知交 napcat 兜底，见文件注释）。

    与 QQ/NapCat 实际语义对齐；调用侧应先过 enforce_access 的静态 required_bot_role
    （确保 bot 至少是 admin），本函数只补"目标是谁"这一层。"""
    if target_role is None:
        return None
    if role_rank(bot_role) > role_rank(target_role):
        return None
    # 目标是 admin/owner → 需要 bot 是 owner 才可能压得住；目标是 member 而仍走到
    # 这里，说明 bot 自己角色未知/member（静态门禁本应已拦），要求至少 admin。
    need = "owner" if role_rank(target_role) >= _ROLE_RANK["admin"] else "admin"
    return ToolOutcome.failure(
        "permission_denied_bot_role",
        f"{tool_name} cannot {action_desc} {target_user_id}: the bot "
        f"(role={bot_role or 'unknown'}) must outrank the target "
        f"(role={target_role}) — QQ forbids acting on an equal-or-higher role.",
        required_bot_role=need,
        actual_bot_role=bot_role or None,
        target_role=target_role,
    )


def _as_action_failure(exc: Exception) -> dict | None:
    """识别 OneBot 动作失败并抽出上游细节；识别不出返回 None。

    返回 dict：``{retcode, message, wording, status, stream}``（对应 NapCat
    BaseResponse 同名字段，缺失键为 None）。用 duck-typing（看 ``.info`` dict / 异常
    类名），不硬 import nonebot —— 工作区没装 nonebot，运行时（server）才有；测试可用
    带 ``.info`` 的假异常驱动。
    """
    info = getattr(exc, "info", None)
    if isinstance(info, dict) and (
        "retcode" in info or "wording" in info or "message" in info
    ):
        return {
            "retcode": info.get("retcode"),
            "message": info.get("message"),
            "wording": info.get("wording") or info.get("msg"),
            "status": info.get("status"),
            "stream": info.get("stream"),
        }
    if type(exc).__name__ in ("ActionFailed", "NetworkError"):
        return {
            "retcode": getattr(exc, "retcode", None),
            "message": None,
            "wording": str(exc) or type(exc).__name__,
            "status": "failed",
            "stream": None,
        }
    return None
