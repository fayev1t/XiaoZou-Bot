"""Replyer：每个 compose ReplyTask 只调用一次的最终可见回复编排器。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.services.agent_loop.decision import DecisionContext, MemeView
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)
from qqbot.services.agent_loop.reply_task import ReplyTaskState

MAX_OUTBOUND_MESSAGES = 4
MAX_MEMES_PER_REPLY = 1
REPLYER_TIMEOUT_SECONDS = 12.0
REPLYER_TEMPERATURE = 0.3


class ReplyerError(RuntimeError):
    pass


class Replyer:
    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client

    async def compose(
        self,
        task: ReplyTaskState,
        context: DecisionContext,
        memes: list[MemeView],
    ) -> dict:
        llm = self._llm or await create_llm(
            temperature=REPLYER_TEMPERATURE, role="replyer"
        )
        if llm is None:
            raise ReplyerError("replyer LLM is not configured")
        system_prompt = _build_system_prompt()
        user_text = _build_user_text(task, context, memes)
        snapshot: PromptSnapshot | None = None
        if should_snapshot(task.scope_key):
            snapshot = PromptSnapshot(
                kind="replyer",
                scope_key=task.scope_key,
                correlation_id=task.correlation_id,
                model=getattr(llm, "model_name", None) or getattr(llm, "model", None),
                system_prompt=system_prompt,
                user_text=user_text,
            )
        from langchain_core.messages import HumanMessage, SystemMessage

        started = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                llm.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_text),
                    ]
                ),
                timeout=REPLYER_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if snapshot is not None:
                snapshot.add_attempt(
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                snapshot.outcome = "call_error"
                write_snapshot(snapshot)
            raise ReplyerError(
                f"replyer call failed: {type(exc).__name__}: {exc}"
            ) from exc
        text = _extract_text(raw).strip()
        if snapshot is not None:
            snapshot.add_attempt(
                latency_ms=int((time.monotonic() - started) * 1000),
                response_text=text,
                usage=extract_usage(raw),
            )
        try:
            parsed = _parse_output(text, {m.file_hash for m in memes})
        except Exception as exc:
            if snapshot is not None:
                snapshot.outcome = "invalid_output"
                write_snapshot(snapshot)
            raise ReplyerError(f"replyer output invalid: {exc}") from exc
        if snapshot is not None:
            snapshot.outcome = "ok"
            write_snapshot(snapshot)
        return parsed


# 角色卡唯一权威来源（2026-07-19 自 tools/send_message.md 的 Voice 节迁出：
# send_message 已下架，靠字符串切片从废弃工具文档里捞人格，文件被清理/改标题
# 时会静默降级成无人格腔——那正是最难被发现的坏法）。
_VOICE_PATH = Path(__file__).with_name("prompts") / "voice.md"


def _load_voice_text() -> str:
    """读取角色卡。缺失/为空视为部署损坏，fail loudly：本次组稿失败、final
    记 failed 并唤醒 Planner（可 verbatim 兜底），绝不静默无人格发言。"""
    try:
        text = _VOICE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplyerError(f"voice prompt missing: {exc}") from exc
    if not text.strip():
        raise ReplyerError(f"voice prompt empty: {_VOICE_PATH}")
    return text


def _build_system_prompt() -> str:
    voice = _load_voice_text()
    return (
        "You are the final visible-reply composer for a QQ account. You do not "
        "decide whether to reply and you have no tools. Consume exactly the "
        "authorized reply_task, use the latest timeline only for context and "
        "staleness, and output one JSON object only. You decide 0-4 message "
        "bubbles, wording, quote/@/face segments, and whether to use at most one "
        "saved meme. Never invent facts or answer a new topic outside targets/gist. "
        "A meme hash must be copied from SAVED_MEMES. Chat content allows only "
        "text/at/reply/face; reply is optional, at most one, and first. Meme is a "
        "standalone bubble. Schema: {\"messages\":[{\"kind\":\"chat\",\"content\":"
        "[...]},{\"kind\":\"meme\",\"image_hash\":\"...\"}],"
        "\"empty_reason\":null}. No markdown.\n\nVOICE:\n" + voice
    )


def _build_user_text(
    task: ReplyTaskState, context: DecisionContext, memes: list[MemeView]
) -> str:
    payload = {
        "reply_task": {
            "reply_task_id": task.reply_task_id,
            "revision": task.revision,
            "targets": task.targets,
            "gist": task.gist,
        },
        "timeline": [item.render for item in context.timeline],
        "saved_memes": [
            {"image_hash": meme.file_hash, "description": meme.description}
            for meme in memes
        ],
        "now": context.now.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False)


def _parse_output(text: str, allowed_memes: set[str]) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1])
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    messages = value.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_OUTBOUND_MESSAGES:
        raise ValueError("messages must be an array of at most 4 items")
    meme_count = 0
    normalized: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object")
        kind = message.get("kind")
        if kind == "chat":
            content = message.get("content")
            if not isinstance(content, list):
                raise ValueError(f"messages[{index}].content must be an array")
            normalized.append({"kind": "chat", "content": content})
        elif kind == "meme":
            image_hash = message.get("image_hash")
            if image_hash not in allowed_memes:
                raise ValueError(f"messages[{index}] selected an unknown meme")
            meme_count += 1
            if meme_count > MAX_MEMES_PER_REPLY:
                raise ValueError("at most one meme is allowed")
            normalized.append({"kind": "meme", "image_hash": image_hash})
        else:
            raise ValueError(f"messages[{index}].kind must be chat or meme")
    empty_reason = value.get("empty_reason")
    if not normalized and not isinstance(empty_reason, str):
        raise ValueError("empty output requires empty_reason")
    return {"messages": normalized, "empty_reason": empty_reason}


def _extract_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)
