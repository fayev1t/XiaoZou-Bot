"""LLM 端点注册表与按模型名路由（纯逻辑层，零三方依赖）。

把「多服务商 × 多模型 × 多把 key」收敛成一个可路由的注册表，核心语义是
**按模型名路由**：调用方只说要哪个模型（如 ``deepseek-chat``），路由器在
所有持有该模型的服务商里按策略（缺省**随机**）挑一个发请求，失败自动
切换到下一个；也可以显式钉死某个服务商（provider + model）。

- ``parse_config``：解析 ``config/model_providers.json`` 配置文档（格式见
  `config/model_providers.example.json` 与 `开发文档/v2.0/20-横切契约/LLM路由契约.md`）：
  ``providers``（服务商注册表）+ ``roles``（用途 → 模型名）+ ``settings``
  （全局策略/冷却）。
- ``EndpointRouter``：模型名索引 + role 解析 + 三策略（random /
  primary_failover / round_robin）+ 被动熔断（失败进冷却、连续失败指数
  退避、成功清零；不做主动探活）。
- ``RoutedChatModel``：暴露给调用方的「模型请求类」——只实现
  ``ainvoke``，每次调用现场解析候选端点，失败自动切换下一个。

端点标识 spec 串 ``provider/model``（服务商名不含 ``/``，模型名允许含
``/``，如 ``sf/deepseek-ai/DeepSeek-V3``）——仅用于注册表键、熔断状态与
日志；对外定位一律用 ``model`` + 可选 ``provider`` 两个字段，无歧义。

本模块**只依赖 stdlib**（不碰 pydantic / langchain / loguru），契约测试
可在本地裸环境直接跑；文件/env 读取、ChatOpenAI 构造与日志接线都留在
``qqbot/core/llm.py`` 胶水层。
"""

from __future__ import annotations

import asyncio
import json
import random as _random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

STRATEGY_PRIMARY_FAILOVER = "primary_failover"
STRATEGY_RANDOM = "random"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGIES: tuple[str, ...] = (
    STRATEGY_PRIMARY_FAILOVER,
    STRATEGY_RANDOM,
    STRATEGY_ROUND_ROBIN,
)

DEFAULT_ROLE = "default"
# 全局缺省策略：按模型名在多个服务商间随机分摊。若某 role（尤其 planner
# 这类高频长前缀场景）更在意各端点的 prompt 前缀缓存命中率，可在配置里
# 按 role 覆写为 primary_failover。
DEFAULT_STRATEGY = STRATEGY_RANDOM
DEFAULT_COOLDOWN_BASE_SECONDS = 60.0
# 连续失败的冷却按 base * 2^(n-1) 指数增长，封顶 base * 该倍数。
COOLDOWN_MAX_MULTIPLIER = 16.0
# 单次 ainvoke 至多尝试的端点数：防止主端点"慢失败"时把整条候选链的
# 延迟全叠上去（快失败场景 3 个已足够覆盖双备份）。
DEFAULT_MAX_ATTEMPTS_PER_CALL = 3


@dataclass(frozen=True)
class ModelEndpoint:
    """一个可请求的「服务商 × 模型」端点（含该服务商的 key 与调用参数）。"""

    provider: str
    model: str
    base_url: str
    api_key: str
    capabilities: frozenset[str] = frozenset()
    streaming: bool = True
    timeout_seconds: float | None = None
    max_tokens: int | None = None

    @property
    def spec(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class RoleTarget:
    """role 的一个候选目标：模型名，可选钉死到某个服务商。"""

    model: str
    provider: str | None = None


@dataclass(frozen=True)
class RoleRule:
    """一个用途（role）的路由规则：目标序列 + 策略覆写 + 能力硬要求。

    ``targets`` 是优先级递减的回退链：先在 targets[0] 的服务商里选，
    全部不可用才轮到 targets[1]，以此类推。``strategy=None`` 表示用
    Router 的全局缺省策略。
    """

    targets: tuple[RoleTarget, ...]
    strategy: str | None = None
    require: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RoutingConfig:
    """``config/model_providers.json`` 解析结果。"""

    endpoints: tuple[ModelEndpoint, ...]
    roles: dict[str, RoleRule]
    default_strategy: str = DEFAULT_STRATEGY
    cooldown_seconds: float = DEFAULT_COOLDOWN_BASE_SECONDS


# ────────────────────────── 配置解析 ──────────────────────────


def _parse_capabilities(value: Any, ctx: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{ctx}.capabilities 必须是非空字符串数组")
    return frozenset(item.strip().lower() for item in value)


def _required_str(obj: Mapping[str, Any], key: str, ctx: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ctx}.{key} 必须是非空字符串")
    return value.strip()


def _optional_str(obj: Mapping[str, Any], key: str, ctx: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ctx}.{key} 必须是非空字符串")
    return value.strip()


def _optional_positive_number(
    obj: Mapping[str, Any], key: str, ctx: str
) -> float | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{ctx}.{key} 必须是正数")
    return float(value)


def _optional_positive_int(obj: Mapping[str, Any], key: str, ctx: str) -> int | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{ctx}.{key} 必须是正整数")
    return value


def _optional_bool(obj: Mapping[str, Any], key: str, ctx: str, default: bool) -> bool:
    value = obj.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{ctx}.{key} 必须是布尔值")
    return value


def _optional_strategy(obj: Mapping[str, Any], key: str, ctx: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if value not in STRATEGIES:
        raise ValueError(
            f"{ctx}.{key} 必须是 {'/'.join(STRATEGIES)} 之一：{value!r}"
        )
    return value


def _parse_provider_items(items: list[Any], ctx: str) -> tuple[ModelEndpoint, ...]:
    endpoints: list[ModelEndpoint] = []
    seen_names: set[str] = set()
    seen_specs: set[str] = set()
    for index, item in enumerate(items):
        item_ctx = f"{ctx}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_ctx} 必须是对象")
        if not _optional_bool(item, "enabled", item_ctx, True):
            continue

        name = _required_str(item, "name", item_ctx)
        if "/" in name or any(ch.isspace() for ch in name):
            raise ValueError(f"{item_ctx}.name 不能含 '/' 或空白字符：{name!r}")
        if name in seen_names:
            raise ValueError(f"服务商名重复：{name!r}")
        seen_names.add(name)

        base_url = _required_str(item, "base_url", item_ctx)
        api_key = _required_str(item, "api_key", item_ctx)
        provider_caps = _parse_capabilities(item.get("capabilities"), item_ctx)
        streaming = _optional_bool(item, "streaming", item_ctx, True)
        timeout_seconds = _optional_positive_number(item, "timeout", item_ctx)
        max_tokens = _optional_positive_int(item, "max_tokens", item_ctx)

        models = item.get("models")
        if not isinstance(models, list) or not models:
            raise ValueError(f"{item_ctx}.models 必须是非空数组")
        for model_index, entry in enumerate(models):
            model_ctx = f"{item_ctx}.models[{model_index}]"
            if isinstance(entry, str):
                model_name = entry.strip()
                if not model_name:
                    raise ValueError(f"{model_ctx} 不能是空字符串")
                model_caps = provider_caps
            elif isinstance(entry, dict):
                model_name = _required_str(entry, "name", model_ctx)
                model_caps = provider_caps | _parse_capabilities(
                    entry.get("capabilities"), model_ctx
                )
            else:
                raise ValueError(f"{model_ctx} 必须是字符串或对象")

            endpoint = ModelEndpoint(
                provider=name,
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                capabilities=model_caps,
                streaming=streaming,
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
            )
            if endpoint.spec in seen_specs:
                raise ValueError(f"端点重复：{endpoint.spec!r}")
            seen_specs.add(endpoint.spec)
            endpoints.append(endpoint)

    if not endpoints:
        raise ValueError(
            f"{ctx} 解析后没有任何可用端点（全部被 enabled=false 跳过？）"
        )
    return tuple(endpoints)


def _parse_role_target(value: Any, ctx: str) -> RoleTarget:
    if isinstance(value, str):
        model = value.strip()
        if not model:
            raise ValueError(f"{ctx} 不能是空字符串")
        return RoleTarget(model=model)
    if isinstance(value, dict):
        return RoleTarget(
            model=_required_str(value, "model", ctx),
            provider=_optional_str(value, "provider", ctx),
        )
    raise ValueError(f"{ctx} 必须是模型名字符串或 {{model, provider}} 对象")


def _parse_roles_value(value: Any) -> dict[str, RoleRule]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("roles 必须是 JSON 对象")

    roles: dict[str, RoleRule] = {}
    for role, entry in value.items():
        if not role.strip():
            raise ValueError("roles 的 role 名不能为空")
        ctx = f"roles[{role!r}]"
        if isinstance(entry, str):
            rule = RoleRule(targets=(_parse_role_target(entry, ctx),))
        elif isinstance(entry, dict):
            rule = RoleRule(
                targets=(_parse_role_target(entry, ctx),),
                strategy=_optional_strategy(entry, "strategy", ctx),
                require=_parse_capabilities(entry.get("require"), ctx),
            )
        elif isinstance(entry, list):
            if not entry:
                raise ValueError(f"{ctx} 不能是空数组")
            rule = RoleRule(
                targets=tuple(
                    _parse_role_target(item, f"{ctx}[{i}]")
                    for i, item in enumerate(entry)
                )
            )
        else:
            raise ValueError(f"{ctx} 必须是字符串、对象或数组")
        roles[role.strip()] = rule
    return roles


def parse_config(raw: str) -> RoutingConfig:
    """解析 ``config/model_providers.json`` 全文档。

    顶层结构 ``{"providers": [...], "roles": {...}, "settings": {...}}``，
    仅 ``providers`` 必填。非法配置一律 raise ValueError（含
    JSONDecodeError），带上下文定位；未知字段忽略（向前兼容）。
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            "LLM 配置必须是 JSON 对象：{providers: [...], roles?, settings?}"
        )

    providers = data.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("providers 必须是非空数组")
    endpoints = _parse_provider_items(providers, "providers")

    roles = _parse_roles_value(data.get("roles"))
    # role 钉死的服务商必须在注册表里（模型名的存在性由 Router 兜底校验，
    # 因为它还要容忍"模型在但都冷却"这类运行期状态）。
    known_providers = {endpoint.provider for endpoint in endpoints}
    for role, rule in roles.items():
        for target in rule.targets:
            if target.provider is not None and target.provider not in known_providers:
                raise ValueError(
                    f"roles[{role!r}] 钉死的服务商不存在：{target.provider!r}"
                )

    settings = data.get("settings")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("settings 必须是 JSON 对象")
    strategy = _optional_strategy(settings, "strategy", "settings") or DEFAULT_STRATEGY
    cooldown = (
        _optional_positive_number(settings, "cooldown_seconds", "settings")
        or DEFAULT_COOLDOWN_BASE_SECONDS
    )

    return RoutingConfig(
        endpoints=endpoints,
        roles=roles,
        default_strategy=strategy,
        cooldown_seconds=cooldown,
    )


def collect_api_keys(raw: str) -> tuple[tuple[str, str], ...]:
    """从配置原文尽力提取 ``(服务商名, api_key)`` 对。

    兼容两种形态：完整配置文档（``{"providers": [...]}``）与裸服务商数组。
    供 prompt 快照脱敏使用：**永不 raise**——配置再烂也不能反过来把
    脱敏环节炸掉；解析不了就返回空元组（此时也不会有请求发出去）。
    """
    try:
        data = json.loads(raw)
    except Exception:
        return ()
    if isinstance(data, dict):
        data = data.get("providers")
    if not isinstance(data, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        api_key = item.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            continue
        name = item.get("name")
        label = (
            name.strip()
            if isinstance(name, str) and name.strip()
            else f"#{index}"
        )
        pairs.append((label, api_key.strip()))
    return tuple(pairs)


# ────────────────────────── 路由器 ──────────────────────────


class EndpointRouter:
    """端点注册表 + 按模型名/role 路由 + 被动熔断（进程内共享一个实例）。

    定位一次调用的候选端点有三种入口（``resolve`` 参数）：

    - ``model="deepseek-chat"``：所有持有该模型的服务商（用户核心语义：
      只给模型名，路由器自己挑服务商）；
    - ``model + provider``：显式钉死某服务商的某模型，无视策略与冷却；
    - ``role="planner"``：查 role 规则表——精确命中 → ``"default"`` 键
      → 内置兜底（注册表顺序下每个服务商的第一个模型）。规则的 targets
      是回退链：前一个目标的服务商全试完才轮到下一个。

    ``require``（能力硬要求，如 caption 的 vision）语义：
    - 规则来自**精确命中**（用户显式为该 role 配置）：过滤后为空时警告
      并按显式配置放行——信任用户比静默拒发更不坑；
    - 其余入口（default/内置兜底/直接按模型名）：严格过滤，为空即无候选
      （fail loudly）。

    策略缺省 ``random``（同模型多服务商随机分摊）；role 可覆写。冷却是
    被动熔断：``mark_failure`` 后该端点冷却 base * 2^(n-1) 秒（n 为连续
    失败次数，封顶 base * cooldown_max_multiplier），``mark_success``
    清零。冷却中的端点排到候选序尾部而非剔除——全员冷却时宁可重试也
    不无脑拒绝。
    """

    def __init__(
        self,
        endpoints: Iterable[ModelEndpoint],
        roles: Mapping[str, RoleRule] | None = None,
        *,
        default_strategy: str = DEFAULT_STRATEGY,
        cooldown_base_seconds: float = DEFAULT_COOLDOWN_BASE_SECONDS,
        cooldown_max_multiplier: float = COOLDOWN_MAX_MULTIPLIER,
        max_attempts_per_call: int = DEFAULT_MAX_ATTEMPTS_PER_CALL,
        rng: _random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_warning: Callable[[str], None] | None = None,
    ) -> None:
        if default_strategy not in STRATEGIES:
            raise ValueError(f"未知策略：{default_strategy!r}")
        self._endpoints: dict[str, ModelEndpoint] = {}
        self._by_model: dict[str, list[str]] = {}
        for endpoint in endpoints:
            if endpoint.spec in self._endpoints:
                raise ValueError(f"端点重复：{endpoint.spec!r}")
            self._endpoints[endpoint.spec] = endpoint
            self._by_model.setdefault(endpoint.model, []).append(endpoint.spec)

        self._warn = on_warning or (lambda message: None)
        self._roles: dict[str, RoleRule] = {}
        for role, rule in (roles or {}).items():
            kept: list[RoleTarget] = []
            for target in rule.targets:
                if target.provider is not None:
                    known = f"{target.provider}/{target.model}" in self._endpoints
                else:
                    known = target.model in self._by_model
                if known:
                    kept.append(target)
                else:
                    self._warn(
                        f"role {role!r} 的目标不在注册表内，已忽略："
                        f"provider={target.provider!r} model={target.model!r}"
                    )
            if not kept:
                self._warn(f"role {role!r} 剔除未知目标后没有任何候选")
            self._roles[role] = RoleRule(
                targets=tuple(kept), strategy=rule.strategy, require=rule.require
            )

        self._default_strategy = default_strategy
        self.max_attempts_per_call = max_attempts_per_call
        self._cooldown_base = float(cooldown_base_seconds)
        self._cooldown_cap = float(cooldown_max_multiplier)
        self._rng = rng if rng is not None else _random.Random()
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._rr_counters: dict[str, int] = {}

    # ── 查询 ──

    def endpoint(self, spec: str) -> ModelEndpoint | None:
        return self._endpoints.get(spec)

    def has_candidates(
        self,
        role: str = DEFAULT_ROLE,
        *,
        model: str | None = None,
        provider: str | None = None,
        require: Sequence[str] = (),
    ) -> bool:
        groups, _ = self._target_groups(role, model, provider, require)
        return any(groups)

    def primary_model_name(
        self,
        role: str = DEFAULT_ROLE,
        *,
        model: str | None = None,
        provider: str | None = None,
        require: Sequence[str] = (),
    ) -> str | None:
        """首个候选端点的模型名（配置顺序、不动策略计数器，无副作用）。"""
        groups, _ = self._target_groups(role, model, provider, require)
        for group in groups:
            if group:
                return group[0].model
        return None

    # ── 路由 ──

    def resolve(
        self,
        role: str = DEFAULT_ROLE,
        *,
        model: str | None = None,
        provider: str | None = None,
        require: Sequence[str] = (),
    ) -> list[ModelEndpoint]:
        """一次调用的有序尝试列表。``model+provider`` 钉死时无视策略与冷却。"""
        groups, strategy_override = self._target_groups(role, model, provider, require)
        if not any(groups):
            return []
        if model is not None and provider is not None:
            return [endpoint for group in groups for endpoint in group]
        strategy = strategy_override or self._default_strategy
        rr_key = role if model is None else f"model::{model}"
        return self._order(rr_key, strategy, groups)

    def _rule_for(self, role: str) -> tuple[RoleRule, bool]:
        rule = self._roles.get(role)
        if rule is not None:
            return rule, True
        rule = self._roles.get(DEFAULT_ROLE)
        if rule is not None:
            return rule, False
        return self._builtin_rule(), False

    def _builtin_rule(self) -> RoleRule:
        """无任何 role 配置时的兜底：每个服务商的第一个模型，注册表顺序。"""
        seen: set[str] = set()
        targets: list[RoleTarget] = []
        for endpoint in self._endpoints.values():
            if endpoint.provider in seen:
                continue
            seen.add(endpoint.provider)
            targets.append(
                RoleTarget(model=endpoint.model, provider=endpoint.provider)
            )
        return RoleRule(targets=tuple(targets))

    def _expand_target(self, target: RoleTarget) -> list[ModelEndpoint]:
        if target.provider is not None:
            endpoint = self._endpoints.get(f"{target.provider}/{target.model}")
            return [endpoint] if endpoint is not None else []
        return [
            self._endpoints[spec] for spec in self._by_model.get(target.model, ())
        ]

    def _target_groups(
        self,
        role: str,
        model: str | None,
        provider: str | None,
        require: Sequence[str],
    ) -> tuple[list[list[ModelEndpoint]], str | None]:
        """候选端点分组（组间是回退优先级，组内配置顺序）+ 策略覆写。

        无副作用（不推进计数器、不掷随机数），供 resolve /
        has_candidates / primary_model_name 共用。
        """
        need = frozenset(item.strip().lower() for item in require if item.strip())
        explicit = False

        if provider is not None and model is None:
            self._warn("provider 只能与 model 一起指定，已忽略该定位")
            return [], None

        if model is not None:
            target = RoleTarget(model=model.strip(), provider=provider)
            groups = [self._expand_target(target)]
            if not groups[0]:
                self._warn(
                    f"注册表内没有匹配端点：model={model!r} provider={provider!r}"
                )
            strategy_override: str | None = None
        else:
            rule, explicit = self._rule_for(role)
            need = need | rule.require
            seen_specs: set[str] = set()
            groups = []
            for target in rule.targets:
                group = [
                    endpoint
                    for endpoint in self._expand_target(target)
                    if endpoint.spec not in seen_specs
                ]
                seen_specs.update(endpoint.spec for endpoint in group)
                groups.append(group)
            strategy_override = rule.strategy

        if need:
            filtered = [
                [e for e in group if need <= e.capabilities] for group in groups
            ]
            if any(filtered):
                groups = filtered
            elif explicit and any(groups):
                self._warn(
                    f"role {role!r} 的显式候选均不具备能力 {sorted(need)}，"
                    "按显式配置放行"
                )
            else:
                groups = []
        return groups, strategy_override

    def _order(
        self, rr_key: str, strategy: str, groups: list[list[ModelEndpoint]]
    ) -> list[ModelEndpoint]:
        """组间保持回退优先级（前组的可用端点恒在后组之前），组内按策略排；
        冷却端点整体挪到所有可用端点之后。"""
        now = self._clock()
        available: list[ModelEndpoint] = []
        cooling: list[ModelEndpoint] = []
        for group in groups:
            group_available: list[ModelEndpoint] = []
            group_cooling: list[ModelEndpoint] = []
            for endpoint in group:
                if self._cooldown_until.get(endpoint.spec, 0.0) <= now:
                    group_available.append(endpoint)
                else:
                    group_cooling.append(endpoint)
            if strategy == STRATEGY_RANDOM:
                self._rng.shuffle(group_available)
                self._rng.shuffle(group_cooling)
            available.extend(group_available)
            cooling.extend(group_cooling)

        if strategy == STRATEGY_ROUND_ROBIN and available:
            counter = self._rr_counters.get(rr_key, 0)
            self._rr_counters[rr_key] = counter + 1
            offset = counter % len(available)
            available = available[offset:] + available[:offset]
        return available + cooling

    # ── 熔断状态 ──

    def mark_failure(self, spec: str) -> float:
        """记一次失败，返回本次进入的冷却秒数（供日志）。"""
        count = self._failures.get(spec, 0) + 1
        self._failures[spec] = count
        delay = min(
            self._cooldown_base * (2.0 ** (count - 1)),
            self._cooldown_base * self._cooldown_cap,
        )
        self._cooldown_until[spec] = self._clock() + delay
        return delay

    def mark_success(self, spec: str) -> None:
        self._failures.pop(spec, None)
        self._cooldown_until.pop(spec, None)


# ────────────────────────── 模型请求类 ──────────────────────────


class RoutedChatModel:
    """路由化聊天模型：对外只承诺 ``ainvoke(messages, **kwargs)``。

    每次 ainvoke 现场向 Router 解析候选端点（冷却/熔断状态全进程共享），
    逐个尝试至多 ``router.max_attempts_per_call`` 个：单个端点失败记熔断
    并切下一个，全部失败重抛**最后一个**异常（调用方各自的异常处理
    ——planner 的 idle 回退 / ReplyerError / CaptionError——原样生效）。

    ``asyncio.CancelledError`` 立即透传且不计失败：取消不是端点的错；
    外层 ``asyncio.wait_for`` 的超时预算覆盖的是整条切换链（Replyer 的
    12s 内切不完就整体超时，不会偷偷延长）。

    ``model_name`` 是 best-effort 观测标注（prompt 快照 / 日志用）：
    有过成功调用后是最近一次实际使用的模型，否则是首选端点的模型。
    """

    def __init__(
        self,
        router: EndpointRouter,
        *,
        client_factory: Callable[[ModelEndpoint, float | None], Any],
        role: str = DEFAULT_ROLE,
        model: str | None = None,
        provider: str | None = None,
        require: Sequence[str] = (),
        temperature: float | None = None,
        on_event: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._router = router
        self._client_factory = client_factory
        self._role = role
        self._model = model
        self._provider = provider
        self._require = tuple(require)
        self._temperature = temperature
        self._on_event = on_event
        self._clock = clock
        self._last_endpoint: ModelEndpoint | None = None

    @property
    def model_name(self) -> str | None:
        if self._last_endpoint is not None:
            return self._last_endpoint.model
        try:
            return self._router.primary_model_name(
                self._role,
                model=self._model,
                provider=self._provider,
                require=self._require,
            )
        except Exception:
            return None

    @property
    def last_endpoint_spec(self) -> str | None:
        return self._last_endpoint.spec if self._last_endpoint else None

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        candidates = self._router.resolve(
            self._role,
            model=self._model,
            provider=self._provider,
            require=self._require,
        )
        if not candidates:
            raise RuntimeError(
                f"role={self._role!r} model={self._model!r} "
                f"provider={self._provider!r} 没有可用的 LLM 端点"
                "（检查 config/model_providers.json 配置与启动日志）"
            )

        last_exc: Exception | None = None
        for endpoint in candidates[: self._router.max_attempts_per_call]:
            started = self._clock()
            try:
                client = self._client_factory(endpoint, self._temperature)
                result = await client.ainvoke(messages, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cooldown = self._router.mark_failure(endpoint.spec)
                self._emit(
                    "call_failed",
                    endpoint=endpoint.spec,
                    role=self._role,
                    latency_ms=self._elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}"[:300],
                    cooldown_seconds=cooldown,
                )
                last_exc = exc
                continue
            self._router.mark_success(endpoint.spec)
            self._last_endpoint = endpoint
            self._emit(
                "call_ok",
                endpoint=endpoint.spec,
                role=self._role,
                latency_ms=self._elapsed_ms(started),
            )
            return result

        if last_exc is None:  # max_attempts_per_call 被配成 0 之类的病态情形
            raise RuntimeError("未尝试任何 LLM 端点（max_attempts_per_call<1?）")
        raise last_exc

    def _elapsed_ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)

    def _emit(self, kind: str, **info: Any) -> None:
        """观测回调永不反噬调用链：异常一律吞掉。"""
        if self._on_event is None:
            return
        try:
            self._on_event(kind, **info)
        except Exception:
            pass
