"""Replyer 输入信封合同：与 Planner 同权重的 XML 形态（2026-07-22）。

旧版 _build_user_text 是 json.dumps 整包——timeline 行内的 XML 引号被转义
成 \\"，可读性低 Planner 一档，且不带 bot_qq/bot_role。本文件钉住新契约：
XML 信封、timeline 行原样嵌入、身份属性与 <agent-input> 同名同语义。

2026-07-25 起另钉住 system prompt 的 §MEMES 段与角色卡里的表情包动机段
（ReplyerMemeGuidanceTests）：发送决策权归 Replyer 之后，判据只能长在这两处。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import (
    DecisionContext,
    MemeView,
    TimelineItem,
)
from qqbot.services.agent_loop.reply_task import ReplyTaskState
from qqbot.services.agent_loop.replyer import (
    _build_system_prompt,
    _build_user_text,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=TZ)
HASH_A = "a" * 64

ROW = (
    '<message sender_name="李四" sender_qq="222" time="2026-07-22T11:59:00+08:00"'
    ' message_id="M1"><at qq="10001"/>在吗</message>'
)


def _task() -> ReplyTaskState:
    return ReplyTaskState(
        reply_task_id="R1",
        scope_key="group:100",
        revision=2,
        state="claimed",
        created_at=NOW,
        updated_at=NOW,
        flush_at=NOW,
        hard_deadline=NOW + timedelta(seconds=90),
        mode="compose",
        targets=[{"message_id": "M1", "context": "李四@我打招呼"}],
        gist={"intent": "回应", "situation": "只有这一条线"},
        verbatim_messages=[],
        latest_event_id="E1",
        source_tool_call_event_id="TC1",
        correlation_id="CID",
    )


def _context(**overrides: object) -> DecisionContext:
    fields: dict = dict(
        scope_key="group:100",
        correlation_id="CID",
        tick_seq=0,
        now=NOW,
        timeline=[
            TimelineItem(
                event_id="E1", occurred_at=NOW, kind="message", render=ROW
            )
        ],
    )
    fields.update(overrides)
    return DecisionContext(**fields)


class ReplyerUserTextTests(unittest.TestCase):
    def test_envelope_is_xml_with_identity_and_verbatim_timeline_rows(
        self,
    ) -> None:
        text = _build_user_text(
            _task(),
            _context(bot_user_id="10001", bot_role="member"),
            [],
        )
        self.assertTrue(
            text.startswith(
                '<replyer-input scope="group:100" bot_qq="10001" '
                'bot_role="member">'
            )
        )
        self.assertTrue(text.endswith("</replyer-input>"))
        # timeline 行原样逐行嵌入——不能再被 JSON 转义（\" 是旧形态的标志）。
        self.assertIn(ROW, text)
        self.assertNotIn('\\"', text.split("<reply-task", 1)[0])
        self.assertIn("<timeline>", text)
        self.assertIn("</timeline>", text)
        self.assertIn('<current now="2026-07-22T12:00:00+08:00"/>', text)

    def test_reply_task_section_is_a_bare_anchor(self) -> None:
        """2026-07-24（待办#19）起 <reply-task> 只是锚：授权是 append-only
        的序列，原文在 timeline 的 <tool-call name="reply"> 行里，这里不再
        搬一份 targets/gist——否则又成了双重渲染，且"哪几条仍然有效"会与
        timeline 上的序列打架。"""
        text = _build_user_text(_task(), _context(), [])
        self.assertIn('<reply-task reply_task_id="R1"/>', text)
        self.assertNotIn('"situation"', text)
        self.assertNotIn("李四@我打招呼", text)

    def test_identity_attributes_absent_when_unresolved(self) -> None:
        text = _build_user_text(_task(), _context(), [])
        self.assertIn('<replyer-input scope="group:100">', text)
        self.assertNotIn("bot_qq=", text)
        self.assertNotIn("bot_role=", text)

    def test_saved_memes_rendered_only_when_present(self) -> None:
        without = _build_user_text(_task(), _context(), [])
        self.assertNotIn("<saved-memes>", without)
        with_memes = _build_user_text(
            _task(),
            _context(),
            [
                MemeView(
                    file_hash=HASH_A,
                    description="黑猫瞪眼，不屑语气",
                    saved_at=NOW,
                )
            ],
        )
        self.assertIn("<saved-memes>", with_memes)
        self.assertIn(f'<meme hash="{HASH_A}">', with_memes)
        self.assertIn("黑猫瞪眼，不屑语气", with_memes)


class ReplyerMemeGuidanceTests(unittest.TestCase):
    """system prompt 的 §MEMES 段（2026-07-25 新增）。

    在此之前，"什么时候该发表情包"在任何 prompt 里都没有正文：system prompt
    只有半句 `whether to use at most one saved meme`，voice.md 一字未提——
    发送决策权 2026-07-19 归 Replyer 后，判据一直是空的。三处 prompt 的分工见
    表情包工具黑盒设计 §6.1；本类钉住其中 Replyer 的两处（判据 + 人格动机），
    Planner 那处由 test_llm_planner / xml_format 侧覆盖。
    """

    def _prompt(self) -> str:
        return _build_system_prompt()

    def test_memes_section_states_the_core_judgements(self) -> None:
        prompt = self._prompt()
        self.assertIn("MEMES", prompt)
        # 描述是选图的全部依据 —— 不得脑补描述里没写的细节。
        self.assertIn("the images are not attached", prompt)
        # 文字是默认；合适度优先于可用性（宁可不发也不硬凑）。
        self.assertIn("Words are the default", prompt)
        self.assertIn("Fit beats availability", prompt)
        # 连发抑制锚在 timeline 上真实存在的 <sent-meme> 行（投影渲染的标签），
        # 不是凭空造的信号；写错标签名等于这条判据永远不触发。
        self.assertIn("<sent-meme>", prompt)

    def test_voice_card_is_folded_in_with_its_meme_section(self) -> None:
        prompt = self._prompt()
        # 角色卡整体仍在（voice.md 是唯一权威来源，缺失应 fail loudly）。
        self.assertIn("小奏", prompt)
        # 人格层的表情包动机段（voice.md §表情包）也必须随卡片进来。
        self.assertIn("表情包对你不是装饰", prompt)


if __name__ == "__main__":
    unittest.main()
