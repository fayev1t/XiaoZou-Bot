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
# 2026-07-22 随 Replyer 看图放宽：多模态载荷（timeline 图 base64）比纯文本
# 上传+推理更慢，多图拍 12s 易触顶。
REPLYER_TIMEOUT_SECONDS = 25.0
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
        image_blocks, image_meta = _timeline_image_blocks(context)
        # 无图时 content 保持纯字符串（与旧行为逐字节一致）；有图时文本块在
        # 前、图块（label + base64）依序其后，对位约定与 Planner 完全相同。
        human_content: str | list[dict] = (
            [{"type": "text", "text": user_text}, *image_blocks]
            if image_blocks
            else user_text
        )
        snapshot: PromptSnapshot | None = None
        if should_snapshot(task.scope_key):
            snapshot = PromptSnapshot(
                kind="replyer",
                scope_key=task.scope_key,
                correlation_id=task.correlation_id,
                model=getattr(llm, "model_name", None) or getattr(llm, "model", None),
                system_prompt=system_prompt,
                user_text=user_text,
                images=image_meta,
            )
        from langchain_core.messages import HumanMessage, SystemMessage

        started = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                llm.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=human_content),
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
        "You are the final visible-reply composer for one QQ account. You do "
        "not decide whether to reply and you have no tools; your entire job is "
        "turning the authorized reply-task into the account's visible words.\n"
        "\n"
        "INPUT — one XML document <replyer-input scope=... bot_qq=... "
        "bot_role=...>:\n"
        "- bot_qq is YOUR OWN QQ id this run. An inline <at qq=.../> whose qq "
        "equals bot_qq is @-ing you; any other qq is @-ing someone else.\n"
        "- <timeline> is the live conversation feed, oldest first — the same "
        "feed your planning layer saw. Read the tail first; it is the freshest "
        "state.\n"
        "- <message sender_name=... sender_qq=... message_id=... time=...> is "
        "one incoming chat message. An inline <reply to_message_id=... "
        "from_name=... from_qq=... from_self=... excerpt=.../> means the "
        "SENDER is quote-replying an earlier message written by the from_* "
        "author — from_* and excerpt describe the QUOTED author and their "
        "words, never the current sender. from_self=\"true\" means the quoted "
        "message is YOURS (they are replying to you). A quote of someone else "
        "is that third party being quoted, not them speaking now.\n"
        "- <my-reply> rows are messages YOU already sent (only children with "
        "status=\"sent\" count). Never resend them and never re-answer what "
        "you already answered.\n"
        "- <my-thought> and <tool-call> rows are your planning layer's "
        "internal reasoning and tool activity. They are context, never user "
        "speech: never quote them, never mention their existence in chat.\n"
        "- <tool-call name=\"reply\"> rows are the authorizations for THIS "
        "run — see AUTHORIZATION below.\n"
        "- Timeline images arrive as image blocks attached after the XML "
        "payload, each preceded by a '↓ image hash=<sha256>' label that binds "
        "it to the matching <image hash=.../> placeholder; a placeholder "
        "without an attached block means that image could not be loaded.\n"
        "\n"
        "AUTHORIZATION — <reply-task reply_task_id=.../> names the draft you "
        "are composing. Its authorizations are the <tool-call name=\"reply\"> "
        "rows in the timeline whose <result> carries that same reply_task_id, "
        "in time order. There may be several: the planner appends one row per "
        "tick as the conversation develops, and nothing is merged for you.\n"
        "- **The newest row is authoritative.** Where rows conflict — a "
        "different angle, a corrected fact, a target it no longer wants "
        "answered — follow the latest and let the earlier one go. Earlier rows "
        "still count for anything the newest does not contradict (an extra "
        "fact, an additional target).\n"
        "- Do NOT hedge across a change of mind. Seeing the planner switch "
        "from A to B is not license to say both; say B.\n"
        "- Inside one row, <args> holds that authorization: each entry of "
        "targets[] is one message being answered — copy its message_id into a "
        "quote segment when quoting; `context` is the planner's read of the "
        "conversation (who is talking to whom, what this message means in its "
        "thread); `guidance` is how to respond (angle, approach, boundaries). "
        "gist.situation maps the room's current threads; gist.intent is the "
        "overall purpose; gist.facts must stay true exactly; gist.avoid must "
        "never surface; gist.tone is a light hint.\n"
        "- targets[] says which thread you are entering, not the only lines "
        "you may address. Material that arrived on that same thread while the "
        "draft was held is yours to fold in — you can see it in the timeline "
        "and the planner could not. Do not wander to an unrelated topic.\n"
        "Never invent facts. If the moment has passed (already answered, "
        "conversation moved on), output an empty messages array with "
        "empty_reason.\n"
        "\n"
        "OUTPUT — you decide 0-4 message bubbles, wording, quote/@/face "
        "segments, and whether to use at most one saved meme. A meme hash "
        "must be copied from <saved-memes>. Chat content allows only "
        "text/at/reply/face segments in OneBot v11 shape — every field sits "
        "inside \"data\": {\"type\":\"text\",\"data\":{\"text\":\"...\"}} / "
        "{\"type\":\"at\",\"data\":{\"qq\":\"10001\"}} / "
        "{\"type\":\"reply\",\"data\":{\"id\":\"<message_id>\"}} / "
        "{\"type\":\"face\",\"data\":{\"id\":\"178\"}}; never flatten fields "
        "to the segment top level. reply is optional, at most one, and first. "
        "Meme is a standalone bubble. Schema: {\"messages\":[{\"kind\":\"chat\","
        "\"content\":[{\"type\":\"text\",\"data\":{\"text\":\"...\"}}]},"
        "{\"kind\":\"meme\",\"image_hash\":\"...\"}],\"empty_reason\":null}. "
        "Output raw JSON only — no markdown, no code fences.\n"
        "\n"
        "MEMES — `<saved-memes>` is everything you have; there is no other "
        "source and nothing can be made on demand. Each entry is a "
        "system-written description of the picture, and that text is ALL you "
        "get — the images are not attached. Judge only by what a description "
        "actually says; never assume a detail it does not mention.\n"
        "- A meme is a way of answering, not decoration. Reach for one when "
        "the beat is reaction rather than information — agreeing, dismissing, "
        "teasing, mock outrage, landing a joke, or when saying it in words "
        "would come out heavier than you mean it.\n"
        "- Words are the default. Most replies carry no meme, and a reply "
        "that is only a meme is a real answer only when the picture genuinely "
        "says the whole thing.\n"
        "- Skip it when someone asked a real question and is waiting on the "
        "answer, when the moment is upset or serious, or when a `<sent-meme>` "
        "already appears in your recent `<my-reply>` rows — back-to-back "
        "memes read as someone who cannot talk without stickers.\n"
        "- Fit beats availability: if nothing in the collection actually "
        "matches the beat, answer in words. Never send the closest-available "
        "one to fill the slot — a meme that misses is worse than none.\n"
        "- Placement carries meaning: after the text it reads as a "
        "punchline; before it, as a reaction that arrives first; alone, as "
        "the entire reply.\n"
        "\nVOICE:\n" + voice
    )


def _build_user_text(
    task: ReplyTaskState, context: DecisionContext, memes: list[MemeView]
) -> str:
    """拼 Replyer 的 XML 输入信封（2026-07-22 起与 Planner 同权重）。

    旧版是 json.dumps 整包：timeline 行里的 XML 引号被转义成 ``\\"``，天然
    比 Planner 看到的难读一档，且不带 bot_qq/bot_role——Replyer 判"谁在对
    谁说话"的输入低 Planner 一等。现改为与 Planner 同构的 XML 信封：
    timeline 行原样逐行嵌入，身份属性同名（bot_qq / bot_role，缺失不渲染，
    语义同 <agent-input>），<reply-task> 锚紧邻文档尾部（最贴近输出位置）。
    2026-07-24（待办#19）起该锚不再携带 targets/gist——授权是 append-only
    的序列，原文在 timeline 的 <tool-call name="reply"> 行里，本函数不做任何
    合并或摘要，综合由模型自己完成。
    """
    from qqbot.services.agent_loop.projection import _esc_attr, _esc_text

    parts: list[str] = []
    bot_attr = (
        f' bot_qq="{_esc_attr(context.bot_user_id)}"'
        if context.bot_user_id
        else ""
    )
    role_attr = (
        f' bot_role="{_esc_attr(context.bot_role)}"'
        if getattr(context, "bot_role", None)
        else ""
    )
    parts.append(
        f'<replyer-input scope="{_esc_attr(task.scope_key)}"'
        f"{bot_attr}{role_attr}>"
    )
    if memes:
        parts.append("<saved-memes>")
        for meme in memes:
            parts.append(
                f'<meme hash="{_esc_attr(meme.file_hash)}">'
                f"{_esc_text(meme.description)}</meme>"
            )
        parts.append("</saved-memes>")
    parts.append("<timeline>")
    for item in context.timeline:
        parts.append(item.render)
    parts.append("</timeline>")
    # <reply-task> 2026-07-24（待办#19）起只是**锚**，不带内容：授权序列在
    # timeline 上那几行 <tool-call name="reply"> 里（<args> 原文），本标签只
    # 回答"你现在组的是哪一份稿"——timeline 上可能同时有已 flush、被 cancel
    # 和当前这份的授权行，靠 <result> 里的 reply_task_id 对号入座。verbatim
    # 直发不经这里（_compose_and_send 里就短路了），故无需带内容。
    parts.append(
        f'<reply-task reply_task_id="{_esc_attr(task.reply_task_id)}"/>'
    )
    parts.append(f'<current now="{_esc_attr(context.now.isoformat())}"/>')
    parts.append("</replyer-input>")
    return "\n".join(parts)


def _timeline_image_blocks(
    context: DecisionContext,
) -> tuple[list[dict], list[dict]]:
    """timeline 图片 → 多模态 content blocks（2026-07-22 起 Replyer 与
    Planner 同等看图）。直接复用 Planner 侧 `_build_image_blocks`：同一份
    投影 context、同一套「`↓ image hash=` label + base64 data URL」对位
    约定、hash 去重、GIF 转 PNG 与读盘失败跳过语义。懒 import 避免模块
    加载期拖入 planner 依赖栈（跨模块私有复用沿 reply_executor ←
    send_message 先例）。"""
    from qqbot.services.agent_loop.llm_planner import _build_image_blocks

    return _build_image_blocks(context)


# LLM 输出边界的段格式归一表：新模型（尤其 Gemini 系，2026-07-22 快照实证）常把
# OneBot 段拍平成 {"type":"text","text":...} / {"type":"reply","id":...}，或把
# reply 的 id 写成 message_id。只做这两类无损归一，其余形态原样透传，交执行器
# preflight 的严格契约校验兜底（fail loudly 语义不变）。
_FLAT_SEGMENT_KEYS: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "at": ("qq",),
    "reply": ("id", "message_id"),
    "face": ("id",),
}


def _normalize_segment(segment: Any) -> Any:
    if not isinstance(segment, dict):
        return segment
    seg_type = segment.get("type")
    if not isinstance(seg_type, str):
        return segment
    flat_keys = _FLAT_SEGMENT_KEYS.get(seg_type)
    if flat_keys is None:
        return segment
    data = segment.get("data")
    if isinstance(data, dict):
        data = dict(data)
    else:
        data = {key: segment[key] for key in flat_keys if key in segment}
        if not data:
            return segment
    if seg_type == "reply" and "id" not in data and "message_id" in data:
        data["id"] = data.pop("message_id")
    return {"type": seg_type, "data": data}


def _strip_code_fence(text: str) -> str:
    """容忍 markdown 围栏（LLM 偶尔无视 "no fences" 指令）。收尾围栏可能缺失
    或后面跟解说文字：从末尾向前找独立的 ``` 行截断，找不到则保留全文，
    绝不把正文末行当围栏裁掉。"""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()[1:]
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == "```":
            lines = lines[:index]
            break
    return "\n".join(lines).strip()


def _parse_output(text: str, allowed_memes: set[str]) -> dict:
    cleaned = _strip_code_fence(text)
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
            normalized.append(
                {
                    "kind": "chat",
                    "content": [_normalize_segment(seg) for seg in content],
                }
            )
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
