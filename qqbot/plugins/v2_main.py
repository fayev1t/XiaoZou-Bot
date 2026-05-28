"""V2 主 plugin —— napcat 事件入口 + AgentLoop 生命周期。

职责（v1 已删除，v2 是唯一路径）：
  1. on_message / on_notice / on_request / on_metaevent 四个通道接收 nonebot
     事件，每次调 bot_registry.register(bot) 让 ReplySendWorker / ToolWorker
     能反查到对应 bot 实例
  2. 把事件交给 EventIngest 走 mapper → 媒体副作用 → 入库 → 唤醒 supervisor
     的完整流水线（meta_event.heartbeat 走文件旁路，详见 EventIngest契约 §7）
  3. 启动期：拉起 LoopSupervisor（含 SystemAgentLoop + ReplySendWorker +
     ToolWorker）；关停期：优雅停止
  4. 任何 handler 异常一律 swallow —— 单条事件失败不能让 napcat 重推爆炸

priority 取较低值（10）以避免与可能存在的调试 plugin 抢先；block=True
确保事件不再被任何残留 matcher 处理。

契约：开发文档/v2.0/事件系统设计.md, EventIngest契约.md, 任务与决策契约.md
"""

from __future__ import annotations

from pathlib import Path

from nonebot import get_driver, on_message, on_metaevent, on_notice, on_request
from nonebot.adapters import Bot, Event

from qqbot.core.database import AsyncSessionLocal
from qqbot.core.logging import get_logger
from qqbot.services.agent_loop import (
    LLMPlanner,
    LoopSupervisor,
    Projector,
    bot_registry,
)
from qqbot.services.agent_loop.tools import build_default_registry as build_tool_registry
from qqbot.services.event_ingest import EventIngest
from qqbot.services.event_ingest.mappers import build_default_registry

logger = get_logger(__name__)

_ingest: EventIngest | None = None
_supervisor: LoopSupervisor | None = None

# qqbot/ 没有 __init__.py（namespace package），所以 `qqbot.__file__` 是
# None，不能用来定位 persona。改用本文件的相对路径：
#   qqbot/plugins/v2_main.py → parent = qqbot/plugins → parent = qqbot/
_PERSONA_PATH = Path(__file__).resolve().parent.parent / "persona.md"


def _load_persona_text() -> str | None:
    """从 qqbot/persona.md 读主人设。文件缺失 / 为空返回 None，
    LLMPlanner 会退化为纯决策协议 prompt（不报错，便于排障）。"""
    try:
        if not _PERSONA_PATH.exists():
            logger.warning(
                "[v2_main] persona.md not found at {}, running without persona",
                _PERSONA_PATH,
            )
            return None
        text = _PERSONA_PATH.read_text(encoding="utf-8").strip()
        if not text:
            logger.warning("[v2_main] persona.md is empty, running without persona")
            return None
        return text
    except Exception as exc:
        logger.warning(
            "[v2_main] failed to read persona.md ({}), running without persona",
            exc,
        )
        return None


def _get_supervisor() -> LoopSupervisor:
    global _supervisor
    if _supervisor is None:
        projector = Projector(session_factory=AsyncSessionLocal)
        tool_registry = build_tool_registry(session_factory=AsyncSessionLocal)
        persona_text = _load_persona_text()
        # LLMPlanner 对 LLM 不可用 / JSON 解析失败一律 fallback 为 IdleAction，
        # 不会让 supervisor 起不来。tool_registry 同时给 planner（prompt
        # tool_catalog）和 supervisor（ToolWorker 调度查找）共用。
        # persona_text 进入 SystemPrompt 最前段，立人设；缺文件时退化为
        # 纯决策协议 prompt（LLMPlanner 自己处理 None）。
        _supervisor = LoopSupervisor(
            planner=LLMPlanner(
                tool_registry=tool_registry,
                persona_text=persona_text,
            ),
            session_factory=AsyncSessionLocal,
            projector=projector,
            tool_registry=tool_registry,
        )
    return _supervisor


def _get_ingest() -> EventIngest:
    global _ingest
    if _ingest is None:
        _ingest = EventIngest(
            registry=build_default_registry(),
            session_factory=AsyncSessionLocal,
            supervisor=_get_supervisor(),
        )
    return _ingest


async def _ingest_event(event: Event) -> None:
    try:
        result = await _get_ingest().ingest(event)
        if result.status == "error":
            logger.warning("[v2_main] persist error: {}", result.reason)
        elif result.status == "unknown":
            logger.debug(
                "[v2_main] no mapper: post_type={} sub_type={}",
                getattr(event, "post_type", "?"),
                getattr(event, "sub_type", "?"),
            )
    except Exception as exc:
        logger.warning("[v2_main] ingest swallowed: {}", exc)


def _remember_bot(bot: Bot) -> None:
    """每个 handler 入口缓存 bot；供 ReplySendWorker / ToolWorker 反查。"""
    try:
        bot_registry.register(bot)
    except Exception as exc:
        logger.warning("[v2_main] bot_registry.register swallowed: {}", exc)


# v2 是唯一消费者：block=True 防止任何残留 matcher 再处理同一事件
# (priority=10 留出余地给调试 plugin 用 priority<10 抢在前面)。
_message_matcher = on_message(priority=10, block=True)
_notice_matcher = on_notice(priority=10, block=True)
_request_matcher = on_request(priority=10, block=True)
# meta_event：heartbeat → 文件旁路（无 DB 写）；lifecycle → 正常 mapper 入库。
# 分支在 EventIngest.ingest() 内部处理（EventIngest契约.md §7）。
_meta_matcher = on_metaevent(priority=10, block=True)


@_message_matcher.handle()
async def _on_message(bot: Bot, event: Event) -> None:
    _remember_bot(bot)
    await _ingest_event(event)


@_notice_matcher.handle()
async def _on_notice(bot: Bot, event: Event) -> None:
    _remember_bot(bot)
    await _ingest_event(event)


@_request_matcher.handle()
async def _on_request(bot: Bot, event: Event) -> None:
    _remember_bot(bot)
    await _ingest_event(event)


@_meta_matcher.handle()
async def _on_meta(bot: Bot, event: Event) -> None:
    _remember_bot(bot)
    await _ingest_event(event)


_driver = get_driver()


@_driver.on_startup
async def _start_v2_loop() -> None:
    """init_db 之后启动 LoopSupervisor。startup.py 的 on_startup 注册在前，
    所以这条 hook 跑时 DB 已经就绪。"""
    try:
        await _get_supervisor().start()
    except Exception as exc:
        logger.exception("[v2_main] supervisor.start failed: {}", exc)


@_driver.on_shutdown
async def _stop_v2_loop() -> None:
    sup = _supervisor
    if sup is None:
        return
    try:
        await sup.stop()
    except Exception as exc:
        logger.warning("[v2_main] supervisor.stop failed: {}", exc)


logger.info("[v2_main] plugin loaded; AgentLoop will start on driver startup")
