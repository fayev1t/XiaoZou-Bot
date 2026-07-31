"""LLM 请求出口：多服务商注册表 + 按模型名路由（胶水层）。

纯逻辑（配置解析 / 路由策略 / 被动熔断 / 失败切换）在
``qqbot/core/llm_routing.py``；本模块负责三件事：加载配置、把端点落成
ChatOpenAI 客户端（streaming / stream_usage 探测 / max_tokens / timeout）、
把路由事件接到日志。契约见 `开发文档/v2.0/20-横切契约/LLM路由契约.md`。

配置**只有一个来源**：``config/model_providers.json``（路径可用 env
``MODEL_PROVIDERS_PATH`` 覆写；模板见 ``config/model_providers.example.json``，
真实文件含 api_key、已被 .gitignore 排除）。三段：``providers``（服务商注册表：
名称 / base_url / api_key / 持有的模型）+ ``roles``（用途 → 模型名，可选钉死
服务商、可配 per-role 温度）+ ``settings``（全局策略缺省 random、冷却秒数、
全局采样缺省 temperature/max_tokens）。调用方只给模型名即可，
路由器在持有该模型的服务商里按策略挑选。文件缺失或解析失败 → LLM 整体不可用
（fail loudly：``create_llm`` 返回 None，各调用方按自己的降级语义处理），
**绝不静默回落到别处的配置**。

2026-07-28 删除扁平 env 形态（``LLM_PROVIDER / LLM_API_KEY / LLM_MODEL /
LLM_BASE_URL``）。它当年是单服务商时代的向后兼容层，留着有两处实际危害：
一是同一份部署有两个真相源，"为什么改了 .env 不生效"要靠记住优先级才能答；
二是它把那个唯一端点硬标成 ``capabilities={"vision"}``（"旧配置视为天然
多模态"），于是配一个纯文本模型时，图片描述与表情包收藏会去调它然后失败——
而 Planner/Replyer 去多模态化之后，"planner 用纯文本模型"恰恰成了常态配置。

2026-07-29 采样参数同样收拢：``LLM_TEMPERATURE / LLM_MAX_TOKENS`` 从 .env
删除，改在 ``settings`` 段配 ``temperature`` / ``max_tokens``；per-role 温度
（原散落在 replyer / image_description / meme_caption 的常量）改在 ``roles``
里配。温度解析链：调用方显式传参（测试注入用）> role 规则的 ``temperature``
> ``settings.temperature`` > 内置 0.7。.env 里不再有任何 LLM 键。

配置在首次 ``create_llm()`` 时读取并缓存为进程级单例——冷却/熔断状态必须跨
调用方（planner / vision / caption / memory）共享；改配置需重启生效。
（``roles.replyer`` 随 2026-07-31 删除 Replyer 一并退役，见
重构提案-删除Replyer.md §5.5。）
"""

from typing import Any

from qqbot.core.llm_routing import (
    DEFAULT_ROLE,
    EndpointRouter,
    ModelEndpoint,
    RoutedChatModel,
    RoutingConfig,
    parse_config,
)
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_model_providers_path

logger = get_logger(__name__)


class _LLMRuntime:
    """配置 + 路由器 + ChatOpenAI 客户端缓存的进程级单例载体。"""

    def __init__(self, routing: RoutingConfig, router: EndpointRouter) -> None:
        self.routing = routing
        self.router = router
        # (spec, temperature) → ChatOpenAI。端点数 x 少数几档温度，有界。
        self.clients: dict[tuple[str, float | None], Any] = {}


_runtime: _LLMRuntime | None = None
_runtime_failed: bool = False


def reset_llm_runtime() -> None:
    """丢弃已缓存的配置/路由器/客户端（测试或热重载入口用）。"""
    global _runtime, _runtime_failed
    _runtime = None
    _runtime_failed = False


def _load_routing_config() -> RoutingConfig | None:
    """读 ``config/model_providers.json``。不存在 → None（无回落，LLM 不可用）；
    存在但读不了/解析失败 → raise（fail loudly，绝不静默换成另一套配置）。"""
    path = get_model_providers_path()
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    return parse_config(raw)


def _build_runtime() -> _LLMRuntime | None:
    try:
        routing = _load_routing_config()
    except (OSError, ValueError) as exc:
        logger.error(f"[llm] 配置文件加载失败（{get_model_providers_path()}）：{exc}")
        return None

    if routing is None:
        # 2026-07-28 起没有扁平 env 回落：这里是唯一的配置来源，缺了就是
        # 部署没配好，必须是 error 而不是 warning——LLM 全线不可用（Planner
        # 每拍降级 idle、caption raise、图片不描述），排查时第一眼
        # 就要看到这行。
        logger.error(
            "[llm] 未找到模型配置文件 {}：LLM 全部不可用。"
            "从 config/model_providers.example.json 复制一份并填好 api_key，"
            "或用 env MODEL_PROVIDERS_PATH 指向实际路径。",
            get_model_providers_path(),
        )
        return None

    endpoints: list[ModelEndpoint] = list(routing.endpoints)
    roles = routing.roles
    default_strategy = routing.default_strategy
    cooldown = routing.cooldown_seconds
    source = str(get_model_providers_path())

    router = EndpointRouter(
        endpoints,
        roles,
        default_strategy=default_strategy,
        cooldown_base_seconds=cooldown,
        cooldown_max_multiplier=routing.cooldown_max_multiplier,
        max_attempts_per_call=routing.max_attempts_per_call,
        on_warning=lambda message: logger.warning(f"[llm] {message}"),
    )
    logger.info(
        "[llm] endpoint registry ready",
        extra={
            "source": source,
            "endpoints": [e.spec for e in endpoints],
            "roles": {
                r: [f"{t.provider or '*'}/{t.model}" for t in rule.targets]
                for r, rule in roles.items()
            },
            "default_strategy": default_strategy,
            "cooldown_base_seconds": cooldown,
            "max_attempts_per_call": routing.max_attempts_per_call,
            "temperature": routing.temperature,
            "max_tokens": routing.max_tokens,
        },
    )
    return _LLMRuntime(routing, router)


def _get_runtime() -> _LLMRuntime | None:
    global _runtime, _runtime_failed
    if _runtime is not None:
        return _runtime
    if _runtime_failed:
        logger.warning("[llm] LLM 配置不可用（详见首次构建时的错误日志）")
        return None
    built = _build_runtime()
    if built is None:
        _runtime_failed = True
        return None
    _runtime = built
    return _runtime


def _chat_client_for(
    runtime: _LLMRuntime, endpoint: ModelEndpoint, temperature: float | None
) -> Any:
    """端点 → ChatOpenAI（按 (spec, temperature) 缓存复用连接池）。"""
    cache_key = (endpoint.spec, temperature)
    cached = runtime.clients.get(cache_key)
    if cached is not None:
        return cached

    from langchain_openai import ChatOpenAI

    llm_kwargs: dict[str, Any] = {
        "model_name": endpoint.model,
        "api_key": endpoint.api_key,
        "temperature": temperature,
    }
    if endpoint.streaming:
        llm_kwargs["streaming"] = True
        # 流式响应默认不带 usage；stream_usage=True 让最后一个 chunk 携带
        # token 用量（Prompt 快照 / 观测基线依赖它，待办 #11）。老版本
        # langchain_openai 没有该字段——按字段表探测，不支持就不传，
        # 行为退化为快照里 usage=null，不影响调用。
        fields = (
            getattr(ChatOpenAI, "model_fields", None)
            or getattr(ChatOpenAI, "__fields__", {})
        )
        if "stream_usage" in fields:
            llm_kwargs["stream_usage"] = True

    max_tokens = (
        endpoint.max_tokens
        if endpoint.max_tokens is not None
        else runtime.routing.max_tokens
    )
    if max_tokens is not None:
        llm_kwargs["max_tokens"] = max_tokens
    if endpoint.base_url:
        llm_kwargs["base_url"] = endpoint.base_url
    if endpoint.timeout_seconds is not None:
        llm_kwargs["timeout"] = endpoint.timeout_seconds

    logger.info(
        "[llm] create client",
        extra={
            "endpoint": endpoint.spec,
            "base_url": endpoint.base_url,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "streaming": llm_kwargs.get("streaming", False),
        },
    )
    client = ChatOpenAI(**llm_kwargs)
    runtime.clients[cache_key] = client
    return client


def _log_route_event(kind: str, **info: Any) -> None:
    if kind == "call_ok":
        logger.info(
            "[llm] call ok endpoint={} role={} latency_ms={}".format(
                info.get("endpoint"), info.get("role"), info.get("latency_ms")
            )
        )
    elif kind == "call_cancelled":
        # 取消（绝大多数是调用方 wait_for 超时，少数是停机）不计端点失败、不
        # 进冷却，但必须留痕：否则被砍掉的慢调用在 [llm] 日志里完全不存在，
        # 按 latency_ms 做的延迟统计就只剩幸存者。
        logger.warning(
            "[llm] call cancelled endpoint={} role={} latency_ms={}".format(
                info.get("endpoint"), info.get("role"), info.get("latency_ms")
            )
        )
    else:
        logger.warning(
            "[llm] call failed endpoint={} role={} latency_ms={} "
            "cooldown={}s error={}".format(
                info.get("endpoint"),
                info.get("role"),
                info.get("latency_ms"),
                info.get("cooldown_seconds"),
                info.get("error"),
            )
        )


async def create_llm(
    temperature: float | None = None,
    *,
    role: str = DEFAULT_ROLE,
    model: str | None = None,
    provider: str | None = None,
    require: tuple[str, ...] = (),
) -> RoutedChatModel | None:
    """拿一个统一的模型请求类（只承诺 ``ainvoke``，失败自动切换服务商）。

    三种定位方式（优先级从高到低）：

    - ``model="deepseek-chat"``：在所有持有该模型的服务商里按策略
      （缺省随机）挑一个——调用方只需要知道模型名；
    - ``model=... , provider=...``：显式钉死某服务商的某模型；
    - ``role="planner"``：按 ``config/model_providers.json`` 的 roles 表解析
      （planner / caption / default）。

    配置缺失 / 解析失败 / 候选为空时返回 None（与旧版语义一致，调用方
    已有 None 分支）。``require`` 是能力硬要求（如 caption 传
    ("vision",)）；显式配置的 role 候选不满足时警告放行，其余入口严格
    过滤（见 llm_routing 文档）。

    温度解析链（2026-07-29 收拢）：``temperature`` 显式传参（测试注入用，
    生产调用点不传）> role 规则配置的 ``temperature``（走 role 路由时）>
    ``settings.temperature`` 全局缺省。
    """
    runtime = _get_runtime()
    if runtime is None:
        return None

    try:
        import langchain_openai  # noqa: F401  仅探测运行时依赖可导入
    except ImportError as exc:
        logger.error(f"Failed to import ChatOpenAI: {exc}")
        return None

    if not runtime.router.has_candidates(
        role, model=model, provider=provider, require=require
    ):
        logger.warning(
            f"[llm] role={role!r} model={model!r} provider={provider!r} "
            "没有可用端点，返回 None"
        )
        return None

    temp = temperature
    if temp is None and model is None:
        temp = runtime.router.role_temperature(role)
    if temp is None:
        temp = runtime.routing.temperature
    return RoutedChatModel(
        runtime.router,
        client_factory=lambda endpoint, t: _chat_client_for(runtime, endpoint, t),
        role=role,
        model=model,
        provider=provider,
        require=require,
        temperature=temp,
        on_event=_log_route_event,
    )
