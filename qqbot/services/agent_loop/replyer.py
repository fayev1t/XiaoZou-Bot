"""Replyer：每个 compose ReplyTask 只调用一次的最终可见回复编排器。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.decision import DecisionContext, MemeView
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)
from qqbot.services.agent_loop.reply_task import ReplyTaskState

logger = get_logger(__name__)

MAX_OUTBOUND_MESSAGES = 4
MAX_MEMES_PER_REPLY = 1
# 2026-07-22 随 Replyer 看图放宽从 12s 提到 25s（多模态载荷上传+推理更慢）。
# 2026-07-28 Replyer 改回纯文本后没有调低：组稿本身仍可能长（timeline + 收藏夹
# 全量进 prompt），25s 是给慢端点的余量，不是当初为图片留的。
# 2026-07-29 提到 60s 并补一次重试：25s 实际切在端点常态延迟的正中间——最近
# 四天 planner 档调用（与组稿同量级的 prompt）p50 在 9~27.5s 间浮动，07-28 当
# 天中位数 27.5s 本身就越过了 25s，p95 落在 43~60s。而这个超时是外层 wait_for
# 取消的，RoutedChatModel 按 CancelledError 原样透传（既不计端点失败也不切下
# 一个），所以一次偏慢的调用就是一次彻底失败：final 记 failed、Planner 醒来看
# 到 failed 通常判 idle，那句已经决定要说的话再没有出口。单次上限按最坏一天的
# p95 取 60s；总预算 90s 封顶，因为组稿期间 reply_task 已是 claimed 态、不再是
# open，Planner 可以另开一份新授权——拖得越久越容易两份授权先后说话。
REPLYER_TIMEOUT_SECONDS = 60.0
REPLYER_TOTAL_BUDGET_SECONDS = 90.0
# 剩余预算不足这个数就不再开新尝试：几秒钟的重试只够白烧一次请求。
REPLYER_MIN_RETRY_SECONDS = 20.0
REPLYER_MAX_ATTEMPTS = 2


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
        # 温度不在这里定：roles.replyer.temperature（建议 0.3）> settings 全局
        # 缺省，见 LLM 路由契约 §2（2026-07-29 收拢）。
        llm = self._llm or await create_llm(role="replyer")
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

        raw, latency_ms = await _invoke_within_budget(
            llm,
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_text),
            ],
            snapshot,
            scope_key=task.scope_key,
        )
        text = _extract_text(raw).strip()
        if snapshot is not None:
            snapshot.add_attempt(
                latency_ms=latency_ms,
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


def _describe_call_error(exc: BaseException, budget: float) -> str:
    """把调用异常压成一行可读死因。

    ``asyncio.wait_for`` 抛的 TimeoutError 自带空 ``str()``——线上那条
    ``replyer call failed: TimeoutError:`` 后面什么都没有，看不出是谁超时、
    上限几秒、还是端点真的报错了。这里把当次预算补进去。
    """
    if isinstance(exc, asyncio.TimeoutError):
        return f"TimeoutError: 单次上限 {budget:g}s 用尽"
    return f"{type(exc).__name__}: {exc}"


async def _invoke_within_budget(
    llm: Any,
    messages: list[Any],
    snapshot: PromptSnapshot | None,
    *,
    scope_key: str,
) -> tuple[Any, int]:
    """在总预算内调用组稿 LLM，失败时于剩余预算里重试，返回 (响应, 耗时ms)。

    单次不超过 ``REPLYER_TIMEOUT_SECONDS``、合计不超过
    ``REPLYER_TOTAL_BUDGET_SECONDS``、至多 ``REPLYER_MAX_ATTEMPTS`` 次。重试只
    针对调用本身（超时、端点报错）——输出解析失败不在这里重试：同一份 prompt
    再问一遍大概率还是同样的坏输出，白烧预算。

    每次失败都记进快照并打一条带耗时的 warning：外层 wait_for 的取消在
    ``[llm]`` 路由日志里只表现为 CancelledError 透传，这里不打就真的查不到
    ——线上正是因此看不见 replyer 的超时，延迟统计只剩幸存者。
    """
    deadline = time.monotonic() + REPLYER_TOTAL_BUDGET_SECONDS
    attempts = 0
    while True:
        attempts += 1
        budget = min(REPLYER_TIMEOUT_SECONDS, deadline - time.monotonic())
        started = time.monotonic()
        try:
            raw = await asyncio.wait_for(llm.ainvoke(messages), timeout=budget)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            detail = _describe_call_error(exc, budget)
            if snapshot is not None:
                snapshot.add_attempt(latency_ms=elapsed_ms, error=detail[:300])
            logger.warning(
                "[replyer] {} 第 {} 次组稿调用失败（{}ms）：{}",
                scope_key,
                attempts,
                elapsed_ms,
                detail,
            )
            remaining = deadline - time.monotonic()
            if (
                attempts >= REPLYER_MAX_ATTEMPTS
                or remaining < REPLYER_MIN_RETRY_SECONDS
            ):
                if snapshot is not None:
                    snapshot.outcome = "call_error"
                    write_snapshot(snapshot)
                raise ReplyerError(
                    f"replyer call failed: {detail}（已尝试 {attempts} 次，"
                    f"剩余预算 {max(remaining, 0.0):.0f}s）"
                ) from exc
            continue
        return raw, int((time.monotonic() - started) * 1000)


def _build_system_prompt() -> str:
    """组稿 system prompt = prompts/catalog.py 的 "replyer" 装配单。

    两段：`prompts/replyer.md`（职责页 + 角色卡——2026-07-30 由维护者把
    `voice.md` 并入并删除后者，它自此是角色卡的唯一权威来源）+ 与 Planner
    共享的 `prompts/envelope.md`。任一文件缺失或为空时 catalog 直接抛
    （OSError / PromptSectionMissing），这里统一转成 ReplyerError，维持
    "组稿失败、final 记 failed 并回注时间线"的 fail-loudly 语义——绝不静默
    降级成无人格腔。这条红线定于 2026-07-19 角色卡自 tools/send_message.md
    的 Voice 节迁出时：靠字符串切片从废弃工具文档里捞人格，文件被清理或改
    标题就会无声失效，那是最难被发现的坏法。
    """
    from qqbot.services.agent_loop.prompts.catalog import render_system_prompt

    try:
        return render_system_prompt("replyer")
    except ReplyerError:
        raise
    except Exception as exc:
        raise ReplyerError(
            f"replyer prompt assets unavailable: {exc}"
        ) from exc


def _build_user_text(
    task: ReplyTaskState, context: DecisionContext, memes: list[MemeView]
) -> str:
    """拼 Replyer 的 XML 输入信封（2026-07-22 起与 Planner 同权重）。

    旧版是 json.dumps 整包：timeline 行里的 XML 引号被转义成 ``\\"``，天然
    比 Planner 看到的难读一档，且不带 bot_qq/bot_role——Replyer 判"谁在对
    谁说话"的输入低 Planner 一等。现改为与 Planner 同构的 XML 信封：
    timeline 行原样逐行嵌入，身份属性同名（bot_qq / bot_role，缺失不渲染，
    语义同 <agent-input>），<reply-task> 锚紧邻文档尾部（最贴近输出位置）。
    2026-07-28 起，``<reply-task>`` 携带折叠出的**最新完整 analysis**。旧
    revision 仍在 timeline 的 tool-call 行里供历史回看，但不再参与 Replyer
    授权合并；这同时消除 hold=0 时 terminal result 尚未落库、以及活跃群窗口
    裁剪掉授权行造成的空授权竞态。
    """
    from qqbot.services.agent_loop.projection import (
        _esc_attr,
        _esc_text,
        render_timeline_stream,
    )

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
    # 与 Planner 逐字节同构的时间流渲染（render_timeline_stream，2026-07-26）。
    parts.extend(render_timeline_stream(context.timeline))
    parts.append("</timeline>")
    analysis = task.analysis.strip() if isinstance(task.analysis, str) else ""
    if not analysis:
        raise ReplyerError("compose reply_task has no current analysis")
    # 当前授权来自已提交的 reply_task upsert 折叠态，而不是通用 timeline：
    # notify 发生在 ToolWorker 写 terminal result 之前，hold=0 时 tool-call 仍可
    # 是 processing；timeline 也有条数上限。revision 一并透出，方便快照审计。
    parts.append(
        f'<reply-task reply_task_id="{_esc_attr(task.reply_task_id)}" '
        f'revision="{task.revision}">'
        f"<analysis>{_esc_text(analysis)}</analysis>"
        "</reply-task>"
    )
    parts.append(f'<current now="{_esc_attr(context.now.isoformat())}"/>')
    parts.append("</replyer-input>")
    return "\n".join(parts)


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
