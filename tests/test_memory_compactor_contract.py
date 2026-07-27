"""MemoryCompactor 契约测试（记忆系统契约 §2/§4/§5）。

钉住：
1. 批选取与游标——折叠区间 (旧游标, covers_until] 不重叠、保留最新 KEEP 条、
   低于 TRIGGER 不动手、非 group scope 直接拒绝；
2. recap 事件字段——type/origin/visibility、correlation 运行级新配、
   causation=覆盖边界事件、occurred_at 回填边界+1ms、payload 全字段；
3. 失败语义——LLM 输出不可解析时不写任何事件（宁可没记忆不写脏记忆）；
4. 单次触顶——恰好一次 LLM merge、一次 recap 落盘，超预算直接硬截断；
5. 输出协议——标签块是主格式（引号/换行免转义），旧 JSON 兼容；
6. 启动语义——worker 启动不扫描、不查询、不调用 LLM。

全离线：_ScriptedSession 按调用顺序回放查询结果并捕获 insert（照
test_task_store_contract 的 fake 惯例）；LLM / 系统提示词 / 渲染均注入假件。
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.dialects import postgresql

from qqbot.services.agent_loop.memory_compactor import (
    RECAP_EVENT_TYPE,
    MemoryCompactor,
    parse_compaction_output,
    truncate_at_sentence,
)

CHINA = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 27, 12, 0, 0, tzinfo=CHINA)
RUN_AT = BASE + timedelta(minutes=30)

# 测试参数（setUp 里灌进环境；os.environ 优先于 .env 文件）：
# TRIGGER=6 / KEEP=3 / 摘要上限 100 字。
_TEST_ENV = {
    "PROMPT_SNAPSHOT_ENABLED": "false",
    "MEMORY_SUMMARY_MAX_CHARS": "100",
    "MEMORY_COMPACTION_TRIGGER_EVENTS": "6",
    "MEMORY_COMPACTION_KEEP_EVENTS": "3",
}


# ─── fakes ───


class _AnyResult:
    """一份结果三用：scalars().all()（ORM 行）、.all()（列元组）、
    .rowcount（insert）。"""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []
        self.rowcount = 1

    def scalars(self) -> "_AnyResult":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _ScriptedSession:
    """按 execute 调用顺序回放脚本结果并捕获全部语句（脚本耗尽后返回
    insert 型空结果）。脚本与捕获列表跨 session 实例共享——压缩器每次
    查询都新开 session。"""

    def __init__(self, script: list[_AnyResult], captured: list[Any]) -> None:
        self._script = script
        self._captured = captured

    async def execute(self, stmt: Any) -> _AnyResult:
        self._captured.append(stmt)
        if self._script:
            return self._script.pop(0)
        return _AnyResult()

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory(script: list[_AnyResult], captured: list[Any]):
    def make() -> _ScriptedSession:
        return _ScriptedSession(script, captured)

    return make


class _FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.calls: list[Any] = []
        self._replies = list(replies)

    async def ainvoke(self, messages: Any) -> Any:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return SimpleNamespace(content=self._replies[idx])


def _ev_row(i: int, *, type: str = "external.message.group.normal") -> SimpleNamespace:
    """伪 AgentEvent ORM 行（_snapshot_from_row / project 只读这些属性）。"""
    return SimpleNamespace(
        event_id=f"EV{i:05d}",
        occurred_at=BASE + timedelta(seconds=i),
        origin=type.split(".", 1)[0],
        type=type,
        scope="group",
        group_id=999,
        user_id=111,
        visibility="agent_visible",
        correlation_id=None,
        causation_id=None,
        payload={"raw_message": f"msg {i}"},
    )


def _keys(rows: list[SimpleNamespace]) -> list[tuple[str, datetime]]:
    return [(r.event_id, r.occurred_at) for r in rows]


def _json_reply(summary: str, cues: list[str] | None = None) -> str:
    return json.dumps(
        {"summary": summary, "recall_cues": cues or ["有人再提这事时"]},
        ensure_ascii=False,
    )


def _reply(summary: str, cues: list[str] | None = None) -> str:
    cue_lines = "\n".join(
        f"- {cue}" for cue in (cues if cues is not None else ["有人再提这事时"])
    )
    return (
        f"<summary>\n{summary}\n</summary>\n<recall-cues>\n{cue_lines}\n</recall-cues>"
    )


def _params(stmt: Any) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


def _make_compactor(
    script: list[_AnyResult], captured: list[Any], llm: _FakeLLM
) -> MemoryCompactor:
    async def llm_factory() -> Any:
        return llm

    compactor = MemoryCompactor(_factory(script, captured), llm_factory=llm_factory)
    # 渲染与系统提示词注入假件：渲染逻辑属投影域（另有专测），这里只验
    # 压缩器自身的契约。
    compactor._load_system_prompt = lambda: "SYS"  # type: ignore[method-assign]
    compactor._render_slice = (  # type: ignore[method-assign]
        lambda scope_key, snaps, now: "\n".join(f"<row {s.event_id}/>" for s in snaps)
    )
    return compactor


class _EnvMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _TEST_ENV}
        os.environ.update(_TEST_ENV)

    def tearDown(self) -> None:
        for key, old in self._saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# ─── 纯函数 ───


class ParseOutputTests(unittest.TestCase):
    def test_tagged_output_preserves_quotes_and_newlines(self) -> None:
        summary = '群名是"MCE汉化组"。\n群主昵称里也有"引号"。'
        parsed = parse_compaction_output(_reply(summary, ["再提群名时"]))
        assert parsed is not None
        self.assertEqual(parsed[0], summary)
        self.assertEqual(parsed[1], ["再提群名时"])

    def test_tagged_output_tolerates_fence_case_and_tag_space(self) -> None:
        text = (
            "```text\n<SUMMARY >\n总结。\n</SUMMARY >\n"
            "<RECALL-CUES >\n• 线索\n</RECALL-CUES >\n```"
        )
        parsed = parse_compaction_output(text)
        assert parsed is not None
        self.assertEqual(parsed, ("总结。", ["线索"]))

    def test_tagged_cues_are_filtered_and_capped_at_five(self) -> None:
        text = (
            "<summary>总结。</summary>\n<recall-cues>\n"
            "- a\n\n* b\n• c\n1. d\n2、e\n- f\n</recall-cues>"
        )
        parsed = parse_compaction_output(text)
        assert parsed is not None
        self.assertEqual(parsed[1], ["a", "b", "c", "d", "e"])

    def test_duplicate_summary_blocks_return_none(self) -> None:
        self.assertIsNone(
            parse_compaction_output(
                "<summary>第一段</summary><summary>第二段</summary>"
            )
        )

    def test_malformed_tags_do_not_fall_back_to_embedded_json(self) -> None:
        text = "<summary>未闭合\n" + _json_reply("不应被选中。")
        self.assertIsNone(parse_compaction_output(text))

    def test_legacy_plain_json(self) -> None:
        parsed = parse_compaction_output(_json_reply("总结。", ["线索"]))
        assert parsed is not None
        self.assertEqual(parsed[0], "总结。")
        self.assertEqual(parsed[1], ["线索"])

    def test_legacy_fenced_json(self) -> None:
        text = "```json\n" + _json_reply("总结。") + "\n```"
        parsed = parse_compaction_output(text)
        assert parsed is not None
        self.assertEqual(parsed[0], "总结。")

    def test_legacy_brace_slice_from_prose(self) -> None:
        text = "结果如下：" + _json_reply("总结。") + " 完"
        parsed = parse_compaction_output(text)
        assert parsed is not None
        self.assertEqual(parsed[0], "总结。")

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_compaction_output("不是 json 的自由发挥"))

    def test_non_dict_json_returns_none(self) -> None:
        self.assertIsNone(parse_compaction_output("[1, 2, 3]"))

    def test_empty_summary_returns_none(self) -> None:
        self.assertIsNone(
            parse_compaction_output('{"summary": "  ", "recall_cues": []}')
        )
        self.assertIsNone(
            parse_compaction_output("<summary>  </summary><recall-cues></recall-cues>")
        )

    def test_cues_filtered_and_capped_at_five(self) -> None:
        raw = json.dumps(
            {
                "summary": "总结。",
                "recall_cues": ["a", "", 3, "b", "c", "d", "e", "f"],
            }
        )
        parsed = parse_compaction_output(raw)
        assert parsed is not None
        self.assertEqual(parsed[1], ["a", "b", "c", "d", "e"])


class TruncateTests(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        self.assertEqual(truncate_at_sentence("短。", 10), "短。")

    def test_cuts_at_sentence_boundary(self) -> None:
        self.assertEqual(truncate_at_sentence("一句。二句。三句。", 5), "一句。")

    def test_hard_cut_without_punctuation(self) -> None:
        self.assertEqual(truncate_at_sentence("abcdefghij", 5), "abcde")


# ─── 压缩主流程 ───


class CompactScopeTests(_EnvMixin, unittest.IsolatedAsyncioTestCase):
    async def test_below_trigger_writes_nothing(self) -> None:
        captured: list[Any] = []
        rows = [_ev_row(i) for i in range(1, 5)]  # 4 < TRIGGER(6)
        script = [_AnyResult([]), _AnyResult(_keys(rows))]
        llm = _FakeLLM([_reply("不该被调用")])
        compactor = _make_compactor(script, captured, llm)

        outcome = await compactor.compact_scope("group:999", now=RUN_AT)

        self.assertEqual(outcome.skipped_reason, "below_trigger")
        self.assertEqual(outcome.rounds, 0)
        self.assertEqual(len(captured), 2)  # cursor + keys，无 insert
        self.assertEqual(llm.calls, [])

    async def test_first_generation_full_flow(self) -> None:
        captured: list[Any] = []
        rows = [_ev_row(i) for i in range(1, 11)]  # 10 ≥ TRIGGER(6)
        slice_rows = rows[:7]  # 10 - KEEP(3) → EV00001..EV00007
        script = [
            _AnyResult([]),  # 游标：无 recap
            _AnyResult(_keys(rows)),  # 未覆盖键
            _AnyResult(slice_rows),  # 折叠区间行
            _AnyResult(),  # insert
        ]
        llm = _FakeLLM([_reply("首代总结。", ["约饭场景", "开黑时间"])])
        compactor = _make_compactor(script, captured, llm)

        outcome = await compactor.compact_scope("group:999", now=RUN_AT)

        self.assertIsNone(outcome.skipped_reason)
        self.assertEqual(outcome.rounds, 1)
        self.assertEqual(len(outcome.event_ids), 1)
        self.assertEqual(len(captured), 4)

        params = _params(captured[3])
        self.assertEqual(params["type"], RECAP_EVENT_TYPE)
        self.assertEqual(params["origin"], "runtime")
        self.assertEqual(params["visibility"], "agent_visible")
        self.assertEqual(params["scope"], "group")
        self.assertEqual(params["group_id"], 999)
        self.assertEqual(params["causation_id"], "EV00007")
        self.assertEqual(len(params["correlation_id"]), 26)
        self.assertIsNone(params["idempotency_key"])
        # occurred_at 回填 = 覆盖边界 + 1ms（渲染落在接缝处）。
        boundary_at = BASE + timedelta(seconds=7)
        self.assertEqual(params["occurred_at"], boundary_at + timedelta(milliseconds=1))
        payload = params["payload"]
        self.assertEqual(payload["summary"], "首代总结。")
        self.assertEqual(payload["recall_cues"], ["约饭场景", "开黑时间"])
        self.assertEqual(payload["covers_until_event_id"], "EV00007")
        self.assertEqual(payload["covers_until_occurred_at"], boundary_at.isoformat())
        self.assertEqual(
            payload["covers_from_occurred_at"],
            (BASE + timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(payload["dropped_event_count"], 7)
        self.assertEqual(payload["folded_revision"], 1)
        self.assertEqual(payload["compactor_version"], 2)
        # 首代：user 信封声明无旧摘要。
        user_text = llm.calls[0][1].content
        self.assertIn('<previous-summary empty="true"/>', user_text)
        self.assertIn("<row EV00001/>", user_text)
        # 纯文本契约（契约 §5.1）：压缩调用绝不携带多模态块——两条消息的
        # content 必须是纯 str（图像在渲染文本里只是 <image hash=…/> 占位，
        # role=memory 因此可配纯文本模型）。
        for message in llm.calls[0]:
            self.assertIsInstance(message.content, str)

    async def test_second_generation_folds_previous_summary(self) -> None:
        captured: list[Any] = []
        boundary_at = BASE + timedelta(seconds=7)
        recap_row = SimpleNamespace(
            event_id="RC00001",
            occurred_at=boundary_at + timedelta(milliseconds=1),
            payload={
                "summary": "旧摘要内容。",
                "recall_cues": [],
                "covers_until_event_id": "EV00007",
                "covers_until_occurred_at": boundary_at.isoformat(),
                "folded_revision": 1,
            },
        )
        rows = [_ev_row(i) for i in range(8, 21)]  # EV00008..EV00020，共 13
        slice_rows = rows[:10]  # 13 - KEEP(3) → ..EV00017
        script = [
            _AnyResult([recap_row]),
            _AnyResult(_keys(rows)),
            _AnyResult(slice_rows),
            _AnyResult(),  # insert
        ]
        llm = _FakeLLM([_reply("二代总结。")])
        compactor = _make_compactor(script, captured, llm)

        outcome = await compactor.compact_scope("group:999", now=RUN_AT)

        self.assertIsNone(outcome.skipped_reason)
        params = _params(captured[3])
        self.assertEqual(params["causation_id"], "EV00017")
        self.assertEqual(params["payload"]["folded_revision"], 2)
        user_text = llm.calls[0][1].content
        self.assertIn("旧摘要内容。", user_text)
        self.assertIn('revision="1"', user_text)

    async def test_parse_failure_writes_no_event(self) -> None:
        captured: list[Any] = []
        rows = [_ev_row(i) for i in range(1, 11)]
        script = [
            _AnyResult([]),
            _AnyResult(_keys(rows)),
            _AnyResult(rows[:7]),
        ]
        llm = _FakeLLM(["自由发挥，没有标签或 JSON"])
        compactor = _make_compactor(script, captured, llm)

        outcome = await compactor.compact_scope("group:999", now=RUN_AT)

        self.assertEqual(outcome.skipped_reason, "llm_failed")
        self.assertEqual(outcome.rounds, 0)
        self.assertEqual(len(captured), 3)  # 只有三次查询，绝无 insert

    async def test_large_input_is_merged_in_one_request_and_one_write(self) -> None:
        captured: list[Any] = []
        rows = [_ev_row(i) for i in range(1, 51)]
        slice_rows = rows[:47]
        script = [
            _AnyResult([]),
            _AnyResult(_keys(rows)),
            _AnyResult(slice_rows),
            _AnyResult(),  # insert
        ]
        llm = _FakeLLM([_reply("完整总结。")])
        compactor = _make_compactor(script, captured, llm)
        huge_input = "x" * 50000
        compactor._render_slice = (  # type: ignore[method-assign]
            lambda _scope_key, _snaps, _now: huge_input
        )

        outcome = await compactor.compact_scope("group:999", now=RUN_AT)

        self.assertEqual(outcome.rounds, 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(captured), 4)
        self.assertIn(huge_input, llm.calls[0][1].content)
        payload = _params(captured[3])["payload"]
        self.assertEqual(payload["dropped_event_count"], 47)
        self.assertEqual(payload["covers_until_event_id"], "EV00047")

    async def test_budget_truncates_without_second_llm_call(self) -> None:
        captured: list[Any] = []
        rows = [_ev_row(i) for i in range(1, 11)]
        script = [
            _AnyResult([]),
            _AnyResult(_keys(rows)),
            _AnyResult(rows[:7]),
            _AnyResult(),  # insert
        ]
        long_summary = "这是一句测试。" * 20
        llm = _FakeLLM([_reply(long_summary)])
        compactor = _make_compactor(script, captured, llm)

        await compactor.compact_scope("group:999", now=RUN_AT)

        self.assertEqual(len(llm.calls), 1)
        summary = _params(captured[3])["payload"]["summary"]
        self.assertLessEqual(len(summary), 100)
        self.assertTrue(summary.endswith("。"))

    async def test_non_group_scope_rejected(self) -> None:
        captured: list[Any] = []
        compactor = _make_compactor([], captured, _FakeLLM([]))

        outcome = await compactor.compact_scope("system", now=RUN_AT)

        self.assertEqual(outcome.skipped_reason, "scope_not_supported")
        self.assertEqual(captured, [])


class TriggerWorkerTests(_EnvMixin, unittest.IsolatedAsyncioTestCase):
    async def test_start_does_not_scan_query_or_call_llm(self) -> None:
        captured: list[Any] = []
        llm = _FakeLLM([])
        compactor = _make_compactor([], captured, llm)

        compactor.start()
        try:
            await asyncio.sleep(0)

            self.assertFalse(compactor._wake.is_set())
            self.assertEqual(compactor._pending, set())
            self.assertEqual(captured, [])
            self.assertEqual(llm.calls, [])
        finally:
            await compactor.stop()


class ScopeAllowlistTests(_EnvMixin, unittest.IsolatedAsyncioTestCase):
    """MEMORY_COMPACTION_SCOPES 灰度白名单（记忆系统契约 §8 S3）：构造时
    读定；notify 就地拦掉名单外 scope（零 SQL 零 env 读），compact_scope
    兜底复核；裸群号归一化为 group:<id>。"""

    async def test_allowlist_gates_notify_and_compact(self) -> None:
        os.environ["MEMORY_COMPACTION_SCOPES"] = "group:1, 999"
        try:
            captured: list[Any] = []
            compactor = _make_compactor([], captured, _FakeLLM([]))

            compactor.notify("group:2", 6)
            self.assertEqual(compactor._pending, set())
            compactor.notify("group:1", 5)  # 未触顶不入队
            self.assertEqual(compactor._pending, set())
            self.assertFalse(compactor._wake.is_set())
            compactor.notify("group:1", 6)
            compactor.notify("group:999", 6)  # 裸群号已归一化进白名单
            self.assertEqual(compactor._pending, {"group:1", "group:999"})

            outcome = await compactor.compact_scope("group:2", now=RUN_AT)
            self.assertEqual(outcome.skipped_reason, "scope_not_enabled")
            self.assertEqual(captured, [])  # 名单外零查询
        finally:
            os.environ.pop("MEMORY_COMPACTION_SCOPES", None)

    async def test_empty_allowlist_allows_all_groups(self) -> None:
        compactor = _make_compactor([], [], _FakeLLM([]))
        compactor.notify("group:42", 6)
        compactor.notify("system", 6)  # 非 group 仍被拒
        self.assertEqual(compactor._pending, {"group:42"})


class RenderSliceSmokeTests(_EnvMixin, unittest.TestCase):
    def test_real_projection_pipeline_renders_runtime_hint(self) -> None:
        """走真实 projection 折叠/渲染管线的冒烟（细节由投影契约测试钉）。"""
        compactor = MemoryCompactor(_factory([], []))
        snaps = [
            _ev_row(1, type="runtime.wait_elapsed"),
            _ev_row(2, type="runtime.wait_elapsed"),
        ]
        for snap in snaps:
            snap.payload = {"seconds": 300, "note": "回头看看"}

        text = compactor._render_slice("group:999", snaps, RUN_AT)

        self.assertIn("wait_elapsed", text)
        self.assertIn("<time", text)


if __name__ == "__main__":
    unittest.main()
