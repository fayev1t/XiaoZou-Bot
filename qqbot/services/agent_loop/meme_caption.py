"""表情包收录/换描述时的看图写描述（meme 工具 save/recaption 的内部 LLM 调用）。

meme 工具不让 planner 在动作 JSON 里顺手写收藏描述：决策 tick 的主职是决策，
顺手写的一句话密度和稳定性都不够。这里用专用 prompt 单独调一次多模态 LLM：
输入 = 图片 bytes（+ planner 可选提供的群聊语境 context_note——纯看图写不出
"这是谁的名场面/本群怎么用"），输出 = 一段密度优先的中文描述，落进
agent_memes.description；之后 <saved-memes> 渲染与 meme.send 选图都只看它。

注入方式：caption_image 由 v2_main 传给 LoopSupervisor → ToolWorker，在
run() context 里以 ``caption_image`` 键到达 meme 工具 —— 工具不直接 import
本模块，契约测试塞假 captioner 即可全离线跑（与 session_factory 的注入/伪造
方式一致）。

失败语义：LLM 未配置 / 调用异常 / 空输出一律 **raise CaptionError**，由
meme 工具折成 ToolOutcome.failure("caption_failed", retryable=True)——收录的
核心产出就是描述，生成失败宁可整体失败让 LLM 下拍重试，不落无描述的残记录
（recaption 场景则保留旧描述不动）。
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.image_utils import normalize_image_for_llm
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)

logger = get_logger(__name__)

# 描述上限（字符）。收藏夹整体进 prompt（MAX_SAVED_MEMES 条），单条必须短；
# prompt 里要求 ≤150 字（2026-07-27 起，给使用场景留篇幅），这里再硬截兜底。
MAX_DESCRIPTION_CHARS = 300

# 看图写描述的专用 prompt 2026-07-27 外置为 prompts/meme_caption.md（收口，
# 段目录见 prompts/catalog.py）：只描述、不寒暄、限长；描述要同时可"检索"
# （画面/文字）与可"使用"（情绪/场景）——meme.send 选图时模型只看这段文本，
# 且收藏夹全 bot 共享，描述必须自包含。required 段：文件缺失/为空时上抛，
# caption_image 折成 CaptionError（收藏失败、不落表），不静默用空指令看图。
def _load_caption_prompt() -> str:
    from qqbot.services.agent_loop.prompts.catalog import render_system_prompt

    return render_system_prompt("caption")

class CaptionError(RuntimeError):
    """caption 生成失败（LLM 未配置 / 调用异常 / 空输出）。"""


async def caption_image(
    image_bytes: bytes, mime: str, context_note: str | None = None
) -> str:
    """看图生成收藏描述。失败一律 raise CaptionError（见模块 docstring）。

    与 llm_planner 同一个 create_llm 入口，走 role="caption" 路由并硬性
    要求 vision 能力（单服务商旧配置视为天然多模态，行为不变；多服务商
    注册表下 caption 候选须带 vision 标签或显式配置 caption role）。
    每次调用新建包装对象 —— 收藏是低频动作，底层客户端由 llm 层缓存。
    """
    try:
        image_bytes, mime = normalize_image_for_llm(image_bytes, mime or "image/png")
    except Exception as exc:
        raise CaptionError(
            f"caption image conversion failed: {type(exc).__name__}: {exc}"
        ) from exc

    # 温度在 roles.caption.temperature 配（建议低温 0.2：同一张图的描述应当
    # 稳定，不需要发散），见 LLM 路由契约 §2。
    llm = await create_llm(role="caption", require=("vision",))
    if llm is None:
        raise CaptionError(
            "caption LLM not configured "
            "(config/model_providers.json 缺失，或 caption role 无带 vision 能力的候选)"
        )

    from langchain_core.messages import HumanMessage

    try:
        prompt = _load_caption_prompt()
    except Exception as exc:
        raise CaptionError(
            f"caption prompt asset missing: {type(exc).__name__}: {exc}"
        ) from exc
    if context_note:
        prompt += f"\n收藏者附注（聊天语境，据实融进描述）：{context_note}"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime or 'image/png'};base64,{b64}"
                },
            },
        ]
    )
    # Prompt 快照（待办 #11）：辅助 LLM 调用同样留观测记录。图片只记
    # hash/mime/字节数（脱敏契约：base64 永不落盘）；scope_key=None——
    # 收藏夹是全 bot 共享的，caption 不属于任何单一 scope。
    snapshot: PromptSnapshot | None = None
    if should_snapshot(None):
        snapshot = PromptSnapshot(
            kind="meme_caption",
            model=getattr(llm, "model_name", None)
            or getattr(llm, "model", None),
            user_text=prompt,
            images=[
                {
                    "hash": hashlib.sha256(image_bytes).hexdigest(),
                    "mime": mime or "image/png",
                    "bytes": len(image_bytes),
                }
            ],
        )
    started = time.monotonic()
    try:
        raw = await llm.ainvoke([message])
    except Exception as exc:
        if snapshot is not None:
            snapshot.add_attempt(
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            snapshot.outcome = "call_error"
            write_snapshot(snapshot)
        raise CaptionError(
            f"caption LLM call failed: {type(exc).__name__}: {exc}"
        ) from exc
    text = _extract_text(raw).strip()
    if snapshot is not None:
        snapshot.add_attempt(
            latency_ms=int((time.monotonic() - started) * 1000),
            response_text=text,
            usage=extract_usage(raw),
        )
        snapshot.outcome = "ok" if text else "empty_response"
        write_snapshot(snapshot)
    if not text:
        raise CaptionError("caption LLM returned empty text")
    return text[:MAX_DESCRIPTION_CHARS]


def _extract_text(message: Any) -> str:
    """langchain BaseMessage.content 可能是 str 或 list[dict]，拍平成 str
    （与 llm_planner._extract_text 同语义的本地副本，避免反向 import）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict) and "text" in chunk:
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts)
    return str(content)
