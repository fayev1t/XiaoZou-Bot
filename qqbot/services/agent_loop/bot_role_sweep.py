"""Bot 角色 baseline 扫描 + 反射写入。

两条入口：

1. ``sweep_bot_role(bot, session_factory)`` —— lifecycle.connect 触发的全量
   扫描。调 napcat ``get_group_list`` 列出 bot 所在群，再对每个群调
   ``get_group_member_info`` 取 bot 自己的 role，逐群写入一条
   ``runtime.bot_role_observed`` 事件。

2. ``observe_bot_role_change(...)`` —— 群管角色变更 / 入群 / 退群事件的反射
   写入。v2_main 的 notice 路由检测到目标用户是 bot 自己时调用，写一条增量
   ``runtime.bot_role_observed``。

写入的事件结构（payload）：
    {
      "role": "owner" | "admin" | "member",
      "self_id": "<bot.self_id as str>",
      "source": "lifecycle_sweep" | "group_admin_notice" | "group_member_change",
      "group_id": <int>,
    }

scope 一律 ``group:<id>``、visibility ``agent_visible``——Projector 在 fold
bot_role 时只看 ``runtime.bot_role_observed`` 类型，scope / visibility 仅供
索引和未来过滤。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import write_runtime_event

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

# 退群 / 被踢：napcat 没法再 query role，统一写 "member" 当作降级（保守地
# 撤销管理权限），事件 source 区分以便审计。
_DEFAULT_ROLE_ON_LEAVE = "member"


async def sweep_bot_role(bot: Any, session_factory: SessionFactory) -> int:
    """一次性扫描 bot 所在的所有群，写入 ``runtime.bot_role_observed`` baseline。

    返回成功写入的群数；网络 / API 异常时尽量继续（单群失败不阻塞其他群）。
    """
    self_id = str(getattr(bot, "self_id", "") or "")
    if not self_id:
        logger.warning("[bot_role_sweep] bot.self_id is empty, skipping")
        return 0

    try:
        groups = await bot.call_api("get_group_list")
    except Exception as exc:
        logger.warning("[bot_role_sweep] get_group_list failed: {}", exc)
        return 0

    if not isinstance(groups, list):
        logger.warning(
            "[bot_role_sweep] get_group_list returned non-list: {}", type(groups)
        )
        return 0

    written = 0
    for entry in groups:
        group_id = _extract_group_id(entry)
        if group_id is None:
            continue
        try:
            info = await bot.call_api(
                "get_group_member_info",
                group_id=group_id,
                user_id=int(self_id),
                no_cache=True,
            )
        except Exception as exc:
            logger.warning(
                "[bot_role_sweep] get_group_member_info({}) failed: {}",
                group_id,
                exc,
            )
            continue
        role = _normalize_role(info)
        if role is None:
            continue
        try:
            await _write_role_observed(
                session_factory,
                group_id=group_id,
                role=role,
                self_id=self_id,
                source="lifecycle_sweep",
            )
            written += 1
        except Exception as exc:
            logger.exception(
                "[bot_role_sweep] write {} failed: {}", group_id, exc
            )

    logger.info(
        "[bot_role_sweep] self_id={} swept {} groups (wrote {})",
        self_id,
        len(groups),
        written,
    )
    return written


async def reflect_bot_role_from_notice(
    bot: Any,
    event: Any,
    session_factory: SessionFactory,
) -> None:
    """对涉及 bot 自身的 notice 写一条 ``runtime.bot_role_observed``。

    被 v2_main 的 notice handler 调用；任何异常都吞掉，反射失败不应让主消息
    处理路径炸。三类触发：

      - notice_type=group_admin, user_id==self_id → role 由 sub_type 决定
        ("set" → admin, "unset" → member)
      - notice_type=group_increase, user_id==self_id → 新加入，默认 member
      - notice_type=group_decrease, user_id==self_id → 被踢/退群，写 member

    其它 notice_type 直接返回。
    """
    try:
        notice_type = getattr(event, "notice_type", None)
        if notice_type not in ("group_admin", "group_increase", "group_decrease"):
            return
        target_user_id = getattr(event, "user_id", None)
        self_id = getattr(bot, "self_id", None)
        if target_user_id is None or self_id is None:
            return
        if int(target_user_id) != int(self_id):
            return
        group_id = getattr(event, "group_id", None)
        if group_id is None:
            return
        if notice_type == "group_admin":
            sub = (getattr(event, "sub_type", "") or "").strip().lower()
            role = "admin" if sub == "set" else "member"
            source = "group_admin_notice"
        elif notice_type == "group_increase":
            role = _DEFAULT_ROLE_ON_LEAVE
            source = "group_increase_notice"
        else:  # group_decrease
            role = _DEFAULT_ROLE_ON_LEAVE
            source = "group_decrease_notice"
        await observe_bot_role_change(
            session_factory=session_factory,
            group_id=int(group_id),
            self_id=str(self_id),
            role=role,
            source=source,
        )
    except Exception as exc:
        logger.warning(
            "[bot_role_observe] reflect from notice swallowed: {}", exc
        )


def reflect_bot_role_from_meta(
    bot: Any,
    event: Any,
    session_factory: SessionFactory,
) -> None:
    """meta_event 的反射：lifecycle.connect → 全量 sweep。其它 sub_type 忽略。

    同步函数：内部 ``schedule_sweep`` 是 fire-and-forget create_task。
    异常吞掉，理由同 reflect_bot_role_from_notice。
    """
    try:
        meta_event_type = getattr(event, "meta_event_type", None)
        sub_type = (getattr(event, "sub_type", "") or "").strip().lower()
        if meta_event_type == "lifecycle" and sub_type == "connect":
            schedule_sweep(bot, session_factory)
    except Exception as exc:
        logger.warning(
            "[bot_role_observe] reflect from meta swallowed: {}", exc
        )


async def observe_bot_role_change(
    *,
    session_factory: SessionFactory,
    group_id: int,
    self_id: str,
    role: str,
    source: str,
) -> None:
    """反射写入：增量观测到的 bot 角色变化。

    调用方（v2_main notice handler）已经判定 target user_id == bot.self_id，
    本函数仅做事件写入；异常不抛出，仅日志，避免影响主消息处理路径。
    """
    try:
        await _write_role_observed(
            session_factory,
            group_id=group_id,
            role=role,
            self_id=self_id,
            source=source,
        )
        logger.info(
            "[bot_role_observe] {} group={} → {}",
            source,
            group_id,
            role,
        )
    except Exception as exc:
        logger.warning(
            "[bot_role_observe] write failed group={} role={}: {}",
            group_id,
            role,
            exc,
        )


def schedule_sweep(bot: Any, session_factory: SessionFactory) -> None:
    """同步入口：在 lifecycle.connect 路径上 fire-and-forget 触发 sweep。

    不阻塞 napcat 主循环，单个 sweep 自身允许 fail，所以错误吞掉就好。
    返回的 Task 不被外面引用——拿日志做唯一兜底。
    """
    try:
        asyncio.create_task(
            sweep_bot_role(bot, session_factory),
            name=f"bot_role_sweep:{getattr(bot, 'self_id', '?')}",
        )
    except RuntimeError:
        # 没在 event loop 里被调到（极少见，比如测试环境），降级为同步执行
        logger.warning("[bot_role_sweep] no running loop; sweep skipped")


# ─── helpers ───


def _extract_group_id(entry: Any) -> int | None:
    """napcat 的 get_group_list 返回 dict，包含 group_id；防御性兜底。"""
    if isinstance(entry, dict):
        gid = entry.get("group_id")
        if gid is None:
            return None
        try:
            return int(gid)
        except (TypeError, ValueError):
            return None
    return None


def _normalize_role(info: Any) -> str | None:
    """get_group_member_info 返回 dict，role 字段 ∈ {owner, admin, member}。"""
    if not isinstance(info, dict):
        return None
    role = info.get("role")
    if not isinstance(role, str):
        return None
    role = role.strip().lower()
    if role not in ("owner", "admin", "member"):
        return None
    return role


async def _write_role_observed(
    session_factory: SessionFactory,
    *,
    group_id: int,
    role: str,
    self_id: str,
    source: str,
) -> None:
    """统一封装写入逻辑。correlation_id 用新 event_id —— 角色观测自成一条因果
    链起点，不挂在其他 tick 上。"""
    cid = new_event_id()
    await write_runtime_event(
        session_factory,
        event_type="runtime.bot_role_observed",
        scope_key=f"group:{group_id}",
        visibility="agent_visible",
        correlation_id=cid,
        causation_id=None,
        payload={
            "role": role,
            "self_id": self_id,
            "source": source,
            "group_id": group_id,
        },
    )


__all__ = [
    "DEFAULT_ROLE_ON_LEAVE",
    "observe_bot_role_change",
    "reflect_bot_role_from_meta",
    "reflect_bot_role_from_notice",
    "schedule_sweep",
    "sweep_bot_role",
]

# 公开常量别名（模块外要写 "member" 时统一引用一次而非到处硬编码）
DEFAULT_ROLE_ON_LEAVE = _DEFAULT_ROLE_ON_LEAVE
