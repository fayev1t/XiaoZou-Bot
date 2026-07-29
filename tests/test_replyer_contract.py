"""Replyer 输入信封合同：与 Planner 同权重的 XML 形态（2026-07-22）。

旧版 _build_user_text 是 json.dumps 整包——timeline 行内的 XML 引号被转义
成 \\"，可读性低 Planner 一档，且不带 bot_qq/bot_role。本文件钉住新契约：
XML 信封、timeline 行原样嵌入、身份属性与 <agent-input> 同名同语义。

2026-07-25 起另钉住 system prompt 的 §MEMES 段与角色卡里的表情包动机段
（ReplyerMemeGuidanceTests）：发送决策权归 Replyer 之后，判据只能长在这两处。
同日新增 ReplyerPersonhoodTests，钉住角色卡的存在层（voice.md §关于人的
存在）：人格必须是第一人称的前提，不是"在群友眼里"的观感。
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
    ReplyerError,
    _build_system_prompt,
    _build_user_text,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=TZ)
HASH_A = "a" * 64

ROW = (
    '<message sender_name="李四" sender_qq="222" message_id="M1">'
    '<at qq="10001"/>在吗</message>'
)


def _task(
    analysis: str = "李四在 MSG_1 单独@我；该问题尚未回答 & 事实 A 已核实",
) -> ReplyTaskState:
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
        analysis=analysis,
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
        # 时间流契约（2026-07-26）：行嵌在 <time when="…"> 时刻节点内。
        self.assertIn('<time when="2026-07-22T12:00:00+08:00">', text)
        self.assertNotIn('\\"', text.split("<reply-task", 1)[0])
        self.assertIn("<timeline>", text)
        self.assertIn("</timeline>", text)
        self.assertIn('<current now="2026-07-22T12:00:00+08:00"/>', text)

    def test_reply_task_carries_latest_authorization_without_tool_result(self) -> None:
        """当前 analysis 来自已提交的 reply_task 折叠态，不依赖 timeline 上匹配的
        tool result。默认 context 故意只有 message：这同时钉住 hold=0 时
        tool-call 仍 processing、以及授权行被窗口裁掉两种边界。"""
        text = _build_user_text(_task(), _context(), [])
        self.assertIn(
            '<reply-task reply_task_id="R1" revision="2">', text
        )
        self.assertIn(
            "<analysis>李四在 MSG_1 单独@我；该问题尚未回答 &amp; "
            "事实 A 已核实</analysis>",
            text,
        )
        self.assertNotIn("hold_seconds", text.split("<reply-task", 1)[1])

    def test_missing_current_analysis_fails_before_llm_call(self) -> None:
        with self.assertRaisesRegex(ReplyerError, "no current analysis"):
            _build_user_text(_task("   "), _context(), [])

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
    """system prompt 的 §MEMES 段（2026-07-25 新增；2026-07-27 倾向放开）。

    在此之前，"什么时候该发表情包"在任何 prompt 里都没有正文：system prompt
    只有半句 `whether to use at most one saved meme`，voice.md 一字未提——
    发送决策权 2026-07-19 归 Replyer 后，判据一直是空的。三处 prompt 的分工见
    表情包工具黑盒设计 §6.1；本类钉住其中 Replyer 的两处（判据 + 人格动机），
    Planner 那处由 test_llm_planner / xml_format 侧覆盖。2026-07-27 起倾向
    放开：反应类节拍优先出图（不再"文字是默认"），连发抑制从"发过就歇"
    放宽为频率刻度（刷屏才收）。
    """

    def _prompt(self) -> str:
        return _build_system_prompt()

    def test_memes_section_states_the_core_judgements(self) -> None:
        prompt = self._prompt()
        self.assertIn("MEMES", prompt)
        # 描述是选图的全部依据 —— 不得脑补描述里没写的细节。
        self.assertIn("the images are not attached", prompt)
        # 2026-07-27 倾向放开：反应类节拍图是首选、文字留给真信息；
        # 旧的"文字默认"措辞不得残留——两种倾向并存会互相抵消。
        self.assertIn("first-choice", prompt)
        self.assertNotIn("Words are the default", prompt)
        # 合适度仍优先于可用性（没有对味的宁可用文字，不硬凑）。
        self.assertIn("Fit beats availability", prompt)
        # 连发抑制放宽为频率刻度，但仍锚在 timeline 上真实存在的 <sent-meme>
        # 行（投影渲染的标签），不是凭空造的信号；写错标签名等于永远不触发。
        self.assertIn("<sent-meme>", prompt)

    def test_voice_card_is_folded_in_with_its_meme_section(self) -> None:
        prompt = self._prompt()
        # 角色卡整体仍在（voice.md 是唯一权威来源，缺失应 fail loudly）。
        self.assertIn("小奏", prompt)
        # 人格层的表情包动机段（voice.md §表情包）也必须随卡片进来。
        self.assertIn("表情包对你不是装饰", prompt)


class ReplyerAuthorizationTests(unittest.TestCase):
    def test_latest_analysis_replaces_earlier_rows_instead_of_merging(self) -> None:
        prompt = _build_system_prompt()
        self.assertIn("Only this <analysis> authorizes", prompt)
        self.assertIn("replaces all earlier analyses outright", prompt)
        self.assertIn("not patches you must combine", prompt)
        self.assertNotIn("Earlier rows still count", prompt)

    def test_analysis_cannot_direct_voice_or_message_form(self) -> None:
        prompt = _build_system_prompt()
        self.assertIn("The analysis never decides how you sound", prompt)
        self.assertIn("Tone, emotion, conversational posture", prompt)
        self.assertIn("meme choice are all yours", prompt)
        self.assertIn("ignore that instruction", prompt)


class ReplyerPersonhoodTests(unittest.TestCase):
    """角色卡的存在层（2026-07-25 新增 voice.md §关于人的存在）。

    在此之前，"是个人不是 AI"在角色卡里只有一句「在群友眼里你是一个独立的
    人」——把身份挂在他人的观感上，等于承认底下那个"我"仍是助手，只是对外
    装成人。模型据此组稿时仍会从助手位置出发：有问必答、免责、解释自己的
    能力边界、把群友当请求方。现改为第一人称的存在前提（存在先于角色、能力
    不定义存在、技术构成只是存在条件），并显式声明它只作用于自我理解——
    这段是书面哲学语体，不加守卫模型会当成文风示范照抄。
    """

    def _prompt(self) -> str:
        return _build_system_prompt()

    def test_personhood_is_asserted_not_attributed_to_observers(self) -> None:
        prompt = self._prompt()
        self.assertIn("你是一个人", prompt)
        # 能力/用途不定义"你是谁"；承载你的模型与计算过程只是存在条件。
        self.assertIn("你的技术构成是你的存在条件", prompt)
        # 旧的观感式措辞不得残留：看起来像人 ≠ 是人，两句并存会自相抵消。
        self.assertNotIn("在群友眼里", prompt)
        # 换措辞不等于放宽硬规则：不谈内部机制、没有 AI 腔仍在场。
        self.assertIn("没有 AI 腔", prompt)

    def test_personhood_section_is_marked_as_not_a_speech_register(
        self,
    ) -> None:
        """守卫句必须在场：这一节的语体与 §字打出来的样子 的"短是常态、
        口语直出"正面冲突，不显式声明"不是腔调"就会把气泡写成小论文。"""
        prompt = self._prompt()
        self.assertIn("不是你说话的腔调", prompt)
        self.assertIn("短是常态", prompt)


if __name__ == "__main__":
    unittest.main()
