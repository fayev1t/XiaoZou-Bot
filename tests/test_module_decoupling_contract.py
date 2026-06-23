"""Contract tests for ③ 模块解耦 —— 包级 __init__ 不在 import 期拉重基础设施。

这套测试**能在本地裸环境跑**(未装 sqlalchemy / langchain):如果 `qqbot.core`
或 `qqbot.services.agent_loop` 的 __init__ 仍 eager 导入依赖 sqlalchemy 的
database/projection/worker 等,下面的 `import` 行会直接 ModuleNotFoundError。
能导入成功,本身就证明重模块已惰性化。

冻结的契约:
- `import qqbot.core` / `import qqbot.services.agent_loop` 不连带拉重模块
- 二者均提供 PEP 562 `__getattr__`,公开名映射表 `_LAZY` 完整
- eager 名(纯数据类 / 轻量子模块)即时可用
- 未知名 → AttributeError;惰性名 → 路由到真实 import(而非 AttributeError)
"""

from __future__ import annotations

import unittest


class CoreLazyImportTests(unittest.TestCase):
    def test_import_core_pulls_no_heavy_infra(self) -> None:
        import qqbot.core as core  # 若 eager 拉 database(建 engine)→ 这里就炸

        self.assertTrue(hasattr(core, "__getattr__"))
        # 公开 API 名仍在 __all__
        for name in ("init_db", "close_db", "get_db_session", "create_llm"):
            self.assertIn(name, core.__all__)

    def test_unknown_attr_raises(self) -> None:
        import qqbot.core as core

        with self.assertRaises(AttributeError):
            _ = core.does_not_exist

    def test_lazy_name_routes_to_import_not_attribute_error(self) -> None:
        import qqbot.core as core

        # 访问惰性名应触发真实导入:服务器(装了 sqlalchemy)拿到函数;本地裸环境
        # 抛 ModuleNotFoundError —— 但**绝不**是 AttributeError(那意味着没接上)。
        try:
            obj = core.init_db
        except ModuleNotFoundError:
            return
        self.assertTrue(callable(obj))


class AgentLoopLazyImportTests(unittest.TestCase):
    def test_import_agent_loop_pulls_no_heavy_infra(self) -> None:
        import qqbot.services.agent_loop as al  # eager 拉 projection/worker 就会炸

        self.assertTrue(hasattr(al, "__getattr__"))

    def test_pure_dataclasses_are_eager(self) -> None:
        # 纯 stdlib 依赖,任何环境都能直接取
        from qqbot.services.agent_loop import DecisionContext, TaskView

        self.assertEqual(TaskView.__name__, "TaskView")
        self.assertEqual(DecisionContext.__name__, "DecisionContext")

    def test_bot_registry_submodule_eager(self) -> None:
        from qqbot.services.agent_loop import bot_registry

        self.assertTrue(hasattr(bot_registry, "register"))

    def test_heavy_classes_are_lazy_mapped(self) -> None:
        import qqbot.services.agent_loop as al

        for name, submod in {
            "LLMPlanner": "llm_planner",
            "Projector": "projection",
            "LoopSupervisor": "supervisor",
            "ReplySendWorker": "reply_worker",
            "ToolWorker": "tool_worker",
            "AgentLoop": "loop",
        }.items():
            self.assertEqual(al._LAZY.get(name), submod)
            self.assertIn(name, al.__all__)

    def test_unknown_attr_raises(self) -> None:
        import qqbot.services.agent_loop as al

        with self.assertRaises(AttributeError):
            _ = al.NoSuchSymbol

    def test_lazy_name_routes_to_import_not_attribute_error(self) -> None:
        import qqbot.services.agent_loop as al

        # 见 core 同名用例:惰性名必须路由到 import,本地裸环境抛
        # ModuleNotFoundError 也算"接上了",AttributeError 才是断链。
        try:
            obj = al.Projector
        except ModuleNotFoundError:
            return
        self.assertEqual(obj.__name__, "Projector")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
