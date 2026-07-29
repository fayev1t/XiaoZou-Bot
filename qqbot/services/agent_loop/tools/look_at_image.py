"""LookAtImageTool —— 带着具体问题重看群里的某张图（2026-07-28 新增）。

为什么需要它：
  同日起 Planner / Replyer 降级为纯文本模型，群里的图在 EventIngest 落盘时由
  专用 VLM 转录成一段**客观描述**写进事件正文，投影渲染成
  `<image hash="..." desc="..."/>`。那段描述是无语境的（ingest 时刻语境往往
  还不存在——先甩图、隔几条再补话是常态），所以它必然覆盖不到所有后续追问。
  本工具就是那条兜底路径：模型现场带着 timeline 语境提一个具体问题，重新看一
  次原图。**没有它，这次改动就是纯降级**；有了它，描述不够用时天花板还在。

参数（刻意只有两个，理由见下）：
  image_hash  必填，64 位 sha256，从 timeline 的 <image hash="..."/> 原样抄
  question    必填，自由文本

  question 必填不是形式要求：不带问题的调用等于把 ingest 那次转录再跑一遍，
  而那份转录已经在 timeline 里了，纯浪费一次 VLM 调用。必填能挡住"再看一眼"
  式的偷懒调用。
  也刻意**不**拆成 context + task 两个参数 —— 模型会自己把语境写进问题里，
  参数面越小越不容易选错（同 reply 工具 2026-07-25 收敛参数的取舍）。

返回：{image_hash, question, answer}

失败语义（统一结构化 ToolOutcome，全程无 raise，见契约 §7.2）：
  invalid_arguments    hash 非 64 位 hex（bad_image_hash）/ question 缺失或
                       非字符串（bad_question）/ question 超长（question_too_long）
  image_not_found      hash 合法但盘上没有这个文件（抄错 hash / 图当初没下载
                       成功 / 文件已被媒体 GC 清理）
  upstream_action_failed  VLM 未配置 / 调用失败 / 返回空（retryable —— 对模型
                       是"对面暂时不给看"，不是它的参数错了）
  预料外异常 → BaseTool.run 兜底 internal_tool_error

不缓存：同一张图不同问题答案不同；而相同问题的答案已经作为 tool_result 留在
事件流里，窗口期内模型自己看得到（usage 文档里明确要求别重复问）。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.image_description import (
    ImageLookError,
    answer_about_image,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._meme_common import (
    coerce_image_hash,
    media_path_for_hash,
    sniff_mime,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "look_at_image.md")

# question 上限：它是一个具体问题，不是正文。过长说明模型在把 timeline 抄进来，
# 而 VLM 并不需要那些（它只回答被问到的事）。
MAX_QUESTION_CHARS = 500


class LookAtImageTool(BaseTool):
    name = "look_at_image"
    description = (
        "Look at one image again with a specific question in mind. Timeline "
        'images already carry an objective desc="..." written when they '
        "arrived; call this only when that description cannot answer what you "
        "actually need to know — it costs a fresh vision call. GUEST, any scope."
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "image_hash": {
                "type": "string",
                "description": (
                    "64-char sha256 hex copied verbatim from an "
                    '<image hash="..."/> tag in the timeline. Images without '
                    "a hash= were never downloaded and cannot be looked at."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "The specific thing you need to know about this image. "
                    "Write in the chat context yourself — the model looking at "
                    "the image cannot see the conversation. Asking for a "
                    "general description is wasteful: you already have one."
                ),
            },
        },
        "required": ["image_hash", "question"],
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # GUEST + 不限 scope：enforce_access 实为 no-op，但统一保留首行调用。
        if fail := await self.enforce_access(context):
            return fail

        image_hash, failure = coerce_image_hash(arguments.get("image_hash"))
        if failure is not None:
            return failure
        assert image_hash is not None

        raw_question = arguments.get("question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return ToolOutcome.failure(
                "invalid_arguments",
                "question is required: state the specific thing you need to "
                "know about this image. The timeline already carries an "
                "objective description — re-asking for one wastes a call.",
                field="question",
                reason_code="bad_question",
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        question = raw_question.strip()
        if len(question) > MAX_QUESTION_CHARS:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"question must be at most {MAX_QUESTION_CHARS} chars, got "
                f"{len(question)} — ask one specific thing, do not paste the "
                "conversation.",
                field="question",
                reason_code="question_too_long",
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        path = media_path_for_hash(image_hash)
        try:
            data = path.read_bytes()
        except OSError:
            return ToolOutcome.failure(
                "image_not_found",
                f"no image on disk with hash {image_hash}; copy the hash= "
                'value from an <image hash="..."/> tag in the timeline '
                "(images without hash= were never downloaded).",
                image_hash=image_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        try:
            answer = await answer_about_image(
                data, sniff_mime(data), image_hash, question
            )
        except ImageLookError as exc:
            return ToolOutcome.failure(
                "upstream_action_failed",
                f"vision call failed: {exc}",
                image_hash=image_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )

        return ToolOutcome.success(
            {
                "image_hash": image_hash,
                "question": question,
                "answer": answer,
            }
        )
