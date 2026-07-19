"""delivery_claims —— worker 投递去重的 claim-with-lease 实现。

对外接口：
- `try_claim`：只关心"有没有抢到"的轻量布尔接口
- `claim_delivery`：在抢不到时额外返回"建议多久后再试"，供 worker 给自己补
  一个 lease-expiry retry wake
- `try_claim_once_strict`：聊天发送专用的永久、fail-closed one-shot claim
- `has_delivery_claim`：启动恢复时查协调表，覆盖 claim/event 间崩溃缝隙

定位与语义见 models/agent_delivery_claim.py 顶部注释。

幂等 / 原子:用 Postgres `INSERT ... ON CONFLICT DO NOTHING` 抢首次锁(rowcount
决定胜负,多实例只有一个 insert 成功);若已存在 claim,再用条件 `UPDATE ...
WHERE lease_until < now` 抢过期租约(行级锁串行化,只有一个 UPDATE 命中)。

fail-open:本机制是 best-effort 去重,**绝不能因它阻断投递**(宁可偶发重复也不
丢消息)。try_claim 内部任何异常 → 记 warning 后返回 True(放行)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models.agent_delivery_claim import AgentDeliveryClaim

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

# 默认租约时长:够一次发送 + 写终态完成,又不至于在崩溃后让事件卡死太久。
DEFAULT_LEASE_SECONDS = 120


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    retry_after_seconds: float | None = None


async def claim_delivery(
    session_factory: SessionFactory,
    event_id: str,
    kind: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimResult:
    """抢占 ``event_id`` 的投递权,并在抢不到时返回建议重试时间。

    worker 用它处理两类静默卡死:
    - 别的实例/上次尝试仍持有未过期 lease → 当前轮跳过,但要在 lease 到期后
      自己 wake 一次重新扫描
    - terminal event 写失败后,下一轮重扫时会先看到未过期 lease,此时也靠这个
      retry_after_seconds 把"只等新 notify"改成"到期自唤醒"
    """
    return await _claim_once(
        session_factory,
        event_id,
        kind,
        lease_seconds=lease_seconds,
        include_retry_after=True,
    )


async def try_claim(
    session_factory: SessionFactory,
    event_id: str,
    kind: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """抢占 ``event_id`` 的投递权,返回是否抢到。

    True  → 本 worker 拥有该投递,继续执行外部副作用。
    False → 他人持有未过期租约(并发 / 上次尝试仍在租约内),本轮跳过;事件仍
            是 pending,租约到期后会被重新抢占重试(at-least-once 不破)。
    """
    result = await _claim_once(
        session_factory,
        event_id,
        kind,
        lease_seconds=lease_seconds,
        include_retry_after=False,
    )
    return result.claimed


async def try_claim_once_strict(
    session_factory: SessionFactory,
    event_id: str,
    kind: str,
) -> bool:
    """永久 one-shot claim，用于不可安全重试的聊天发送。

    与工具投递的 lease/fail-open 不同：冲突永不接管，数据库异常 fail-closed。
    NapCat 没有幂等键，宁可把中断批次标 uncertain，也不能在租约过期后复发。
    """
    now = china_now()
    lease_until = now + timedelta(days=36500)
    try:
        async with session_factory() as session:
            stmt = (
                pg_insert(AgentDeliveryClaim)
                .values(
                    event_id=event_id,
                    kind=kind,
                    claimed_at=now,
                    lease_until=lease_until,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            result = await session.execute(stmt)
            await session.commit()
            return (result.rowcount or 0) > 0
    except Exception as exc:
        logger.warning(
            "[delivery_claims] strict claim({}, {}) failed closed: {}",
            event_id,
            kind,
            exc,
        )
        return False


async def has_delivery_claim(
    session_factory: SessionFactory,
    event_id: str,
    kind: str,
) -> bool:
    """查询协调表里的既有 claim，覆盖 claim 已提交、事件尚未写入的崩溃缝隙。"""
    try:
        async with session_factory() as session:
            stmt = (
                select(AgentDeliveryClaim.event_id)
                .where(AgentDeliveryClaim.event_id == event_id)
                .where(AgentDeliveryClaim.kind == kind)
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None
    except Exception as exc:
        logger.warning(
            "[delivery_claims] inspect claim({}, {}) failed: {}",
            event_id,
            kind,
            exc,
        )
        return False


async def _claim_once(
    session_factory: SessionFactory,
    event_id: str,
    kind: str,
    *,
    lease_seconds: int,
    include_retry_after: bool,
) -> ClaimResult:
    now = china_now()
    lease_until = now + timedelta(seconds=lease_seconds)
    try:
        async with session_factory() as session:
            # 1) 首次抢锁:无既有 claim 时插入成功(rowcount>0)即拿下。
            ins = (
                pg_insert(AgentDeliveryClaim)
                .values(
                    event_id=event_id,
                    kind=kind,
                    claimed_at=now,
                    lease_until=lease_until,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
            res = await session.execute(ins)
            if (res.rowcount or 0) > 0:
                await session.commit()
                return ClaimResult(claimed=True)
            # 2) 已有 claim:仅当其租约已过期才抢占(条件 UPDATE,行级锁保证
            #    多实例只有一个命中)。命中 rowcount>0 即重新拿下。
            upd = (
                update(AgentDeliveryClaim)
                .where(AgentDeliveryClaim.event_id == event_id)
                .where(AgentDeliveryClaim.lease_until < now)
                .values(kind=kind, claimed_at=now, lease_until=lease_until)
            )
            res2 = await session.execute(upd)
            if (res2.rowcount or 0) > 0:
                await session.commit()
                return ClaimResult(claimed=True)

            retry_after_seconds: float | None = None
            if include_retry_after:
                lease_stmt = select(AgentDeliveryClaim.lease_until).where(
                    AgentDeliveryClaim.event_id == event_id
                )
                lease_res = await session.execute(lease_stmt)
                current_lease_until = lease_res.scalar_one_or_none()
                if current_lease_until is None:
                    retry_after_seconds = float(lease_seconds)
                else:
                    retry_after_seconds = max(
                        (
                            current_lease_until - china_now()
                        ).total_seconds(),
                        0.0,
                    )
            await session.commit()
            return ClaimResult(
                claimed=False,
                retry_after_seconds=retry_after_seconds,
            )
    except Exception as exc:
        # fail-open:去重失败绝不阻断投递(宁可偶发重复,不可丢消息)。
        logger.warning(
            "[delivery_claims] try_claim({}, {}) failed, proceeding without "
            "dedup: {}",
            event_id,
            kind,
            exc,
        )
        return ClaimResult(claimed=True)
