"""Replyer 输入信封合同：与 Planner 同权重的 XML 形态（2026-07-22）。

旧版 _build_user_text 是 json.dumps 整包——timeline 行内的 XML 引号被转义
成 \\"，可读性低 Planner 一档，且不带 bot_qq/bot_role。本文件钉住新契约：
XML 信封、timeline 行原样嵌入、身份属性与 <agent-input> 同名同语义。

ReplyerPersonhoodTests 钉住角色卡的存在层：人格必须是第一人称的前提，不是
"在群友眼里"的观感。

ReplyerCallBudgetTests 钉住组稿调用的超时预算与重试语义——单次上限切在端点
常态延迟中间会让"已经决定要说的话"整句丢掉，见该类文档。

**2026-07-30 大幅收窄**：角色卡 2026-07-30 并入 replyer.md 时被重写为上位抽象
措辞，维护者判定随之消失的四段正文（§MEMES 判据、引用/@ 判据、analysis 授权
语义、§字打出来的样子 的语域守卫）是有意删除，不是合并事故。钉住那些正文的
断言（ReplyerMemeGuidanceTests 全类、ReplyerQuoteSegmentTests 两条、
ReplyerAuthorizationTests 全类、语域守卫一条）随之删除——留着就是拿已废弃的
措辞当契约。**只有措辞变了、概念仍在的重新锚定，没有删**（"你是一个人" →
"你是qq群内一个有主体性的人"）。四段正文若要回来，判据得连测试一起重写；
缺口登记在 待办清单.md。角色卡在场（`小奏` 在渲染结果里）由
test_prompt_catalog_contract 兜住，不在本文件重复。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop import replyer
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


class ReplyerSegmentListTests(unittest.TestCase):
    """输出段清单的读法（2026-07-29 新增，2026-07-30 收窄为一条）。

    `reply` 段的曝光原本全在读入语义与输出格式上（"optional, at most one, and
    first" 是格式约束，却被读成标配槽位），于是模型几乎条条挂引用。这条钉住
    改后的读法：清单是"合法的结构集合"而非待填的槽位，同时格式约束本身仍在。

    **同类的另两条已删**（2026-07-30）：角色卡里的引用/@ 判据（"引用和 @ 不是
    礼貌，是定位手段" / "默认一条都不挂"）与 analysis 点名 id 不等于要引用
    （"不是要你挂引用段"）两段正文，在角色卡重写时被有意删除，判据现已不在任何
    prompt 里；缺口登记在 待办清单.md。
    """

    def _prompt(self) -> str:
        return _build_system_prompt()

    def test_segment_list_is_not_read_as_a_slot_to_fill(self) -> None:
        prompt = self._prompt()
        self.assertIn("而非待填满的槽位", prompt)
        # 格式约束本身仍在（至多一个、必须首位），只是不再是唯一的一句话。
        self.assertIn("最多一个且排在首位", prompt)


class ReplyerPersonhoodTests(unittest.TestCase):
    """角色卡的存在层（2026-07-25 引入，2026-07-30 随卡片重写换措辞）。

    在此之前，"是个人不是 AI"在角色卡里只有一句「在群友眼里你是一个独立的
    人」——把身份挂在他人的观感上，等于承认底下那个"我"仍是助手，只是对外
    装成人。模型据此组稿时仍会从助手位置出发：有问必答、免责、解释自己的
    能力边界、把群友当请求方。现在是第一人称的存在前提（存在先于角色、能力
    不定义存在、技术构成只是存在条件）。

    锚点 2026-07-30 随卡片重写重锚："你是一个人" → "你是qq群内一个有主体性的
    人"（概念未变，只是措辞）。同期删掉的是 `没有 AI 腔` 与语域守卫那一条
    （"不是你说话的腔调" / "短是常态"）——§字打出来的样子 整段已不在卡片里，
    守卫句失去了它要守的对象，留着断言等于拿已废弃措辞当契约。
    """

    def _prompt(self) -> str:
        return _build_system_prompt()

    def test_personhood_is_asserted_not_attributed_to_observers(self) -> None:
        prompt = self._prompt()
        self.assertIn("你是qq群内一个有主体性的人", prompt)
        # 能力/用途不定义"你是谁"；承载你的模型与计算过程只是存在条件。
        self.assertIn("你的技术构成是你的存在条件", prompt)
        # 旧的观感式措辞不得残留：看起来像人 ≠ 是人，两句并存会自相抵消。
        self.assertNotIn("在群友眼里", prompt)


class ReplyerCallBudgetTests(unittest.IsolatedAsyncioTestCase):
    """组稿调用的超时预算与重试（2026-07-29）。

    线上真实故障：端点当时的常态延迟就在 25s 上下（当天 planner 档 p50
    12.5s、p95 43.4s，同一拍的 planner 调用花了 32.5s），组稿被 25s 的
    wait_for 砍掉；超时是从外面取消的，RoutedChatModel 按 CancelledError
    透传、不计端点失败也不切换，于是一次偏慢就是彻底失败——final 记
    failed，Planner 醒来看到 failed 判 idle，已经决定要说的话没有出口。
    这里钉住：单次上限内失败还有预算就重试、预算不够不空烧、死因带上上限
    秒数（旧文案 ``TimeoutError:`` 后面是空的，读日志的人看不出是谁超时）。
    """

    def setUp(self) -> None:
        self._patches = [
            patch.object(replyer, "REPLYER_TIMEOUT_SECONDS", 0.05),
            patch.object(replyer, "REPLYER_TOTAL_BUDGET_SECONDS", 5.0),
            patch.object(replyer, "REPLYER_MIN_RETRY_SECONDS", 0.01),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_slow_first_attempt_is_retried_within_budget(self) -> None:
        llm = _ScriptedLLM(["hang", '{"messages": [], "empty_reason": "ok"}'])

        parsed = await replyer.Replyer(llm).compose(_task(), _context(), [])

        self.assertEqual(parsed["empty_reason"], "ok")
        self.assertEqual(llm.calls, 2)

    async def test_attempts_are_capped_and_error_names_the_budget(self) -> None:
        llm = _ScriptedLLM(["hang", "hang", "hang"])

        with self.assertRaises(ReplyerError) as caught:
            await replyer.Replyer(llm).compose(_task(), _context(), [])

        message = str(caught.exception)
        self.assertEqual(llm.calls, replyer.REPLYER_MAX_ATTEMPTS)
        self.assertIn("TimeoutError", message)
        self.assertIn("单次上限", message)  # 空 str() 的 TimeoutError 已补齐
        self.assertIn("已尝试 2 次", message)

    async def test_no_retry_when_remaining_budget_is_too_small(self) -> None:
        """总预算只够一次尝试时不开第二次：几秒的重试只够白烧一次请求。"""
        with patch.object(replyer, "REPLYER_MIN_RETRY_SECONDS", 30.0):
            llm = _ScriptedLLM(["hang", '{"messages": []}'])
            with self.assertRaises(ReplyerError):
                await replyer.Replyer(llm).compose(_task(), _context(), [])
        self.assertEqual(llm.calls, 1)

    async def test_invalid_output_is_not_retried(self) -> None:
        """坏输出不重试：同一份 prompt 再问一遍多半还是同样的坏输出。"""
        llm = _ScriptedLLM(["not json at all", '{"messages": []}'])

        with self.assertRaises(ReplyerError) as caught:
            await replyer.Replyer(llm).compose(_task(), _context(), [])

        self.assertIn("output invalid", str(caught.exception))
        self.assertEqual(llm.calls, 1)


class _ScriptedLLM:
    """按脚本逐次应答的假 LLM。``"hang"`` 表示这次调用永远不返回（交给真实
    的 wait_for 去砍，从而覆盖超时路径本身而不是伪造异常）。"""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.calls = 0

    async def ainvoke(self, messages: object, **kwargs: object) -> object:
        self.calls += 1
        reply = self._script[min(self.calls, len(self._script)) - 1]
        if reply == "hang":
            await asyncio.sleep(30)
        return SimpleNamespace(content=reply)


if __name__ == "__main__":
    unittest.main()
