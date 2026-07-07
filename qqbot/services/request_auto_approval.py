"""好友申请 / 邀请入群的自动审批（不走 LLM 决策）。

处理链路拆分（2026-07-03）：``request.friend`` 与 ``request.group(invite)`` 默认
自动同意——事件先由 EventIngest 以 runtime_only 落库留审计，随后本模块直接调
OneBot / NapCat 回执，再补一条 ``runtime.request_auto_handled`` 内部事件闭环。
入群申请（``request.group`` + ``sub_type=add``）不在此处理：它进目标群 timeline，
由该群的 GroupAgentLoop 在管理员明确授权后经 respond_to_group_join_request
工具回执。

调用方是 plugin 层（v2_main 的 request handler，手里正有 bot 实例）。刻意不放
EventIngest 内部：ingest 是"外部事件 → 落库"的纯入站管线，不应反向持有出站
能力（bot / 回执 API）。

幂等性：只在 ``IngestResult.status == "inserted"`` 时动作。napcat 重推同一申请
（同 flag）会被 idempotency_key 挡成 duplicate，不会二次审批；进程重启也不会
重放（事件已在库）。

失败策略（从简，不重试）：napcat 调用失败（bot 掉线、flag 过期等）只记日志 +
审计事件带 ``ok=false`` / ``error``。申请会滞留在 QQ 待处理列表，申请人重新
申请会生成新 flag 新事件、再走一遍。

审计事件 ``runtime.request_auto_handled``：origin=runtime（基础设施策略，非
agent 决策）、scope=system、runtime_only；correlation_id 沿用申请事件（外部
事件自相关）、causation_id=申请事件 event_id——因果链不断。
"""

from __future__ import annotations

from typing import Any, Callable

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import write_internal_event
from qqbot.services.event_ingest import IngestResult, SystemEvent

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]

# 只有这两类自动同意；external.request.group.add 走群内 LLM 授权链路，不在此列。
AUTO_APPROVE_TYPES = frozenset(
    {
        "external.request.friend",
        "external.request.group.invite",
    }
)


async def maybe_auto_approve(
    bot: Any,
    result: IngestResult | None,
    session_factory: SessionFactory,
) -> bool:
    """对刚入库的好友申请 / 邀请入群自动同意；其余输入一律无动作。

    返回是否成功回执 napcat。**永不 raise**：napcat / DB 失败都折成日志 +
    审计事件字段，调用方（nonebot handler）无需再包异常。
    """
    if result is None or result.status != "inserted":
        return False
    event = result.event
    if event is None or event.type not in AUTO_APPROVE_TYPES:
        return False

    flag = str(event.payload.get("flag") or "")
    ok = False
    error: str | None = None
    if not flag:
        error = "request event has no flag; cannot respond"
    elif bot is None:
        error = "no bot available"
    else:
        try:
            if event.type == "external.request.friend":
                await bot.set_friend_add_request(flag=flag, approve=True)
            else:
                await bot.set_group_add_request(
                    flag=flag, sub_type="invite", approve=True
                )
            ok = True
        except Exception as exc:  # noqa: BLE001 —— 回执失败只留审计，不上抛不重试
            error = str(exc) or type(exc).__name__

    await _write_handled_event(session_factory, event, ok=ok, error=error)

    if ok:
        logger.info(
            "[request_auto_approval] approved {} event={} user={}",
            event.type,
            event.event_id,
            event.payload.get("user_id"),
        )
    else:
        logger.warning(
            "[request_auto_approval] approve failed {} event={} err={}",
            event.type,
            event.event_id,
            error,
        )
    return ok


async def _write_handled_event(
    session_factory: SessionFactory,
    request_event: SystemEvent,
    *,
    ok: bool,
    error: str | None,
) -> None:
    """补 ``runtime.request_auto_handled`` 审计事件；写库失败仅记日志。"""
    payload: dict[str, Any] = {
        "request_event_id": request_event.event_id,
        "request_type": (
            "friend"
            if request_event.type == "external.request.friend"
            else "group_invite"
        ),
        "user_id": request_event.payload.get("user_id"),
        "group_id": request_event.payload.get("group_id"),
        "approve": True,
        "ok": ok,
    }
    if error:
        payload["error"] = error
    try:
        await write_internal_event(
            session_factory,
            origin="runtime",
            event_type="runtime.request_auto_handled",
            scope_key="system",
            visibility="runtime_only",
            correlation_id=request_event.correlation_id or request_event.event_id,
            causation_id=request_event.event_id,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001 —— 审计写失败不影响已完成的回执
        logger.error(
            "[request_auto_approval] audit write failed for {}: {}",
            request_event.event_id,
            exc,
        )
