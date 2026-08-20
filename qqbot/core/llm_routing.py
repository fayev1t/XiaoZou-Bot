"""LLM 端点注册表与按模型名路由（纯逻辑层，零三方依赖）。

把「多服务商 × 多模型 × 多把 key」收敛成一个可路由的注册表，核心语义是
**按模型名路由**：调用方只说要哪个模型（如 ``deepseek-chat``），路由器在
所有持有该模型的服务商里按策略（缺省**随机**）挑一个发请求，失败自动
切换到下一个；也可以显式钉死某个服务商（provider + model）。

- ``parse_config``：解析 ``config/model_providers.json`` 配置文档（格式见
  `config/model_providers.example.json` 与
  `开发文档/v2.0/20-横切契约/LLM路由契约.md`）：
  ``providers``（服务商注册表，每个 ``models`` 条目 = 一个端点，可用
  ``upstream_model`` 把它变成**别名**并带上自己的采样/透传参数）+
  ``groups``（命名模型组：有序回退 / 随机或轮询池）+ ``roles``（用途 →
  模型名或 ``group`` 引用；**不含任何采样参数**）+ ``settings``（全局
  策略/冷却/采样缺省——2026-07-29 起 ``temperature``/``max_tokens`` 全局
  缺省也在这里配，.env 不再有 LLM 键）。
- ``EndpointRouter``：模型名索引 + role 解析 + 三策略（random /
  primary_failover / round_robin）+ 被动熔断（失败进冷却、连续失败指数
  退避、成功清零；不做主动探活）。
- ``RoutedChatModel``：暴露给调用方的「模型请求类」——只实现
  ``ainvoke``，每次调用现场解析候选端点，失败自动切换下一个；调用方发现
  「返回了但内容不可用」时用 ``mark_last_call_failed`` 把它补记成端点失败。

端点标识 spec 串 ``provider/model``（服务商名不含 ``/``，模型名允许含
``/``，如 ``sf/deepseek-ai/DeepSeek-V3``）——仅用于注册表键、熔断状态与
日志；对外定位一律用 ``model`` + 可选 ``provider`` 两个字段，无歧义。
这里的 ``model`` 是端点的**路由身份**，可以是本地别名（``grok-4.5-xhigh``），
不必等于发给上游的模型名——于是同一上游模型的不同调用档位天然是两个端点，
各有各的冷却计数器与客户端。

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
# 全局采样温度缺省（2026-07-29 自 .env 的 LLM_TEMPERATURE 收拢进
# settings.temperature）。2026-08-14 起解析链只剩两级：端点声明 >
# settings.temperature > 本缺省——role 不再参与采样。
DEFAULT_TEMPERATURE = 0.7

# ``params`` 里禁止出现的键：它们要么由本层自己组装（model/messages/stream），
# 要么有专用端点字段（temperature/max_tokens/timeout）。允许它们混进透传字典
# 会造出第二个真相源——同一个参数两个地方能配、优先级只能靠记，正是
# 2026-07-28/29 两次收拢要消灭的东西。parse 期 raise 并指路到专用字段。
RESERVED_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "model_name",
        "messages",
        "stream",
        "streaming",
        "temperature",
        "max_tokens",
        "timeout",
    }
)

# roles 里已退役的键：采样参数 2026-08-14 起只在端点上声明。这几个键**不**走
# 「未知字段忽略（向前兼容）」——未知字段是没见过的，退役字段是曾经生效过的，
# 静默吞掉等于配置文件在撒谎（照着 roles 段的形状再写一次不生效，还得翻代码）。
RETIRED_ROLE_KEYS: tuple[str, ...] = ("temperature",)


@dataclass(frozen=True)
class ModelEndpoint:
    """一个可请求的端点：**模型别名** + 上游模型 + 该次调用的全部参数。

    ``model`` 是**路由身份**——roles / groups 引用的名字、注册表键、熔断状态键
    与日志里的 spec 都用它，不必等于发给上游的模型名。``upstream_model`` 省略
    时两者相同；填了就表示 ``model`` 是本地别名（2026-08-14）。

    别名的用途是把「同一个上游模型的不同调用档位」表达成**两个可路由的名字**，
    典型是思考等级：``grok-4.5-xhigh`` 与 ``grok-4.5`` 指向同一个
    ``upstream_model``，只是 ``params`` 里的 ``reasoning_effort`` 不同。这样
    思考等级不需要任何新的路由概念——groups 的加权槽位、role 回退链、按端点
    独立的冷却计数器全部原样复用。网关侧已经把档位烘进模型名的（如
    ``gemini-3.6-flash-high``）就是不带 ``upstream_model`` 的普通条目，两种
    形态在路由层完全同构。

    采样参数（``temperature`` / ``max_tokens`` / ``timeout_seconds`` /
    ``streaming``）全部在这里定死，``None`` 表示回落 ``settings`` 全局缺省。
    ``params`` 是厂商特有的透传参数（``reasoning_effort`` / ``enable_thinking``
    / token 预算……），排序元组形态以保持 dataclass 可哈希。
    """

    provider: str
    model: str
    base_url: str
    api_key: str
    capabilities: frozenset[str] = frozenset()
    streaming: bool = True
    timeout_seconds: float | None = None
    max_tokens: int | None = None
    upstream_model: str | None = None
    temperature: float | None = None
    params: tuple[tuple[str, Any], ...] = ()

    @property
    def spec(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def wire_model(self) -> str:
        """实际发给上游的模型名（别名解析后）。"""
        return self.upstream_model or self.model


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

    **role 不再携带任何采样参数**（2026-08-14）：温度随 max_tokens / timeout /
    厂商透传参数一起落到端点（模型别名）上。role 只回答「用哪些模型、按什么
    顺序」，「怎么调这个模型」由别名自己声明——同一模型要两种温度就注册两个
    别名，与思考等级的表达方式一致。

    ``flat_pool=True``：组内模型展开后并成**一个**排序组再套 strategy
    （命名 ``groups`` 里 ``random`` / ``round_robin`` 的语义）；``False``
    保持 targets 回退链（``primary_failover`` 与裸 ``targets`` 默认）。
    """

    targets: tuple[RoleTarget, ...]
    strategy: str | None = None
    require: frozenset[str] = frozenset()
    flat_pool: bool = False


@dataclass(frozen=True)
class ModelGroup:
    """命名模型组（``config/model_providers.json`` 顶层 ``groups``）。

    ``strategy=primary_failover``：``models`` 顺序是回退链（保缓存友好）。
    ``random`` / ``round_robin``：``models`` 组成扁平池后组内洗牌或轮转。
    """

    models: tuple[RoleTarget, ...]
    strategy: str


@dataclass(frozen=True)
class RoutingConfig:
    """``config/model_providers.json`` 解析结果。"""

    endpoints: tuple[ModelEndpoint, ...]
    roles: dict[str, RoleRule]
    groups: dict[str, ModelGroup] | None = None
    default_strategy: str = DEFAULT_STRATEGY
    cooldown_seconds: float = DEFAULT_COOLDOWN_BASE_SECONDS
    cooldown_max_multiplier: float = COOLDOWN_MAX_MULTIPLIER
    max_attempts_per_call: int = DEFAULT_MAX_ATTEMPTS_PER_CALL
    # 全局采样缺省（原 .env 的 LLM_TEMPERATURE / LLM_MAX_TOKENS）：端点自身
    # 没声明该参数时的兜底，2026-08-14 起两者语义一致（max_tokens 一直如此，
    # temperature 这次从「role 兜底」改成「端点兜底」）。None = 不限制。
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int | None = None


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


def _optional_non_negative_number(
    obj: Mapping[str, Any], key: str, ctx: str
) -> float | None:
    """温度专用：0 是合法采样温度，不能复用「正数」校验。"""
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{ctx}.{key} 必须是非负数")
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


def _parse_params(
    value: Any,
    ctx: str,
    *,
    base: tuple[tuple[str, Any], ...] = (),
) -> tuple[tuple[str, Any], ...]:
    """厂商特有的固定附加请求参数（``reasoning_effort`` / ``enable_thinking`` …）。

    provider 级作缺省，模型级**合并**（同名键覆盖）——与 ``capabilities`` 的并集、
    以及标量字段的整体覆盖都不同：provider 声明大家共用的键，别名只覆写自己那
    一档的值。

    值域限死 JSON 标量：``ModelEndpoint`` 是 ``frozen=True``（``capabilities``
    用 frozenset 就是为了可哈希），嵌套容器会让 ``hash(endpoint)`` 直接炸；
    而且透传字典没有类型校验，``"temperature": "hot"`` 这类错误只会在上游变成
    一个 400，parse 期拦住才有意义。返回按键排序的元组以保持可哈希。
    """
    merged: dict[str, Any] = dict(base)
    if value is None:
        return tuple(sorted(merged.items()))
    if not isinstance(value, dict):
        raise ValueError(f"{ctx}.params 必须是 JSON 对象")
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"{ctx}.params 的键必须是非空字符串")
        key = raw_key.strip()
        if key in RESERVED_PARAM_KEYS:
            raise ValueError(
                f"{ctx}.params.{key} 与端点专用字段冲突：直接写 {ctx}.{key}"
                "（params 只放厂商特有的透传参数）"
            )
        if raw_value is None or not isinstance(raw_value, (bool, int, float, str)):
            raise ValueError(
                f"{ctx}.params.{key} 必须是字符串 / 数字 / 布尔值（不支持嵌套）"
            )
        merged[key] = raw_value
    return tuple(sorted(merged.items()))


def _parse_provider_items(items: list[Any], ctx: str) -> tuple[ModelEndpoint, ...]:
    """服务商注册表 → 端点列表。

    每个 ``models`` 元素展开成一个端点。字符串形态沿用 provider 级的全部调用
    参数；对象形态可逐项覆写，并可用 ``upstream_model`` 把该条目变成**别名**
    （见 ``ModelEndpoint``）。继承语义三种，互不相同，改这里时留意：
    ``capabilities`` 并集、标量字段模型覆盖 provider、``params`` 合并且模型
    级同名键覆盖。
    """
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
        temperature = _optional_non_negative_number(item, "temperature", item_ctx)
        params = _parse_params(item.get("params"), item_ctx)

        models = item.get("models")
        if not isinstance(models, list) or not models:
            raise ValueError(f"{item_ctx}.models 必须是非空数组")
        for model_index, entry in enumerate(models):
            model_ctx = f"{item_ctx}.models[{model_index}]"
            if isinstance(entry, str):
                model_name = entry.strip()
                if not model_name:
                    raise ValueError(f"{model_ctx} 不能是空字符串")
                endpoint = ModelEndpoint(
                    provider=name,
                    model=model_name,
                    base_url=base_url,
                    api_key=api_key,
                    capabilities=provider_caps,
                    streaming=streaming,
                    timeout_seconds=timeout_seconds,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    params=params,
                )
            elif isinstance(entry, dict):
                model_name = _required_str(entry, "name", model_ctx)
                model_temperature = _optional_non_negative_number(
                    entry, "temperature", model_ctx
                )
                model_timeout = _optional_positive_number(entry, "timeout", model_ctx)
                model_max_tokens = _optional_positive_int(
                    entry, "max_tokens", model_ctx
                )
                endpoint = ModelEndpoint(
                    provider=name,
                    model=model_name,
                    base_url=base_url,
                    api_key=api_key,
                    capabilities=provider_caps
                    | _parse_capabilities(entry.get("capabilities"), model_ctx),
                    streaming=_optional_bool(entry, "streaming", model_ctx, streaming),
                    timeout_seconds=(
                        timeout_seconds if model_timeout is None else model_timeout
                    ),
                    max_tokens=(
                        max_tokens if model_max_tokens is None else model_max_tokens
                    ),
                    upstream_model=_optional_str(entry, "upstream_model", model_ctx),
                    # 0 是合法温度，不能用 `or` 链回落
                    # （见 _optional_non_negative_number）
                    temperature=(
                        temperature if model_temperature is None else model_temperature
                    ),
                    params=_parse_params(entry.get("params"), model_ctx, base=params),
                )
            else:
                raise ValueError(f"{model_ctx} 必须是字符串或对象")

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


def _parse_groups_value(value: Any) -> dict[str, ModelGroup]:
    """顶层 ``groups``：独立命名模型池，与 role 无绑定。

    role 的目标可以填**组名**或**模型名**（字符串），也可对象里
    ``"group": "<组名>"``。组内 ``models`` **允许同一模型出现多次**，
    在 ``random`` / ``round_robin`` 下按出现次数加权。

    每组：``{"models": [...], "strategy": "primary_failover"|"random"|"round_robin"}``。
    ``strategy`` 缺省 ``primary_failover``（有序回退，缓存友好）。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("groups 必须是 JSON 对象")
    groups: dict[str, ModelGroup] = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("groups 的组名不能为空")
        ctx = f"groups[{name!r}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{ctx} 必须是对象")
        models_raw = entry.get("models")
        if not isinstance(models_raw, list) or not models_raw:
            raise ValueError(f"{ctx}.models 必须是非空数组")
        # 不去重：重复条目 = 加权槽位
        models = tuple(
            _parse_role_target(item, f"{ctx}.models[{i}]")
            for i, item in enumerate(models_raw)
        )
        strategy = (
            _optional_strategy(entry, "strategy", ctx) or STRATEGY_PRIMARY_FAILOVER
        )
        groups[name.strip()] = ModelGroup(models=models, strategy=strategy)
    return groups


def _role_rule_from_group(
    group: ModelGroup,
    *,
    strategy_override: str | None = None,
    require: frozenset[str] = frozenset(),
) -> RoleRule:
    strategy = strategy_override or group.strategy
    return RoleRule(
        targets=group.models,
        strategy=strategy,
        require=require,
        flat_pool=strategy in (STRATEGY_RANDOM, STRATEGY_ROUND_ROBIN),
    )


def _parse_roles_value(
    value: Any, groups: Mapping[str, ModelGroup]
) -> dict[str, RoleRule]:
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
            # 字符串：先当组名，否则当模型名（组与模型不得同名，见 parse_config）
            name = entry.strip()
            if not name:
                raise ValueError(f"{ctx} 不能是空字符串")
            if name in groups:
                rule = _role_rule_from_group(groups[name])
            else:
                rule = RoleRule(targets=(RoleTarget(model=name),))
        elif isinstance(entry, dict):
            rule = _parse_role_object(entry, ctx, groups)
        elif isinstance(entry, list):
            if not entry:
                raise ValueError(f"{ctx} 不能是空数组")
            # 数组仍是模型回退链（元素不解析组名，避免隐式展开）
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


def _parse_role_object(
    entry: Mapping[str, Any],
    ctx: str,
    groups: Mapping[str, ModelGroup],
) -> RoleRule:
    """role 对象：``model`` / ``targets`` / ``group`` 三选一 + 可选覆写。

    ``group`` 只表示「用这个独立组」；组本身不绑定任何 role。
    """
    _reject_retired_role_keys(entry, ctx)

    has_model = "model" in entry
    has_targets = "targets" in entry
    has_group = "group" in entry
    kinds = sum(bool(flag) for flag in (has_model, has_targets, has_group))
    if kinds > 1:
        raise ValueError(f"{ctx} 的 model / targets / group 互斥，只能配一个")
    if kinds == 0:
        raise ValueError(f"{ctx} 必须配置 model、targets 或 group 之一")

    require = _parse_capabilities(entry.get("require"), ctx)
    strategy_override = _optional_strategy(entry, "strategy", ctx)

    if has_group:
        if "provider" in entry:
            raise ValueError(f"{ctx}.provider 只能与 model 搭配（不能与 group 并用）")
        group_name = entry["group"]
        if not isinstance(group_name, str) or not group_name.strip():
            raise ValueError(f"{ctx}.group 必须是非空字符串")
        group_name = group_name.strip()
        if group_name not in groups:
            raise ValueError(f"{ctx}.group 引用了未定义的组：{group_name!r}")
        return _role_rule_from_group(
            groups[group_name],
            strategy_override=strategy_override,
            require=require,
        )

    targets = _parse_role_object_targets(entry, ctx)
    return RoleRule(
        targets=targets,
        strategy=strategy_override,
        require=require,
    )


def _reject_retired_role_keys(entry: Mapping[str, Any], ctx: str) -> None:
    """退役的 role 键一律 fail loudly，不走「未知字段忽略」。

    未知字段忽略是为了向前兼容——没见过的键不该阻断启动。但 ``temperature``
    这类**曾经生效过**的键静默吞掉是另一回事：配置文件看上去接受它，行为上
    不认，改配置的人（或 agent）只会得到「改了没反应」。这里的报错要直接指出
    新落点，否则等价于没报。
    """
    for key in RETIRED_ROLE_KEYS:
        if key in entry:
            raise ValueError(
                f"{ctx}.{key} 已退役（2026-08-14）：采样参数改在端点（模型别名）"
                f"上声明，把它写到 providers[].models[] 对应条目的 {key} 字段，"
                "或用 settings 全局缺省；见 LLM路由契约 §2"
            )


def _parse_role_object_targets(
    entry: Mapping[str, Any],
    ctx: str,
) -> tuple[RoleTarget, ...]:
    """role 对象形态的目标：单目标 ``model``(+``provider``) 或多目标 ``targets``。

    ``targets`` 让「回退链 + strategy/require/temperature 覆写」可以同时表达
    ——纯数组形态带不了覆写字段，此前这个组合（契约推荐的 vision 配法）
    根本写不出来（2026-07-29 修复，example.json 一直按此形态写但解析不过）。
    """
    if "targets" not in entry:
        return (_parse_role_target(entry, ctx),)
    if "provider" in entry:
        raise ValueError(
            f"{ctx}.provider 只能与 model 搭配"
            "（targets 元素内用 {model, provider} 钉死）"
        )
    targets = entry["targets"]
    if not isinstance(targets, list) or not targets:
        raise ValueError(f"{ctx}.targets 必须是非空数组")
    return tuple(
        _parse_role_target(item, f"{ctx}.targets[{i}]")
        for i, item in enumerate(targets)
    )


def parse_config(raw: str) -> RoutingConfig:
    """解析 ``config/model_providers.json`` 全文档。

    顶层结构 ``{"providers": [...], "groups"?: {...}, "roles": {...},
    "settings": {...}}``，仅 ``providers`` 必填。非法配置一律 raise
    ValueError（含 JSONDecodeError），带上下文定位；未知字段忽略
    （向前兼容）。
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            "LLM 配置必须是 JSON 对象："
            "{providers: [...], groups?, roles?, settings?}"
        )

    providers = data.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("providers 必须是非空数组")
    endpoints = _parse_provider_items(providers, "providers")

    groups = _parse_groups_value(data.get("groups"))
    # 组名与模型名冲突时，roles 字符串写法无法区分「用组」还是「用模型」。
    model_names = {endpoint.model for endpoint in endpoints}
    collide = sorted(set(groups) & model_names)
    if collide:
        raise ValueError(
            "groups 的组名不能与任何已注册模型名相同（roles 字符串写法会歧义）："
            + ", ".join(repr(name) for name in collide)
        )
    roles = _parse_roles_value(data.get("roles"), groups)
    # role / group 钉死的服务商必须在注册表里（模型名的存在性由 Router 兜底
    # 校验，因为它还要容忍"模型在但都冷却"这类运行期状态）。
    known_providers = {endpoint.provider for endpoint in endpoints}
    for group_name, group in groups.items():
        for target in group.models:
            if target.provider is not None and target.provider not in known_providers:
                raise ValueError(
                    f"groups[{group_name!r}] 钉死的服务商不存在："
                    f"{target.provider!r}"
                )
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
    cooldown_cap = (
        _optional_positive_number(settings, "cooldown_max_multiplier", "settings")
        or COOLDOWN_MAX_MULTIPLIER
    )
    max_attempts = (
        _optional_positive_int(settings, "max_attempts_per_call", "settings")
        or DEFAULT_MAX_ATTEMPTS_PER_CALL
    )
    temperature = _optional_non_negative_number(settings, "temperature", "settings")
    if temperature is None:  # 0 是合法温度，不能用 or 链
        temperature = DEFAULT_TEMPERATURE
    max_tokens = _optional_positive_int(settings, "max_tokens", "settings")

    return RoutingConfig(
        endpoints=endpoints,
        roles=roles,
        groups=groups,
        default_strategy=strategy,
        cooldown_seconds=cooldown,
        cooldown_max_multiplier=cooldown_cap,
        max_attempts_per_call=max_attempts,
        temperature=temperature,
        max_tokens=max_tokens,
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
                targets=tuple(kept),
                strategy=rule.strategy,
                require=rule.require,
                flat_pool=rule.flat_pool,
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
            # flat_pool（groups 的 random/rr）：保留重复 target 作为加权槽位，
            # 不去重；回退链则按 spec 去重，避免空转。
            if rule.flat_pool:
                pool: list[ModelEndpoint] = []
                for target in rule.targets:
                    pool.extend(self._expand_target(target))
                groups = [pool] if pool else []
            else:
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
    单次上限内切不完就整体超时，不会偷偷延长）。透传前发一条
    ``call_cancelled`` 观测事件——调用方的超时在这一层只表现为取消，不留痕
    就没有任何地方记得下"是哪个端点、跑了多久被砍掉"，延迟统计里也只剩下
    活着回来的那些（2026-07-29 排查 Replyer 超时时正是卡在这里）。

    ``model_name`` 是 best-effort 观测标注（prompt 快照 / 日志用）：
    有过成功调用后是最近一次实际使用的模型，否则是首选端点的模型。给出的是
    **别名**（端点的路由身份，如 ``grok-4.5-xhigh``）而不是上游模型名——快照
    里要能看出这拍跑的是哪个档位，上游模型名在别名里已经蕴含。

    本类**不持有任何采样参数**（2026-08-14）：温度/max_tokens/透传参数全部
    是所选端点的属性，由 ``client_factory`` 从 ``ModelEndpoint`` 现场解析。

    传输层之外的失败（200 + 内容不可用）由调用方回报：见
    ``mark_last_call_failed``。
    """

    def __init__(
        self,
        router: EndpointRouter,
        *,
        client_factory: Callable[[ModelEndpoint], Any],
        role: str = DEFAULT_ROLE,
        model: str | None = None,
        provider: str | None = None,
        require: Sequence[str] = (),
        on_event: Callable[..., None] | None = None,
        on_outcome: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._router = router
        self._client_factory = client_factory
        self._role = role
        self._model = model
        self._provider = provider
        self._require = tuple(require)
        self._on_event = on_event
        self._on_outcome = on_outcome
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

    def mark_last_call_failed(self, reason: str) -> str | None:
        """把上一次「HTTP 成功但内容不可用」的调用补记成端点失败（体级失败）。

        传输层看不出的失败在这里补齐：上游内容策略拦截、网关把纯文本错误页
        当正文返回、模型稳定地吐非目标格式——都是 200 + 一段废话，
        ``ainvoke`` 只能记成 ``call_ok`` + ``mark_success``，端点不进冷却。
        后果是同一拍里的重试反复打在同一个端点上（2026-08-02 Gemini 被
        Google 内容策略拦截时实测：Planner 三次 JSON 重试全落在同一端点）。

        只有调用方知道正文该长什么样，所以由调用方回报；本方法只做熔断记账
        （+ 一条 ``body_rejected`` 观测事件），不改任何重试语义。记账后下一次
        ``ainvoke`` 的 ``resolve`` 会把该端点排到候选序尾部——role 配了
        ``targets`` 回退链就自动落到下一个目标；只有一个候选时冷却只是排序
        降权（冷却端点不剔除），行为不变。

        返回被记账的 spec；还没有过成功调用时返回 None（无副作用）。
        """
        spec = self.last_endpoint_spec
        if spec is None:
            return None
        cooldown = self._router.mark_failure(spec)
        self._emit(
            "body_rejected",
            endpoint=spec,
            role=self._role,
            error=str(reason)[:300],
            cooldown_seconds=cooldown,
        )
        return spec

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
                client = self._client_factory(endpoint)
                result = await client.ainvoke(messages, **kwargs)
            except asyncio.CancelledError:
                self._emit(
                    "call_cancelled",
                    endpoint=endpoint.spec,
                    role=self._role,
                    latency_ms=self._elapsed_ms(started),
                )
                self._publish_outcome(
                    {
                        "ok": False,
                        "role": self._role,
                        "endpoint": endpoint.spec,
                        "error_kind": "timeout",
                        "latency_ms": self._elapsed_ms(started),
                    }
                )
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
            self._publish_outcome(
                {
                    "ok": True,
                    "role": self._role,
                    "endpoint": endpoint.spec,
                    "latency_ms": self._elapsed_ms(started),
                }
            )
            return result

        if last_exc is None:  # max_attempts_per_call 被配成 0 之类的病态情形
            raise RuntimeError("未尝试任何 LLM 端点（max_attempts_per_call<1?）")
        self._publish_outcome(
            {
                "ok": False,
                "role": self._role,
                "error_kind": "call_failed",
                "error_message": f"{type(last_exc).__name__}: {last_exc}"[:300],
            }
        )
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

    def _publish_outcome(self, payload: dict[str, Any]) -> None:
        if self._on_outcome is None:
            return
        try:
            self._on_outcome(payload)
        except Exception:
            pass
