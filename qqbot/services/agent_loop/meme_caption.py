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
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger

logger = get_logger(__name__)

# 描述上限（字符）。收藏夹整体进 prompt（MAX_SAVED_MEMES 条），单条必须短；
# prompt 里要求 ≤120 字，这里再硬截兜底。
MAX_DESCRIPTION_CHARS = 300

# 看图写描述的专用 prompt：只描述、不寒暄、限长。描述要同时可"检索"（画面/
# 文字）与可"使用"（情绪/场景）——meme.send 选图时模型只看这段文本。
# 收藏夹是全 bot 共享的（meme_store 全局收藏），描述会出现在收录时所在会话
# 之外的聊天里，因此要求自包含：附注里只有特定群才懂的背景要概括成通用场景，
# 不写死"本群/群友名"这类离开原群就失效的指代。
CAPTION_PROMPT = (
    "你在为 QQ 群机器人的表情包收藏夹写检索描述。看图输出一段不超过 120 字的"
    "中文描述，依次覆盖：画面内容（角色/动作/构图），图上文字（原样抄录，无则"
    "不提），情绪与语气，适合发出来的聊天场景。这份收藏夹会在多个聊天里使用，"
    "描述必须自包含：不要依赖只有某个群才懂的背景，附注里的群内梗请概括成"
    "通用的使用场景。只输出描述本身，不要任何前缀、引号、换行或解释。"
)

# caption 用低温：同一张图的描述应当稳定，不需要发散。
_CAPTION_TEMPERATURE = 0.2


class CaptionError(RuntimeError):
    """caption 生成失败（LLM 未配置 / 调用异常 / 空输出）。"""


async def caption_image(
    image_bytes: bytes, mime: str, context_note: str | None = None
) -> str:
    """看图生成收藏描述。失败一律 raise CaptionError（见模块 docstring）。

    与 llm_planner 同一个 create_llm 入口（同一份 LLM_* env 配置、天然多模态），
    每次调用新建客户端 —— 收藏是低频动作，不值得为它维护单例。
    """
    llm = await create_llm(temperature=_CAPTION_TEMPERATURE)
    if llm is None:
        raise CaptionError("caption LLM not configured (LLM_API_KEY / LLM_MODEL)")

    from langchain_core.messages import HumanMessage

    prompt = CAPTION_PROMPT
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
    try:
        raw = await llm.ainvoke([message])
    except Exception as exc:
        raise CaptionError(
            f"caption LLM call failed: {type(exc).__name__}: {exc}"
        ) from exc
    text = _extract_text(raw).strip()
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
