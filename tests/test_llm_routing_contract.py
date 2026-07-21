"""Contract tests for qqbot/core/llm_routing.py（LLM 注册表与按模型名路由）。

冻结的契约（详见 开发文档/v2.0/20-横切契约/LLM路由契约.md）：
- config/model_providers.json 文档格式（providers / roles / settings 三段）与校验错误；
- 按模型名路由：model= 命中所有持有该模型的服务商，缺省策略 random；
  model+provider 显式钉死，无视策略与冷却；
- role 规则解析顺序：精确命中 → "default" → 内置兜底（每服务商首模型）；
  targets 是回退链（前一目标的可用端点恒在后一目标之前）；
- require 能力过滤：显式 role 配置不满足时警告放行，其余入口严格过滤；
- 三种策略与冷却分区（冷却端点排尾不剔除）；
- 被动熔断：失败进冷却、连续失败指数退避封顶、成功清零；
- RoutedChatModel.ainvoke：逐端点尝试、失败切换、全败重抛最后异常、
  CancelledError 透传不计失败、尝试数受 max_attempts_per_call 封顶。

本模块与被测模块均零三方依赖，可在本地裸环境直接跑。
"""

from __future__ import annotations

import asyncio
import json
import random
import unittest
from typing import Any

from qqbot.core.llm_routing import (
    DEFAULT_STRATEGY,
    STRATEGY_PRIMARY_FAILOVER,
    STRATEGY_RANDOM,
    STRATEGY_ROUND_ROBIN,
    EndpointRouter,
    ModelEndpoint,
    RoleRule,
    RoleTarget,
    RoutedChatModel,
    collect_api_keys,
    parse_config,
)

_CONFIG_JSON = json.dumps(
    {
        "providers": [
            {
                "name": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-deepseek-key-123",
                "models": ["deepseek-chat", "deepseek-reasoner"],
            },
            {
                "name": "relay",
                "base_url": "https://relay.example.com/v1",
                "api_key": "sk-relay-key-456",
                "capabilities": ["vision"],
                "streaming": False,
                "timeout": 30,
                "max_tokens": 800,
                "models": [
                    "deepseek-chat",
                    {"name": "gpt-4o", "capabilities": []},
                ],
            },
        ],
        "roles": {
            "planner": "deepseek-chat",
            "caption": {"model": "gpt-4o", "require": ["vision"]},
            "default": ["deepseek-chat", {"model": "gpt-4o", "provider": "relay"}],
        },
        "settings": {"strategy": "primary_failover", "cooldown_seconds": 30},
    }
)


def _providers_only(providers: list[Any]) -> str:
    return json.dumps({"providers": providers})


def _endpoint(
    provider: str,
    model: str,
    *,
    caps: tuple[str, ...] = (),
) -> ModelEndpoint:
    return ModelEndpoint(
        provider=provider,
        model=model,
        base_url=f"https://{provider}.example.com/v1",
        api_key=f"sk-{provider}",
        capabilities=frozenset(caps),
    )


class ParseConfigTests(unittest.TestCase):
    def test_full_document_parses(self) -> None:
        config = parse_config(_CONFIG_JSON)

        self.assertEqual(
            [e.spec for e in config.endpoints],
            [
                "deepseek/deepseek-chat",
                "deepseek/deepseek-reasoner",
                "relay/deepseek-chat",
                "relay/gpt-4o",
            ],
        )
        self.assertEqual(config.default_strategy, STRATEGY_PRIMARY_FAILOVER)
        self.assertEqual(config.cooldown_seconds, 30.0)

        ds = config.endpoints[0]
        self.assertTrue(ds.streaming)  # 缺省 True
        self.assertIsNone(ds.timeout_seconds)
        self.assertEqual(ds.capabilities, frozenset())
        relay = config.endpoints[2]
        self.assertFalse(relay.streaming)
        self.assertEqual(relay.timeout_seconds, 30.0)
        self.assertEqual(relay.max_tokens, 800)
        # provider 级 capabilities 并入每个模型
        self.assertIn("vision", relay.capabilities)
        self.assertIn("vision", config.endpoints[3].capabilities)

    def test_roles_three_value_forms(self) -> None:
        roles = parse_config(_CONFIG_JSON).roles

        self.assertEqual(
            roles["planner"].targets, (RoleTarget(model="deepseek-chat"),)
        )
        self.assertIsNone(roles["planner"].strategy)
        self.assertEqual(roles["caption"].require, frozenset({"vision"}))
        self.assertEqual(
            roles["default"].targets,
            (
                RoleTarget(model="deepseek-chat"),
                RoleTarget(model="gpt-4o", provider="relay"),
            ),
        )

    def test_settings_defaults(self) -> None:
        raw = _providers_only(
            [
                {
                    "name": "p",
                    "base_url": "https://p/v1",
                    "api_key": "k",
                    "models": ["m"],
                }
            ]
        )
        config = parse_config(raw)
        self.assertEqual(config.default_strategy, DEFAULT_STRATEGY)
        self.assertEqual(config.default_strategy, STRATEGY_RANDOM)
        self.assertEqual(config.cooldown_seconds, 60.0)
        self.assertEqual(config.roles, {})

    def test_model_capabilities_normalized_lowercase(self) -> None:
        raw = _providers_only(
            [
                {
                    "name": "p",
                    "base_url": "https://p/v1",
                    "api_key": "k",
                    "models": [{"name": "m", "capabilities": ["Vision"]}],
                }
            ]
        )
        (endpoint,) = parse_config(raw).endpoints
        self.assertEqual(endpoint.capabilities, frozenset({"vision"}))

    def test_disabled_provider_skipped(self) -> None:
        raw = _providers_only(
            [
                {
                    "name": "off",
                    "enabled": False,
                    "base_url": "https://off/v1",
                    "api_key": "k",
                    "models": ["m"],
                },
                {
                    "name": "on",
                    "base_url": "https://on/v1",
                    "api_key": "k",
                    "models": ["m"],
                },
            ]
        )
        self.assertEqual(
            [e.spec for e in parse_config(raw).endpoints], ["on/m"]
        )

    def test_malformed_documents_raise_value_error(self) -> None:
        provider = {
            "name": "p",
            "base_url": "https://p/v1",
            "api_key": "k",
            "models": ["m"],
        }
        cases = {
            "非法 JSON": "not-json",
            "顶层非对象": json.dumps([provider]),
            "缺 providers": json.dumps({"roles": {}}),
            "providers 空": json.dumps({"providers": []}),
            "缺 name": _providers_only(
                [{"base_url": "https://x/v1", "api_key": "k", "models": ["m"]}]
            ),
            "缺 base_url": _providers_only(
                [{"name": "p", "api_key": "k", "models": ["m"]}]
            ),
            "缺 api_key": _providers_only(
                [{"name": "p", "base_url": "https://x/v1", "models": ["m"]}]
            ),
            "models 空": _providers_only(
                [{**provider, "models": []}]
            ),
            "name 含斜杠": _providers_only([{**provider, "name": "a/b"}]),
            "服务商重名": _providers_only([provider, provider]),
            "端点重复": _providers_only([{**provider, "models": ["m", "m"]}]),
            "enabled 非布尔": _providers_only(
                [{**provider, "enabled": "false"}]
            ),
            "timeout 非正数": _providers_only([{**provider, "timeout": 0}]),
            "roles 非对象": json.dumps(
                {"providers": [provider], "roles": ["m"]}
            ),
            "role 空字符串": json.dumps(
                {"providers": [provider], "roles": {"planner": ""}}
            ),
            "role 对象缺 model": json.dumps(
                {"providers": [provider], "roles": {"planner": {"provider": "p"}}}
            ),
            "role 空数组": json.dumps(
                {"providers": [provider], "roles": {"planner": []}}
            ),
            "role 未知策略": json.dumps(
                {
                    "providers": [provider],
                    "roles": {"planner": {"model": "m", "strategy": "sticky"}},
                }
            ),
            "role 钉死未知服务商": json.dumps(
                {
                    "providers": [provider],
                    "roles": {"planner": {"model": "m", "provider": "ghost"}},
                }
            ),
            "settings 非对象": json.dumps(
                {"providers": [provider], "settings": []}
            ),
            "settings 未知策略": json.dumps(
                {"providers": [provider], "settings": {"strategy": "sticky"}}
            ),
            "cooldown 非正数": json.dumps(
                {"providers": [provider], "settings": {"cooldown_seconds": -1}}
            ),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    parse_config(raw)


class CollectApiKeysTests(unittest.TestCase):
    def test_collects_from_full_document(self) -> None:
        self.assertEqual(
            collect_api_keys(_CONFIG_JSON),
            (
                ("deepseek", "sk-deepseek-key-123"),
                ("relay", "sk-relay-key-456"),
            ),
        )

    def test_collects_from_bare_provider_list(self) -> None:
        raw = json.dumps([{"name": "p", "api_key": "sk-bare-key"}])
        self.assertEqual(collect_api_keys(raw), (("p", "sk-bare-key"),))

    def test_never_raises_on_garbage(self) -> None:
        for raw in ("not-json", "{}", "[]", json.dumps([1, {"api_key": ""}])):
            with self.subTest(raw=raw):
                self.assertEqual(collect_api_keys(raw), ())

    def test_missing_name_falls_back_to_index_label(self) -> None:
        raw = json.dumps([{"api_key": "sk-anon-key"}])
        self.assertEqual(collect_api_keys(raw), (("#0", "sk-anon-key"),))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _router(
    endpoints: list[ModelEndpoint],
    roles: dict[str, RoleRule] | None = None,
    **kwargs: Any,
) -> tuple[EndpointRouter, _FakeClock, list[str]]:
    """确定性 Router：缺省 primary_failover（random 的行为单独测）。"""
    clock = _FakeClock()
    warnings: list[str] = []
    kwargs.setdefault("default_strategy", STRATEGY_PRIMARY_FAILOVER)
    router = EndpointRouter(
        endpoints,
        roles,
        clock=clock,
        on_warning=warnings.append,
        **kwargs,
    )
    return router, clock, warnings


class RouterModelRoutingTests(unittest.TestCase):
    """核心语义：只给模型名，路由到所有持有该模型的服务商。"""

    def test_model_resolves_every_provider_having_it(self) -> None:
        endpoints = [
            _endpoint("a", "deepseek-chat"),
            _endpoint("b", "gpt-4o"),
            _endpoint("c", "deepseek-chat"),
        ]
        router, _, _ = _router(endpoints)

        resolved = router.resolve(model="deepseek-chat")
        self.assertEqual(
            [e.spec for e in resolved],
            ["a/deepseek-chat", "c/deepseek-chat"],
        )

    def test_unknown_model_resolves_empty_with_warning(self) -> None:
        router, _, warnings = _router([_endpoint("a", "m1")])

        self.assertEqual(router.resolve(model="ghost"), [])
        self.assertFalse(router.has_candidates(model="ghost"))
        self.assertTrue(any("ghost" in w for w in warnings))

    def test_model_with_provider_pins_exact_endpoint(self) -> None:
        endpoints = [_endpoint("a", "m"), _endpoint("b", "m")]
        router, _, _ = _router(endpoints)

        resolved = router.resolve(model="m", provider="b")
        self.assertEqual([e.spec for e in resolved], ["b/m"])

    def test_pinned_endpoint_ignores_cooldown(self) -> None:
        endpoints = [_endpoint("a", "m"), _endpoint("b", "m")]
        router, _, _ = _router(endpoints)
        router.mark_failure("b/m")

        resolved = router.resolve(model="m", provider="b")
        self.assertEqual([e.spec for e in resolved], ["b/m"])

    def test_provider_without_model_resolves_empty_with_warning(self) -> None:
        router, _, warnings = _router([_endpoint("a", "m")])

        self.assertEqual(router.resolve(provider="a"), [])
        self.assertTrue(any("provider" in w for w in warnings))

    def test_model_name_containing_slash_routes(self) -> None:
        endpoints = [_endpoint("sf", "deepseek-ai/DeepSeek-V3")]
        router, _, _ = _router(endpoints)

        resolved = router.resolve(model="deepseek-ai/DeepSeek-V3")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].spec, "sf/deepseek-ai/DeepSeek-V3")

    def test_model_route_requires_capability_strictly(self) -> None:
        endpoints = [
            _endpoint("text", "m"),
            _endpoint("mm", "m", caps=("vision",)),
        ]
        router, _, _ = _router(endpoints)

        resolved = router.resolve(model="m", require=("vision",))
        self.assertEqual([e.spec for e in resolved], ["mm/m"])
        self.assertEqual(router.resolve(model="m", require=("audio",)), [])


class RouterRoleResolutionTests(unittest.TestCase):
    def test_exact_role_wins_over_default(self) -> None:
        endpoints = [_endpoint("a", "m1"), _endpoint("b", "m2")]
        roles = {
            "planner": RoleRule(targets=(RoleTarget(model="m2"),)),
            "default": RoleRule(targets=(RoleTarget(model="m1"),)),
        }
        router, _, _ = _router(endpoints, roles)

        self.assertEqual([e.spec for e in router.resolve("planner")], ["b/m2"])
        self.assertEqual([e.spec for e in router.resolve("replyer")], ["a/m1"])

    def test_builtin_fallback_takes_first_model_per_provider(self) -> None:
        endpoints = [
            _endpoint("a", "m1"),
            _endpoint("a", "m2"),
            _endpoint("b", "m3"),
        ]
        router, _, _ = _router(endpoints)

        self.assertEqual(
            [e.spec for e in router.resolve("anything")],
            ["a/m1", "b/m3"],
        )

    def test_role_model_target_expands_to_all_providers(self) -> None:
        endpoints = [
            _endpoint("a", "shared"),
            _endpoint("b", "shared"),
            _endpoint("c", "other"),
        ]
        roles = {"planner": RoleRule(targets=(RoleTarget(model="shared"),))}
        router, _, _ = _router(endpoints, roles)

        self.assertEqual(
            [e.spec for e in router.resolve("planner")],
            ["a/shared", "b/shared"],
        )

    def test_multi_target_is_fallback_chain(self) -> None:
        endpoints = [
            _endpoint("a", "primary"),
            _endpoint("b", "primary"),
            _endpoint("c", "backup"),
        ]
        roles = {
            "planner": RoleRule(
                targets=(RoleTarget(model="primary"), RoleTarget(model="backup"))
            )
        }
        router, clock, _ = _router(endpoints, roles, cooldown_base_seconds=60.0)

        self.assertEqual(
            [e.spec for e in router.resolve("planner")],
            ["a/primary", "b/primary", "c/backup"],
        )
        # 前一目标部分冷却：其可用端点仍在后一目标之前，冷却端点整体排尾
        router.mark_failure("a/primary")
        self.assertEqual(
            [e.spec for e in router.resolve("planner")],
            ["b/primary", "c/backup", "a/primary"],
        )

    def test_unknown_targets_dropped_with_warning_at_init(self) -> None:
        endpoints = [_endpoint("a", "m1")]
        roles = {
            "planner": RoleRule(
                targets=(RoleTarget(model="ghost"), RoleTarget(model="m1"))
            )
        }
        router, _, warnings = _router(endpoints, roles)

        self.assertEqual([e.spec for e in router.resolve("planner")], ["a/m1"])
        self.assertTrue(any("ghost" in w for w in warnings))

    def test_duplicate_endpoint_across_targets_deduped(self) -> None:
        endpoints = [_endpoint("a", "m"), _endpoint("b", "m")]
        roles = {
            "planner": RoleRule(
                targets=(
                    RoleTarget(model="m"),
                    RoleTarget(model="m", provider="a"),
                )
            )
        }
        router, _, _ = _router(endpoints, roles)

        self.assertEqual(
            [e.spec for e in router.resolve("planner")], ["a/m", "b/m"]
        )


class RouterRequireTests(unittest.TestCase):
    def test_builtin_fallback_filters_strictly(self) -> None:
        endpoints = [
            _endpoint("text", "m1"),
            _endpoint("mm", "m2", caps=("vision",)),
        ]
        router, _, _ = _router(endpoints)

        resolved = router.resolve("caption", require=("vision",))
        self.assertEqual([e.spec for e in resolved], ["mm/m2"])

    def test_builtin_fallback_no_match_resolves_empty(self) -> None:
        router, _, _ = _router([_endpoint("text", "m1")])

        self.assertEqual(router.resolve("caption", require=("vision",)), [])
        self.assertFalse(router.has_candidates("caption", require=("vision",)))

    def test_explicit_role_without_capability_passes_with_warning(self) -> None:
        endpoints = [_endpoint("text", "m1")]
        roles = {"caption": RoleRule(targets=(RoleTarget(model="m1"),))}
        router, _, warnings = _router(endpoints, roles)

        resolved = router.resolve("caption", require=("vision",))
        self.assertEqual([e.spec for e in resolved], ["text/m1"])
        self.assertTrue(any("放行" in w for w in warnings))

    def test_rule_level_require_unions_with_call_level(self) -> None:
        endpoints = [
            _endpoint("a", "m", caps=("vision",)),
            _endpoint("b", "m", caps=("vision", "audio")),
        ]
        roles = {
            "default": RoleRule(
                targets=(RoleTarget(model="m"),), require=frozenset({"audio"})
            )
        }
        router, _, _ = _router(endpoints, roles)

        resolved = router.resolve("x", require=("vision",))
        self.assertEqual([e.spec for e in resolved], ["b/m"])


class RouterStrategyAndCooldownTests(unittest.TestCase):
    def test_default_strategy_constant_is_random(self) -> None:
        self.assertEqual(DEFAULT_STRATEGY, STRATEGY_RANDOM)

    def test_primary_failover_keeps_config_order(self) -> None:
        endpoints = [_endpoint("a", "m"), _endpoint("b", "m")]
        router, _, _ = _router(endpoints)

        for _ in range(3):
            self.assertEqual(
                [e.spec for e in router.resolve(model="m")], ["a/m", "b/m"]
            )

    def test_cooling_endpoint_moves_to_tail_not_removed(self) -> None:
        endpoints = [_endpoint("a", "m"), _endpoint("b", "m")]
        router, clock, _ = _router(endpoints, cooldown_base_seconds=60.0)

        router.mark_failure("a/m")
        self.assertEqual(
            [e.spec for e in router.resolve(model="m")], ["b/m", "a/m"]
        )

        clock.now += 61.0  # 冷却期满自动回到原位
        self.assertEqual(
            [e.spec for e in router.resolve(model="m")], ["a/m", "b/m"]
        )

    def test_all_cooling_still_returns_candidates(self) -> None:
        endpoints = [_endpoint("a", "m"), _endpoint("b", "m")]
        router, _, _ = _router(endpoints)

        router.mark_failure("a/m")
        router.mark_failure("b/m")
        self.assertEqual(len(router.resolve(model="m")), 2)

    def test_consecutive_failures_backoff_exponentially_with_cap(self) -> None:
        router, _, _ = _router([_endpoint("a", "m")], cooldown_base_seconds=60.0)

        self.assertEqual(router.mark_failure("a/m"), 60.0)
        self.assertEqual(router.mark_failure("a/m"), 120.0)
        self.assertEqual(router.mark_failure("a/m"), 240.0)
        delay = 0.0
        for _ in range(10):
            delay = router.mark_failure("a/m")
        self.assertEqual(delay, 60.0 * 16)  # 封顶 base * 16

        router.mark_success("a/m")
        self.assertEqual(router.mark_failure("a/m"), 60.0)  # 成功清零

    def test_random_default_shuffles_providers_of_model(self) -> None:
        endpoints = [_endpoint(p, "m") for p in ("a", "b", "c")]
        clock = _FakeClock()
        # 不传 default_strategy：验证 Router 缺省即 random
        router = EndpointRouter(endpoints, rng=random.Random(42), clock=clock)

        orders = {
            tuple(e.spec for e in router.resolve(model="m")) for _ in range(20)
        }
        for order in orders:
            self.assertEqual(sorted(order), ["a/m", "b/m", "c/m"])
        self.assertGreater(len(orders), 1)

    def test_random_keeps_cooling_after_available(self) -> None:
        endpoints = [_endpoint(p, "m") for p in ("a", "b", "c")]
        clock = _FakeClock()
        router = EndpointRouter(endpoints, rng=random.Random(7), clock=clock)
        router.mark_failure("b/m")

        for _ in range(10):
            resolved = [e.spec for e in router.resolve(model="m")]
            self.assertEqual(resolved[-1], "b/m")  # 冷却端点恒在尾部

    def test_role_strategy_overrides_global_default(self) -> None:
        endpoints = [_endpoint(p, "m") for p in ("a", "b", "c")]
        roles = {
            "planner": RoleRule(
                targets=(RoleTarget(model="m"),),
                strategy=STRATEGY_PRIMARY_FAILOVER,
            )
        }
        clock = _FakeClock()
        router = EndpointRouter(
            endpoints, roles, rng=random.Random(42), clock=clock
        )  # 全局缺省 random

        for _ in range(5):
            self.assertEqual(
                [e.spec for e in router.resolve("planner")],
                ["a/m", "b/m", "c/m"],
            )

    def test_round_robin_rotates_between_calls(self) -> None:
        endpoints = [_endpoint(p, "m") for p in ("a", "b", "c")]
        roles = {
            "default": RoleRule(
                targets=(RoleTarget(model="m"),), strategy=STRATEGY_ROUND_ROBIN
            )
        }
        router, _, _ = _router(endpoints, roles)

        firsts = [router.resolve()[0].spec for _ in range(4)]
        self.assertEqual(firsts, ["a/m", "b/m", "c/m", "a/m"])

    def test_primary_model_name_has_no_strategy_side_effects(self) -> None:
        endpoints = [_endpoint("a", "m1"), _endpoint("b", "m2")]
        roles = {
            "default": RoleRule(
                targets=(RoleTarget(model="m1"), RoleTarget(model="m2")),
                strategy=STRATEGY_ROUND_ROBIN,
            )
        }
        router, _, _ = _router(endpoints, roles)

        for _ in range(5):
            self.assertEqual(router.primary_model_name(), "m1")
        # 计数器未被 primary_model_name 推进：首个 resolve 仍从 a/m1 起
        self.assertEqual(router.resolve()[0].spec, "a/m1")

    def test_unknown_default_strategy_raises(self) -> None:
        with self.assertRaises(ValueError):
            EndpointRouter([_endpoint("a", "m")], default_strategy="sticky")


class _StubClient:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[Any] = []

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.result


class RoutedChatModelTests(unittest.IsolatedAsyncioTestCase):
    def _make(
        self,
        clients: dict[str, Any],
        *,
        roles: dict[str, RoleRule] | None = None,
        endpoints: list[ModelEndpoint] | None = None,
        **model_kwargs: Any,
    ) -> tuple[RoutedChatModel, EndpointRouter, list[tuple[str, dict]], list]:
        endpoints = endpoints or [_endpoint("a", "m"), _endpoint("b", "m")]
        router, _, _ = _router(endpoints, roles)
        factory_calls: list = []
        events: list[tuple[str, dict]] = []

        def factory(endpoint: ModelEndpoint, temperature: float | None) -> Any:
            factory_calls.append((endpoint.spec, temperature))
            return clients[endpoint.spec]

        model = RoutedChatModel(
            router,
            client_factory=factory,
            on_event=lambda kind, **info: events.append((kind, info)),
            **model_kwargs,
        )
        return model, router, events, factory_calls

    async def test_success_uses_primary_and_marks_success(self) -> None:
        ok = _StubClient(result="answer")
        model, router, events, factory_calls = self._make(
            {"a/m": ok, "b/m": _StubClient(result="unused")},
            model="m",
            temperature=0.3,
        )

        result = await model.ainvoke(["msg"])

        self.assertEqual(result, "answer")
        self.assertEqual(factory_calls, [("a/m", 0.3)])
        self.assertEqual(model.model_name, "m")
        self.assertEqual(model.last_endpoint_spec, "a/m")
        self.assertEqual([kind for kind, _ in events], ["call_ok"])

    async def test_failover_to_next_provider_of_same_model(self) -> None:
        model, router, events, _ = self._make(
            {
                "a/m": _StubClient(error=RuntimeError("boom")),
                "b/m": _StubClient(result="rescued"),
            },
            model="m",
        )

        result = await model.ainvoke(["msg"])

        self.assertEqual(result, "rescued")
        self.assertEqual(model.last_endpoint_spec, "b/m")
        self.assertEqual([kind for kind, _ in events], ["call_failed", "call_ok"])
        # 首端点已进冷却：下一次解析排到尾部
        self.assertEqual(
            [e.spec for e in router.resolve(model="m")], ["b/m", "a/m"]
        )

    async def test_all_failed_reraises_last_exception(self) -> None:
        first = RuntimeError("first")
        second = RuntimeError("second")
        model, _, events, _ = self._make(
            {"a/m": _StubClient(error=first), "b/m": _StubClient(error=second)},
            model="m",
        )

        with self.assertRaises(RuntimeError) as caught:
            await model.ainvoke(["msg"])
        self.assertIs(caught.exception, second)
        self.assertEqual(
            [kind for kind, _ in events], ["call_failed", "call_failed"]
        )

    async def test_cancelled_error_propagates_without_failover(self) -> None:
        class _CancellingClient:
            async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
                raise asyncio.CancelledError()

        untouched = _StubClient(result="should-not-run")
        model, router, events, _ = self._make(
            {"a/m": _CancellingClient(), "b/m": untouched}, model="m"
        )

        with self.assertRaises(asyncio.CancelledError):
            await model.ainvoke(["msg"])

        self.assertEqual(untouched.calls, [])
        self.assertEqual(events, [])  # 不计失败：无 call_failed 事件
        self.assertEqual(
            [e.spec for e in router.resolve(model="m")], ["a/m", "b/m"]
        )  # 也不进冷却

    async def test_attempts_capped_by_max_attempts_per_call(self) -> None:
        endpoints = [_endpoint(chr(ord("a") + i), "m") for i in range(5)]
        clients = {
            e.spec: _StubClient(error=RuntimeError("down")) for e in endpoints
        }
        model, _, _, factory_calls = self._make(
            clients, endpoints=endpoints, model="m"
        )

        with self.assertRaises(RuntimeError):
            await model.ainvoke(["msg"])
        self.assertEqual(len(factory_calls), 3)  # DEFAULT_MAX_ATTEMPTS_PER_CALL

    async def test_no_candidates_raises_runtime_error(self) -> None:
        model, _, _, _ = self._make({"a/m": _StubClient()}, model="ghost")

        with self.assertRaises(RuntimeError):
            await model.ainvoke(["msg"])

    async def test_pinned_provider_only_tries_that_endpoint(self) -> None:
        pinned = _StubClient(error=RuntimeError("pinned down"))
        other = _StubClient(result="unused")
        model, _, _, factory_calls = self._make(
            {"a/m": other, "b/m": pinned}, model="m", provider="b"
        )

        with self.assertRaises(RuntimeError):
            await model.ainvoke(["msg"])
        self.assertEqual([spec for spec, _ in factory_calls], ["b/m"])
        self.assertEqual(other.calls, [])

    async def test_role_path_resolves_via_rules(self) -> None:
        endpoints = [_endpoint("a", "m1"), _endpoint("b", "m2")]
        roles = {"planner": RoleRule(targets=(RoleTarget(model="m2"),))}
        ok = _StubClient(result="planned")
        model, _, _, factory_calls = self._make(
            {"a/m1": _StubClient(), "b/m2": ok},
            roles=roles,
            endpoints=endpoints,
            role="planner",
        )

        self.assertEqual(await model.ainvoke(["msg"]), "planned")
        self.assertEqual([spec for spec, _ in factory_calls], ["b/m2"])

    async def test_model_name_before_any_call_is_primary(self) -> None:
        model, _, _, _ = self._make(
            {"a/m": _StubClient(), "b/m": _StubClient()}, model="m"
        )
        self.assertEqual(model.model_name, "m")

    async def test_on_event_exception_never_breaks_call(self) -> None:
        endpoints = [_endpoint("a", "m")]
        router, _, _ = _router(endpoints)

        def bad_event(kind: str, **info: Any) -> None:
            raise RuntimeError("observer crashed")

        model = RoutedChatModel(
            router,
            client_factory=lambda e, t: _StubClient(result="fine"),
            model="m",
            on_event=bad_event,
        )
        self.assertEqual(await model.ainvoke(["msg"]), "fine")

    async def test_kwargs_passthrough_to_client(self) -> None:
        class _KwargsClient:
            def __init__(self) -> None:
                self.kwargs: dict | None = None

            async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
                self.kwargs = kwargs
                return "ok"

        client = _KwargsClient()
        router, _, _ = _router([_endpoint("a", "m")])
        model = RoutedChatModel(
            router, client_factory=lambda e, t: client, model="m"
        )

        await model.ainvoke(["msg"], config={"tags": ["x"]})
        self.assertEqual(client.kwargs, {"config": {"tags": ["x"]}})


if __name__ == "__main__":
    unittest.main()
