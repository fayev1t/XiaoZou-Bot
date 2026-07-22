"""api_lab / main_test 实验台入口契约（待办 #7）。

纯文件断言（AST + 文本），本地无 nonebot / sqlalchemy 也能跑：

1. 实验台与生产互不相交：main_test 不加载 startup / v2_main；api_lab 与
   main_test 不 import DB / LLM / scheduler / services 栈 —— 保证实验动作
   零入库、agent loop 不会被实验台拉起。
2. main_test 自带适配器注册（register_adapter）—— 生产靠 nb run 读
   pyproject 注册，本入口必须独立于 nb-cli 可跑。
3. plugins_test 目录不在 ``[tool.nonebot]`` 的 plugin_dirs 中、api_lab 不
   存在于 qqbot/plugins/ —— nb run 的生产实例永远不会自动加载实验台插件。
4. api_lab 关键观测面存在：事件四通道 tap、SUPERUSER 门、set_group_kick
   快捷验证、JSONL 转储。

AST 解析本身兼做语法校验（等价 py_compile）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "qqbot"
MAIN_TEST = PACKAGE_DIR / "main_test.py"
API_LAB = PACKAGE_DIR / "plugins_test" / "api_lab.py"
PLUGINS_TEST_INIT = PACKAGE_DIR / "plugins_test" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"

# 实验台绝不允许触碰的模块前缀：DB / LLM / 调度器 / 整个 services 栈
# （EventIngest、agent_loop、工具面都在 services 下）。
FORBIDDEN_IMPORT_PREFIXES = (
    "qqbot.core.database",
    "qqbot.core.llm",
    "qqbot.core.scheduler",
    "qqbot.services",
)


def _imported_modules(path: Path) -> set[str]:
    """收集模块 import 的顶层目标（AST 级，不执行代码）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class MainTestEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MAIN_TEST.read_text(encoding="utf-8")

    def test_entry_files_exist(self) -> None:
        for path in (MAIN_TEST, API_LAB, PLUGINS_TEST_INIT):
            self.assertTrue(path.is_file(), f"missing {path}")

    def test_registers_adapter_itself(self) -> None:
        """生产入口靠 nb run 注册适配器；main_test 必须自己 register_adapter。"""
        self.assertIn("register_adapter", self.source)
        self.assertIn("OneBotV11Adapter", self.source)

    def test_loads_only_api_lab_plugin(self) -> None:
        self.assertIn("qqbot.plugins_test.api_lab", self.source)
        self.assertNotIn("qqbot.plugins.startup", self.source)
        self.assertNotIn("qqbot.plugins.v2_main", self.source)
        self.assertNotIn("qqbot.plugins.test_events", self.source)

    def test_no_forbidden_imports(self) -> None:
        for mod in _imported_modules(MAIN_TEST):
            for prefix in FORBIDDEN_IMPORT_PREFIXES:
                self.assertFalse(
                    mod == prefix or mod.startswith(prefix + "."),
                    f"main_test.py must not import {mod}",
                )


class ApiLabIsolationTests(unittest.TestCase):
    def test_no_forbidden_imports(self) -> None:
        """api_lab 无 DB / LLM / services 依赖：实验动作零入库、零 agent 反应。"""
        for mod in _imported_modules(API_LAB):
            for prefix in FORBIDDEN_IMPORT_PREFIXES:
                self.assertFalse(
                    mod == prefix or mod.startswith(prefix + "."),
                    f"api_lab.py must not import {mod}",
                )

    def test_outside_nb_run_plugin_dirs(self) -> None:
        """plugins_test 不能进 plugin_dirs，否则 nb run 生产实例会带上实验台。"""
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        self.assertIn('plugin_dirs = ["qqbot/plugins"]', pyproject)
        self.assertNotIn("plugins_test", pyproject)

    def test_not_in_production_plugins_dir(self) -> None:
        self.assertFalse((PACKAGE_DIR / "plugins" / "api_lab.py").exists())

    def test_init_syntax_ok(self) -> None:
        ast.parse(PLUGINS_TEST_INIT.read_text(encoding="utf-8"))


class ApiLabSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = API_LAB.read_text(encoding="utf-8")

    def test_four_channel_taps(self) -> None:
        for channel in ("on_message", "on_notice", "on_request", "on_metaevent"):
            self.assertIn(channel, self.source)

    def test_superuser_gate_on_commands(self) -> None:
        self.assertIn("SUPERUSER", self.source)
        self.assertIn('on_command("api", permission=SUPERUSER', self.source)
        self.assertIn('on_command("kick", permission=SUPERUSER', self.source)
        self.assertIn('on_command("probe", permission=SUPERUSER', self.source)

    def test_kick_probe_covers_todo_focus(self) -> None:
        """踢人实测面：set_group_kick + reject_add_request + 踢前成员快照。"""
        self.assertIn("set_group_kick", self.source)
        self.assertIn("reject_add_request", self.source)
        self.assertIn("get_group_member_info", self.source)

    def test_jsonl_dump_dir(self) -> None:
        self.assertIn("runtime_data", self.source)
        self.assertIn("api_lab", self.source)
        self.assertIn("jsonl", self.source.lower())


if __name__ == "__main__":
    unittest.main()
