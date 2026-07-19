"""Tool 协议与 registry。

一个 Tool 描述了 agent 可调用的一项能力：
  - name                 工具名（agent.tool_called.payload.tool_name 匹配它）
  - description          提示给 LLM 的简短说明（写入 prompt 的 tool_catalog）
  - arguments_schema     JSON Schema (dict)，描述 arguments 字段，纯文档用途
  - required_permission  (可选) 触发用户最低 tier；默认 GUEST
  - required_bot_role    (可选) 要求 bot 自己在群里的最低角色 "admin"/"owner"；默认
                         None（不限）。旧 require_bot_admin=True 等价 "admin"
  - allowed_scopes       (可选) 限定可见/可调的 scope；默认 None（不限）
  - execute(arguments, **context)  子类实现工具逻辑，**返回** ToolOutcome（成功或
                         失败），永不 raise；BaseTool.run() 把它归一成统一输出

权限/判定语义（**全部在工具内**，详见 BaseTool 与 core/permissions.py）：

- required_permission / required_bot_role / allowed_scopes 是**纯元数据**，不影响
  catalog 可见性（scope 隔离除外）—— LLM 总能看见自己 scope 内的全部工具，靠
  description 自判是否可调。
- execute() 第一行 ``if fail := await self.enforce_access(context): return fail``——
  enforce_access = enforce_scope（越 scope → tool_unavailable_in_scope）+
  enforce_permission（发起人 tier，**实时**查其当前群角色）+ enforce_bot_admin（bot
  自身角色，同样**实时**查 napcat、查不到才回退注入的快照）。AgentLoop **不再做任何
  scope/tier/role 判定或闸门**。
- ToolRegistry.catalog() 把权限元数据透传出来，让 LLM 的 tool 描述自带它们。

执行约定（任务与决策契约 §5.1, §6, §7.2）—— **全程无 raise 控制流**：
  - 子类实现 execute()：成功 ``return ToolOutcome.success(result)``；可预期失败把
    helper 返回的 ``ToolOutcome.failure(error_kind, msg, **extra)`` 直接 return 上来
    （enforce_access / coerce_int / require_group_scope / get_bot / call_action 都
    **返回**失败，不 raise）。
  - BaseTool.run() 是统一出口（调用方只见它、永不 raise）：归一 execute 的返回；只
    兜底**预料外**的第三方异常（httpx / sqlalchemy / napcat 适配器 raise 的）→
    internal_tool_error。
  - ToolWorker 只搬运 ToolOutcome → agent.tool_result / agent.tool_failed。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from qqbot.core.logging import get_logger
from qqbot.core.permissions import (
    PermissionTier,
    resolve_user_tier_from_event,
    tier_from_group_role,
)

logger = get_logger(__name__)

# _effective_bot_role 的缓存哨兵：区分"没缓存"与"缓存值就是 None"，
# 让 bot 角色一次 execute() 内只实时查一次。
_UNSET = object()


@dataclass(frozen=True)
class ToolOutcome:
    """工具调用的结构化输出 —— 取代旧的"成功 return dict / 失败 raise 字符串"。

    工具直接产出 outcome（成功 → ``result``；失败 → ``error_kind`` /
    ``error_message`` / ``extra``）。ToolWorker 只把它机械搬运成
    ``agent.tool_result`` / ``agent.tool_failed``，**不再 introspect 异常类型、不猜
    error_kind**（契约 §6/§7.2）；Projection 据这两类事件渲染 ``<tool-call>``
    （两态：complete + ``<result>`` / ``<error>``）。

    ``error_kind`` 收敛成固定语义集（见 protocol.md §permissions、契约 §7.2）：
      ``tool_unavailable_in_scope`` / ``invalid_arguments`` /
      ``permission_denied_user_tier`` / ``permission_denied_bot_role`` /
      ``no_bot_available`` / ``upstream_action_failed`` / ``internal_tool_error``。
    ``extra`` 是结构化附加字段（required_tier / actual_bot_role / retcode /
    wording / action ...），随 tool_failed.payload 落表供审计与渲染。
    """

    ok: bool
    result: Any = None
    error_kind: str | None = None
    error_message: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def success(cls, result: Any = None, **fields: Any) -> "ToolOutcome":
        """成功 outcome。``result`` 传 dict 或用 kwargs 拼字段，二者可合并。"""
        if fields:
            merged = dict(result) if isinstance(result, dict) else {}
            merged.update(fields)
            return cls(ok=True, result=merged)
        return cls(ok=True, result={} if result is None else result)

    @classmethod
    def failure(
        cls, error_kind: str, error_message: str, **extra: Any
    ) -> "ToolOutcome":
        """失败 outcome。``error_kind`` 用固定语义集，``error_message`` 回给 LLM。"""
        return cls(
            ok=False,
            error_kind=error_kind,
            error_message=str(error_message)[:1000],
            extra=dict(extra),
        )


# 全链路**无 raise 控制流**：工具（含 enforce_* / coerce_int / require_group_scope /
# get_bot / call_action 等 helper）一律**返回** ToolOutcome / (value, ToolOutcome|None)
# 表达失败，不再 raise 任何结构化异常。故不存在 ToolError / ToolPermissionError ——
# 失败即 ``ToolOutcome.failure(error_kind, msg, **extra)``。真正预料外的第三方异常
# （httpx / sqlalchemy / napcat 适配器）由 ``BaseTool.run`` 的兜底 except 收敛成
# ``internal_tool_error``，也不越出工具边界。


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    arguments_schema: dict

    # `usage_prompt` 不是必填——老工具或单测里的 stub 可以省略。
    # ToolRegistry.usage_docs() 用 getattr 兜底，缺失等同于空串。
    # 命名约定：把详细用法（何时该调、参数搭配、结果解读、踩坑）放在
    # sibling .md，由 prompts.load_sibling_md(__file__, "...") 加载注入。

    # `required_permission` / `required_bot_role` 都不是必填——老工具或单测
    # stub 可以省略。catalog() 与工具内 enforce_* 用 getattr 兜底，缺失等同于
    # GUEST / None。

    # `allowed_scopes` 同样可选：None（默认）= 不限 scope，任何 AgentLoop 都
    # 可见可调；非空序列 = 仅列出的 scope（"system"/"group"/"private"）可见，
    # catalog(scope) 在别的 scope 里隐藏它、工具内 enforce_scope 拒绝硬调（返回
    # tool_unavailable_in_scope）。这是契约 §2.2「scope 限定工具（如群管理类
    # 仅 GroupAgentLoop 可见）」的落地点。

    async def run(self, arguments: dict, **context: Any) -> Any:
        """运行工具，**返回** ToolOutcome（成功或失败）。

        BaseTool 子类改实现 `execute()`、继承 `BaseTool.run()`（统一出口、永不
        raise）。`arguments` 是 LLM 给的、匹配 `arguments_schema` 的 dict；
        `context` 是系统注入的同一套 kwargs（scope_key / task_id / correlation_id
        / session_factory / triggered_by_event_id / bot_role ...），每个工具收到
        的完全相同，故无需任何 __init__ 接线。详见 BaseTool。
        """
        ...


class BaseTool:
    """工具基类：实现 run()（统一出口、永不 raise）+ enforce_access（scope/tier/
    bot 角色判定），把"可选属性"默认值固化在一处，realize "系统只认输入输出"。

    继承它后，工具只需实现 `execute()`（**返回** ToolOutcome）并覆盖与默认不同的
    字段（几乎总是 name / description / arguments_schema / usage_prompt）；权限
    相关默认 GUEST / 不限 bot 角色。需要敏感权限的工具显式覆盖 `required_permission`
    / `required_bot_role`（如踢人工具 = ADMIN + "admin"）。

    系统级依赖（session_factory 写/查 agent_events、触发身份 triggered_by_event_id
    / bot_role 等）一律由 ToolWorker 在 run() 的 context 里统一注入，不走各工具的
    __init__ —— build_default_registry 无参构造所有工具，系统也不必按名字特判。

    注：`get_tool_required_permission` / `get_tool_require_bot_admin` /
    `get_tool_required_bot_role` 仍保留为防御层 —— 测试 stub 或第三方工具不继承
    BaseTool 时也能拿到默认值。
    """

    usage_prompt: str = ""
    required_permission: PermissionTier = PermissionTier.GUEST
    require_bot_admin: bool = False
    # None = 不限 scope（默认，所有 AgentLoop 可见可调）；非空 tuple 限定
    # 仅这些 scope 可见（如 ban / respond_to_group_join_request = ("group",)，
    # 契约 §2.2）。
    allowed_scopes: tuple[str, ...] | None = None
    # bot 自身在群里的最低角色要求：None=不限；"admin"=须管理员或群主；
    # "owner"=须群主。由 enforce_bot_admin 在工具内判——bot 角色经
    # _effective_bot_role **实时**查 napcat（查不到才回退 context.bot_role 快照）。
    required_bot_role: str | None = None
    # ToolWorker terminal 后的 Planner 唤醒策略。普通工具恒唤醒；reply 这类
    # “成功只落中间状态”的工具可设 on_failure，避免成功后自激活空拍。
    wake_policy: str = "always"

    async def run(self, arguments: dict, **context: Any) -> "ToolOutcome":
        """工具统一出口：**无论成功还是失败都返回 ToolOutcome，永不 raise**。

        「输入参数 → 工具直接给统一结果」的落地点。子类实现 ``execute()``，全链路
        **无 raise 控制流**：成功 ``return ToolOutcome.success(...)``；失败把 helper
        返回的失败 outcome 直接 return 上来（``enforce_access`` / ``coerce_int`` /
        ``require_group_scope`` / ``get_bot`` / ``call_action`` 都**返回**失败、不
        raise）。本方法只做归一 + 兜底，异常都不越出工具边界：
          - ``ToolOutcome`` → 原样；``dict`` / 其它标量 → success（兼容轻量返回）；
          - ``execute`` 里若冒出**预料外**第三方异常（httpx / sqlalchemy / napcat
            适配器 raise 的）→ 收敛成 ``internal_tool_error``（并记 exception 日志）。
        调用方（ToolWorker / 测试）拿到的永远是一个 ToolOutcome，不需要 try/except、
        也不需要认得任何异常类型。
        """
        try:
            result = await self.execute(arguments, **context)
        except Exception as exc:  # noqa: BLE001 —— 仅兜底预料外的第三方异常
            logger.exception(
                "[tool {}] unexpected error: {}",
                getattr(self, "name", "?"),
                exc,
            )
            return ToolOutcome.failure(
                "internal_tool_error", f"{type(exc).__name__}: {exc}"
            )
        if isinstance(result, ToolOutcome):
            return result
        if isinstance(result, dict):
            return ToolOutcome.success(result)
        if result is None:
            return ToolOutcome.success({})
        return ToolOutcome.success({"value": result})

    async def execute(self, arguments: dict, **context: Any) -> Any:
        """子类实现：跑工具逻辑，**返回** ToolOutcome（成功或失败），不 raise。
        典型骨架（scope/tier/bot 角色/参数校验逐个 return failure）::

            if fail := await self.enforce_access(context):
                return fail
            group_id, fail = require_group_scope(context, self.name)
            if fail:
                return fail
            ...
            return ToolOutcome.success(...)
        """
        raise NotImplementedError

    async def enforce_access(self, context: dict) -> "ToolOutcome | None":
        """工具访问总闸：scope + 发起人 tier + bot 自身角色。任一不过**返回**对应
        失败 ``ToolOutcome``；全过返回 None。工具 execute() 第一行::

            if fail := await self.enforce_access(context):
                return fail

        ``enforce_permission``（发起人 tier）与 ``enforce_bot_admin``（bot 自身角色）
        都可能现场向 napcat **实时**查群角色，故都是 async；``enforce_scope`` 是纯比较。
        ``or`` 短路：前者失败即返回。
        """
        return (
            self.enforce_scope(context)
            or await self.enforce_permission(context)
            or await self.enforce_bot_admin(context)
        )

    async def enforce_permission(self, context: dict) -> "ToolOutcome | None":
        """发起人 tier 判定（**解析也在工具内**）：tier 不足**返回**
        ``permission_denied_user_tier`` 失败；否则 None。

        tier 来源两条：① context 预置 ``triggered_by_user_tier``（测试/预解析）；
        ② 否则据 ``triggered_by_event_id`` 拿到发起人 user_id，再**实时**向 napcat
        查其在当前群的**当前**角色（``get_group_member_info``, no_cache）→ tier
        （生产路径，loop 不再代解析）。GUEST 工具在解析前就放行（省一次 IO）。
        """
        required = get_tool_required_permission(self)
        if required <= PermissionTier.GUEST:
            return None
        user_tier = await self._resolve_triggering_tier(context)
        if user_tier < required:
            return ToolOutcome.failure(
                "permission_denied_user_tier",
                f"{getattr(self, 'name', '?')} requires {required.name}; "
                f"triggering user tier is {user_tier.name}",
                required_tier=required.name,
                actual_tier=user_tier.name,
            )
        return None

    async def _resolve_triggering_tier(self, context: dict) -> PermissionTier:
        """解析触发用户 tier（**实时**，非事件快照）：
          ① context 预置 ``triggered_by_user_tier``（测试/预解析）→ 直接用，免 IO；
          ② 读触发事件拿 user_id + SUPERUSERS 判定——命中 SUPERUSERS 即
             SYSTEM_ADMIN（cross-cutting，越过群角色，不查群）；
          ③ 否则**实时**向 napcat 查该 user 在当前群的**当前**角色
             （``get_group_member_info``, no_cache=True）→ ``tier_from_group_role``。

        无 ``session_factory`` / 无 user / 无群 / 查不到 / 无 bot → GUEST（保守拒绝）。
        注：``resolve_user_tier_from_event`` 返回的 snap_tier 只在 SUPERUSERS 命中时
        为 SYSTEM_ADMIN（群角色最高只到 OWNER），据此识别 SU；其快照的群角色被丢弃、
        改用 ③ 的实时值。
        """
        raw = context.get("triggered_by_user_tier")
        if isinstance(raw, str) and raw:
            try:
                return PermissionTier[raw]
            except KeyError:
                return PermissionTier.GUEST
        session_factory = context.get("session_factory")
        if session_factory is None:
            return PermissionTier.GUEST
        snap_tier, user_id = await resolve_user_tier_from_event(
            context.get("triggered_by_event_id"),
            session_factory=session_factory,
            superusers=context.get("superusers"),
        )
        if snap_tier == PermissionTier.SYSTEM_ADMIN:
            return snap_tier  # SUPERUSERS：越过群角色，无需实时查
        if not user_id:
            return PermissionTier.GUEST
        group_id = _group_id_from_scope_key(context.get("scope_key"))
        if group_id is None:
            return PermissionTier.GUEST
        role = await self._fetch_live_member_role(group_id, user_id)
        return tier_from_group_role(role)

    async def _fetch_live_member_role(
        self, group_id: int, user_id: str
    ) -> str | None:
        """实时查该 user 在群里的**当前**角色（owner/admin/member）。

        经 bot_registry 取 Bot、调 ``get_group_member_info(no_cache=True)`` 强制取
        最新值（不吃缓存）。无 bot / napcat 报错（如该用户已退群）/ 无 role 字段 →
        None（上层据此保守判 GUEST）。延迟 import bot_registry 避免潜在循环。
        """
        from qqbot.services.agent_loop import bot_registry

        bot = bot_registry.get_any()
        if bot is None:
            return None
        try:
            info = await bot.get_group_member_info(
                group_id=int(group_id), user_id=int(user_id), no_cache=True
            )
        except Exception:  # noqa: BLE001 —— 查不到/napcat 错都保守当无角色
            return None
        return info.get("role") if isinstance(info, dict) else None

    async def enforce_bot_admin(self, context: dict) -> "ToolOutcome | None":
        """bot 自身群角色判定：角色不够**返回** ``permission_denied_bot_role``
        失败；否则 None。``required_bot_role``=None 放行；"admin" 要求 admin/owner；
        "owner" 要求 owner。

        bot 角色经 ``_effective_bot_role`` **实时**向 napcat 查其当前群角色（与发起人
        tier 同源，no_cache），查不到才回退 ToolWorker 透传的 ``context.bot_role``
        快照——这样 bot 刚被升/降权也能立刻反映，不再受投影层 sweep 时延影响。未知
        （实时+快照都拿不到）保守拒绝。
        """
        need = get_tool_required_bot_role(self)
        if need is None:
            return None
        bot_role = await self._effective_bot_role(context)
        ok = bot_role == "owner" or (need == "admin" and bot_role == "admin")
        if not ok:
            return ToolOutcome.failure(
                "permission_denied_bot_role",
                f"{getattr(self, 'name', '?')} requires the bot itself to be "
                f"group {need}; current bot_role={bot_role or 'unknown'}",
                required_bot_role=need,
                actual_bot_role=bot_role or None,
            )
        return None

    async def _effective_bot_role(self, context: dict) -> str | None:
        """bot 自身在当前群的**当前**角色（owner/admin/member）——工具判 bot 权限时
        统一走它。**实时**向 napcat 查（与发起人 tier 解析同源），查不到才回退
        ``context.bot_role`` 快照；结果缓存进 context，一次 ``execute()`` 内
        ``enforce_bot_admin`` 与细粒度层级判定（kick/ban/recall...）复用同一个值、
        不重复打 napcat。None = 实时与快照都拿不到（上层保守按无角色处理）。
        """
        cached = context.get("_effective_bot_role", _UNSET)
        if cached is not _UNSET:
            return cached
        resolved = await self._resolve_live_bot_role(context)
        context["_effective_bot_role"] = resolved
        return resolved

    async def _resolve_live_bot_role(self, context: dict) -> str | None:
        """实时解析 bot 自身群角色：取本 bot 的 self_id + scope 的 group_id，调
        ``_fetch_live_member_role``（``get_group_member_info`` no_cache）。查不到 / 无
        bot / 非群 scope → 回退注入的 ``context.bot_role`` 快照（好过凭空拒绝）。
        """
        from qqbot.services.agent_loop import bot_registry

        snap = context.get("bot_role")
        snap = snap.strip().lower() if isinstance(snap, str) and snap.strip() else None
        bot = bot_registry.get_any()
        self_id = getattr(bot, "self_id", None) if bot is not None else None
        group_id = _group_id_from_scope_key(context.get("scope_key"))
        if self_id is None or group_id is None:
            return snap
        role = await self._fetch_live_member_role(group_id, str(self_id))
        if isinstance(role, str) and role.strip():
            return role.strip().lower()
        return snap

    def enforce_scope(self, context: dict) -> "ToolOutcome | None":
        """scope 闸门：``allowed_scopes`` 限定的工具在别的 scope 被（硬）调用时
        **返回** ``tool_unavailable_in_scope`` 失败；否则 None。

        AgentLoop 不做 scope 判定（契约 §2.2 下放工具）——只按 catalog(scope) 隐藏
        LLM 看不到的工具；真要硬调由这里在工具内拦下。``allowed_scopes=None`` 不限。
        """
        allowed = get_tool_allowed_scopes(self)
        if allowed is None:
            return None
        scope_key = context.get("scope_key")
        current = (
            scope_key.split(":", 1)[0]
            if isinstance(scope_key, str) and scope_key
            else None
        )
        if current not in allowed:
            return ToolOutcome.failure(
                "tool_unavailable_in_scope",
                f"{getattr(self, 'name', '?')} is only available in scope(s) "
                f"{list(allowed)}; current scope={current!r}",
                allowed_scopes=list(allowed),
                actual_scope=current,
            )
        return None


def _group_id_from_scope_key(scope_key: Any) -> int | None:
    """从 ``group:<id>`` 形态的 scope_key 取 group_id；非群 scope / 非法 → None。
    只在实时解析发起人群角色时用（enforce_permission），避免额外 import event_writer。
    """
    if not isinstance(scope_key, str) or not scope_key.startswith("group:"):
        return None
    try:
        return int(scope_key.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def get_tool_required_permission(tool: Any) -> PermissionTier:
    """统一读 tool.required_permission 的兜底入口。

    缺失 → GUEST；字符串值（"ADMIN" / "admin"）按 enum name 解析；其它非法
    值 fall back 到 GUEST。集中实现避免每个 caller 都写重复 isinstance。
    """
    raw = getattr(tool, "required_permission", None)
    if raw is None:
        return PermissionTier.GUEST
    if isinstance(raw, PermissionTier):
        return raw
    if isinstance(raw, str):
        try:
            return PermissionTier[raw.strip().upper()]
        except KeyError:
            return PermissionTier.GUEST
    if isinstance(raw, int):
        try:
            return PermissionTier(raw)
        except ValueError:
            return PermissionTier.GUEST
    return PermissionTier.GUEST


def get_tool_require_bot_admin(tool: Any) -> bool:
    """统一读 tool.require_bot_admin 的兜底入口。缺失 → False。"""
    return bool(getattr(tool, "require_bot_admin", False))


def get_tool_required_bot_role(tool: Any) -> str | None:
    """统一读 tool 要求的 bot 最低群角色。优先显式 `required_bot_role`
    （"admin"/"owner"）；回退到旧的 `require_bot_admin=True` → "admin"；
    都没有 → None（不限）。"""
    raw = getattr(tool, "required_bot_role", None)
    if isinstance(raw, str) and raw.strip().lower() in ("admin", "owner"):
        return raw.strip().lower()
    if get_tool_require_bot_admin(tool):
        return "admin"
    return None


def get_tool_allowed_scopes(tool: Any) -> tuple[str, ...] | None:
    """统一读 tool.allowed_scopes 的兜底入口。

    返回 None = 不限 scope（缺失、显式 None、或解析失败都按"不限"处理，
    保守地保证老工具/stub 全 scope 可见）。返回非空 tuple = 仅这些 scope
    可见可调（"system"/"group"/"private"）。字符串单值自动包成单元素 tuple。
    """
    raw = getattr(tool, "allowed_scopes", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw,)
    try:
        scopes = tuple(str(s) for s in raw)
    except TypeError:
        return None
    return scopes or None


def _tool_visible_in_scope(tool: Any, scope: str | None) -> bool:
    """catalog / usage_docs 的 per-scope 过滤判据。

    scope=None（调用方没传）→ 不过滤，全部可见（向后兼容旧调用）。
    工具 allowed_scopes=None → 不限，任何 scope 可见。
    否则仅当 scope 在白名单内才可见。
    """
    if scope is None:
        return True
    allowed = get_tool_allowed_scopes(tool)
    if allowed is None:
        return True
    return scope in allowed


class ToolRegistry:
    """简单的 name → Tool 字典。后续如需作用域/scope 隔离再扩。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not name or not isinstance(name, str):
            raise ValueError("tool.name must be a non-empty string")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def catalog(self, scope: str | None = None) -> list[dict]:
        """渲染给 LLM 的工具清单。LLMPlanner 把这个塞进 prompt。

        权限元数据（required_permission / required_bot_role）随每个条目透出，
        **不在这里做权限可见性过滤** —— LLM 始终看见自己 scope 内的全部工具，
        硬调权限失败由**工具内** enforce_access 返回 permission_denied_*，让 LLM
        能感知 "我没权限" 然后礼貌回复（AgentLoop 不再做权限闸门）。

        但 **scope 可见性确实在这里过滤**（与权限不同维度）：传入当前
        AgentLoop 的 scope（"system"/"group"/"private"）时，`allowed_scopes`
        限定的工具只在白名单 scope 出现。这是契约 §2.2「工具集合按 scope 不同」
        的落地——群专用工具（ban / respond_to_group_join_request …）不出现在
        SystemAgentLoop 的 catalog 里，LLM 不知道它存在。scope=None（默认）时
        不过滤，兼容旧调用。
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "arguments_schema": t.arguments_schema,
                "required_permission": get_tool_required_permission(t).name,
                "require_bot_admin": get_tool_require_bot_admin(t),
                "required_bot_role": get_tool_required_bot_role(t),
            }
            for t in self._tools.values()
            if _tool_visible_in_scope(t, scope)
        ]

    def usage_docs(self, scope: str | None = None) -> str:
        """汇总已注册工具的 usage_prompt，PromptRegistry 在 system prompt 里
        作为一段注入。空 usage_prompt 的工具静默跳过 —— 不会出现孤儿
        `## Tool: foo` 标题。

        与 catalog() 对称地支持 per-scope 过滤：scope 给定时，allowed_scopes
        限定的工具的用法文档不进别的 scope 的 prompt（群专用工具的用法不泄漏进
        system loop 的 prompt，反之亦然）。**生产路径已带
        scope**：LLMPlanner 把本方法作为 tools_usage section 注入，PromptRegistry
        .render(scope=...) 在每个 tick 按 `context.scope_key` 的 scope 前缀求值
        （与 catalog(scope) 同一把尺子），所以群/‌system 专用工具的用法不再互相
        泄漏。scope=None（默认，旧调用 / 单测）= 不过滤，全部可见。
        """
        sections: list[str] = []
        for name in self.names():
            tool = self._tools[name]
            if not _tool_visible_in_scope(tool, scope):
                continue
            usage = getattr(tool, "usage_prompt", "") or ""
            usage = str(usage).strip()
            if not usage:
                continue
            sections.append(f"## Tool: {name}\n\n{usage}")
        return "\n\n".join(sections)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools
