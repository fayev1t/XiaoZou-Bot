"""Permission tier system for tool gating.

四档 tier（IntEnum，可直接 ``<`` 比较）：

    GUEST(10)        — 普通群员（含未识别 / 未填 triggered_by 的兜底）。
    ADMIN(20)        — 群管理员（OneBot sender.role == "admin"）。
    OWNER(30)        — 群主 (sender.role == "owner")。
    SYSTEM_ADMIN(40) — 配置文件 SUPERUSERS 列出的 QQ；越过群角色任何工具。

设计要点：

1. **不存"每个用户的 tier"**。tier 是从事件流 + 配置即时算出的派生值——
   群角色直接读消息事件 payload.sender.role，SUPERUSERS 读 env 配置；不引入
   mutable 状态表，与 v2 单表 append-only 架构一致。

2. **缺 triggered_by → GUEST**。LLM 调敏感工具但漏填 triggered_by_event_id
   时，tier 视作 GUEST，敏感工具自然失败，迫使 LLM 给出明确 attribution。

3. **私聊不跑 loop**（supervisor 不实例化 private 作用域的 AgentLoop），所以
   这里也不处理 scope_key 以 ``private:`` 开头的情况——任何调用都默认是
   group / system 来源。
"""

from __future__ import annotations

import enum
import json
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.settings import get_env_value
from qqbot.models.agent_event import AgentEvent

SessionFactory = Callable[[], AsyncSession]


class PermissionTier(enum.IntEnum):
    """递增的权限档位。

    用 IntEnum 是为了 ``tool.required_permission <= user_tier`` 这种自然比较
    一步到位；name / value 同时可用于事件 payload 序列化。
    """

    GUEST = 10
    ADMIN = 20
    OWNER = 30
    SYSTEM_ADMIN = 40


_GROUP_ROLE_TO_TIER: dict[str, PermissionTier] = {
    "owner": PermissionTier.OWNER,
    "admin": PermissionTier.ADMIN,
    "member": PermissionTier.GUEST,
}


def load_superusers() -> frozenset[str]:
    """从 SUPERUSERS env 解析为字符串集合。

    env 值是 nonebot 风格的 JSON list（``SUPERUSERS=["123","456"]``）。
    解析失败 / 空值 → 空集合，不抛——线上配错也只是没有 SU，不应让进程崩。
    返回值是 ``frozenset[str]``，调用方拿 user_id (int|str) 时统一 ``str()``
    再比较。
    """
    raw = get_env_value("SUPERUSERS")
    if not raw:
        return frozenset()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return frozenset()
    if not isinstance(parsed, list):
        return frozenset()
    return frozenset(str(item).strip() for item in parsed if str(item).strip())


def tier_from_group_role(role: str | None) -> PermissionTier:
    """OneBot sender.role 字符串 → tier。

    未知值（None / "" / 任何不在 _GROUP_ROLE_TO_TIER 的字符串）一律视为 GUEST。
    """
    if not role:
        return PermissionTier.GUEST
    return _GROUP_ROLE_TO_TIER.get(role.strip().lower(), PermissionTier.GUEST)


async def resolve_user_tier_from_event(
    event_id: str | None,
    *,
    session_factory: SessionFactory,
    superusers: frozenset[str] | None = None,
) -> tuple[PermissionTier, str | None]:
    """根据触发事件解析用户 tier，返回 ``(tier, user_id)``。

    - ``event_id`` 为 None / 数据库找不到 → ``(GUEST, None)``。
    - ``event.user_id`` 命中 SUPERUSERS → ``(SYSTEM_ADMIN, user_id)``，
      不再看群角色（SU 是 cross-cutting 身份）。
    - 否则按 ``event.payload.sender.role`` 映射到群角色 tier。

    ``superusers`` 不传则现读 env；测试场景可以注入 ``frozenset()`` 跳过 SU。
    """
    if superusers is None:
        superusers = load_superusers()
    if not event_id:
        return PermissionTier.GUEST, None

    async with session_factory() as session:
        stmt = select(AgentEvent).where(AgentEvent.event_id == event_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()

    if row is None:
        return PermissionTier.GUEST, None

    user_id_value: Any = row.user_id
    user_id_str: str | None = str(user_id_value) if user_id_value is not None else None

    if user_id_str and user_id_str in superusers:
        return PermissionTier.SYSTEM_ADMIN, user_id_str

    payload: dict = row.payload or {}
    sender = payload.get("sender") or {}
    role = sender.get("role") if isinstance(sender, dict) else None
    return tier_from_group_role(role), user_id_str
