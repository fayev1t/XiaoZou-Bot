"""LLM 请求出口：多服务商注册表 + 按模型名路由（胶水层）。

纯逻辑（配置解析 / 路由策略 / 被动熔断 / 失败切换）在
``qqbot/core/llm_routing.py``；本模块负责三件事：加载配置、把端点落成
ChatOpenAI 客户端（streaming / stream_usage 探测 / max_tokens / timeout）、
把路由事件接到日志。契约见 `开发文档/v2.0/20-横切契约/LLM路由契约.md`。

配置**只有一个来源**：``config/model_providers.json``（路径可用 env
``MODEL_PROVIDERS_PATH`` 覆写；模板见 ``config/model_providers.example.json``，
真实文件含 api_key、已被 .gitignore 排除）。三段：``providers``（服务商注册表：
名称 / base_url / api_key / 持有的模型——每个模型条目就是一个端点，可带自己的
温度 / max_tokens / timeout / 厂商透传 ``params``，并可用 ``upstream_model``
变成别名）+ ``roles``（用途 → 模型名，可选钉死服务商；**不含采样参数**）+
``settings``（全局策略缺省 random、冷却秒数、全局采样缺省 temperature/max_tokens）。
调用方只给模型名即可，路由器在持有该模型的服务商里按策略挑选。文件缺失或解析
失败 → LLM 整体不可用（fail loudly：``create_llm`` 返回 None，各调用方按自己的
降级语义处理），**绝不静默回落到别处的配置**。

2026-07-28 删除扁平 env 形态（``LLM_PROVIDER / LLM_API_KEY / LLM_MODEL /
LLM_BASE_URL``）。它当年是单服务商时代的向后兼容层，留着有两处实际危害：
一是同一份部署有两个真相源，"为什么改了 .env 不生效"要靠记住优先级才能答；
二是它把那个唯一端点硬标成 ``capabilities={"vision"}``（"旧配置视为天然
多模态"），于是配一个纯文本模型时，图片描述与表情包收藏会去调它然后失败——
而 Planner/Replyer 去多模态化之后，"planner 用纯文本模型"恰恰成了常态配置。

2026-07-29 采样参数同样收拢：``LLM_TEMPERATURE / LLM_MAX_TOKENS`` 从 .env
删除，改在 ``settings`` 段配 ``temperature`` / ``max_tokens``（原散落在
replyer / image_description / meme_caption 的常量一并删除）。.env 里不再有
任何 LLM 键。

2026-08-14 温度**再下沉一层到端点**：``roles[].temperature`` 退役（写了会
parse 期报错，不静默忽略），温度与 max_tokens / timeout / 厂商透传 ``params``
一样在 ``providers[].models[]`` 上声明，解析链收敛成一条——**端点声明 >
settings 全局缺省 > 内置 0.7**，与 max_tokens 一直以来的形状一致，role 完全
不参与采样。动因是思考等级：各家的档位键名与取值都不一致（``reasoning_effort``
的 ``high``/``xhigh``、``enable_thinking`` 的布尔、token 预算的整数），而 role
的目标是一条跨厂回退链，同一个 role 的请求会落到词表不同的模型上——把「怎么
调这个模型」放在 role 上根本表达不了。改成端点级之后，「模型 × 档位」就是一个
普通的可路由模型名（别名，见 ``ModelEndpoint``），groups 加权、role 回退链、
按端点独立的冷却计数器全部原样复用，路由层新增概念为零。连带 ``create_llm``
的 ``temperature`` 参数、``RoutedChatModel`` 的温度字段、``role_temperature()``
与 ``(spec, temperature)`` 复合缓存键一并删除。

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
        # spec → ChatOpenAI。2026-08-14 起键只剩 spec：一次调用的全部参数都是
        # 「端点 + settings 全局缺省」的纯函数，两者在配置加载时就固定了，同一个
        # spec 不可能再需要第二个客户端。要两种温度就注册两个别名（= 两个 spec）。
        self.clients: dict[str, Any] = {}


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
        "[llm] endpoint registry ready（endpoints 里 * = 回落 settings 全局缺省）",
        extra={
            "source": source,
            "endpoints": [_describe_endpoint(routing, e) for e in endpoints],
            "roles": {
                r: [f"{t.provider or '*'}/{t.model}" for t in rule.targets]
                for r, rule in roles.items()
            },
            "default_strategy": default_strategy,
            "cooldown_base_seconds": cooldown,
            "max_attempts_per_call": routing.max_attempts_per_call,
            "settings_temperature": routing.temperature,
            "settings_max_tokens": routing.max_tokens,
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


def _resolve_endpoint_params(
    routing: RoutingConfig, endpoint: ModelEndpoint
) -> tuple[float, int | None]:
    """端点的采样参数解析链：端点声明 > ``settings`` 全局缺省。

    0 是合法温度，不能用 ``or`` 链回落。无副作用，供客户端构造与启动日志共用
    ——启动日志打的必须是**真正会用的值**，否则「别名忘了写温度」就看不出来。
    """
    temperature = (
        endpoint.temperature
        if endpoint.temperature is not None
        else routing.temperature
    )
    max_tokens = (
        endpoint.max_tokens
        if endpoint.max_tokens is not None
        else routing.max_tokens
    )
    return temperature, max_tokens


def _describe_endpoint(routing: RoutingConfig, endpoint: ModelEndpoint) -> str:
    """启动日志用的端点摘要：这个 spec 实际会用什么参数。

    别名多起来之后，「``cpa/grok-4.5-xhigh`` 到底是哪个上游模型、哪一档」不该
    要去翻 JSON 才知道；「新加的别名忘了写温度」也该在启动时一眼看见，而不是
    等某天发现输出变飘再回头查。``*`` 标记该值回落自 ``settings`` 全局缺省。
    """
    temperature, max_tokens = _resolve_endpoint_params(routing, endpoint)
    parts = [endpoint.spec]
    if endpoint.upstream_model:
        parts.append(f"→{endpoint.wire_model}")
    parts.append(f"t={temperature}{'' if endpoint.temperature is not None else '*'}")
    if max_tokens is not None:
        fallback = "" if endpoint.max_tokens is not None else "*"
        parts.append(f"max_tokens={max_tokens}{fallback}")
    if endpoint.timeout_seconds is not None:
        parts.append(f"timeout={endpoint.timeout_seconds}")
    if not endpoint.streaming:
        parts.append("streaming=off")
    if endpoint.params:
        body = ",".join(f"{key}={value}" for key, value in endpoint.params)
        parts.append(f"params={{{body}}}")
    return " ".join(parts)


def _chat_client_for(runtime: _LLMRuntime, endpoint: ModelEndpoint) -> Any:
    """端点 → ChatOpenAI（按 spec 缓存复用连接池）。

    2026-08-14 起这里是采样参数的**唯一**落地点：温度不再由调用方或 role 传入，
    与 max_tokens / timeout / 厂商透传参数一样从 ``ModelEndpoint`` 上读，缺省
    回落 ``settings``。别名（``upstream_model`` 非空）在这里解回上游模型名。
    """
    cached = runtime.clients.get(endpoint.spec)
    if cached is not None:
        return cached

    from langchain_openai import ChatOpenAI

    # 字段表探测：pin 是 langchain-openai>=0.0.5，新字段不能假设存在。
    fields = (
        getattr(ChatOpenAI, "model_fields", None)
        or getattr(ChatOpenAI, "__fields__", {})
    )
    temperature, max_tokens = _resolve_endpoint_params(runtime.routing, endpoint)

    llm_kwargs: dict[str, Any] = {
        "model_name": endpoint.wire_model,
        "api_key": endpoint.api_key,
        "temperature": temperature,
    }
    if endpoint.streaming:
        llm_kwargs["streaming"] = True
        # 流式响应默认不带 usage；stream_usage=True 让最后一个 chunk 携带
        # token 用量（Prompt 快照 / 观测基线依赖它，待办 #11）。老版本
        # langchain_openai 没有该字段——不支持就不传，行为退化为快照里
        # usage=null，不影响调用。
        if "stream_usage" in fields:
            llm_kwargs["stream_usage"] = True

    if max_tokens is not None:
        llm_kwargs["max_tokens"] = max_tokens
    if endpoint.base_url:
        llm_kwargs["base_url"] = endpoint.base_url
    if endpoint.timeout_seconds is not None:
        llm_kwargs["timeout"] = endpoint.timeout_seconds
    if endpoint.params:
        # 别名的固定透传参数（reasoning_effort / enable_thinking / token 预算…）。
        # 新版 langchain_openai 有专用的 extra_body；老版本只能靠 model_kwargs
        # 并进顶层 payload。同 stream_usage 一样按字段表探测。
        passthrough = dict(endpoint.params)
        if "extra_body" in fields:
            llm_kwargs["extra_body"] = passthrough
        else:
            llm_kwargs["model_kwargs"] = passthrough

    logger.info(
        "[llm] create client",
        extra={
            "endpoint": endpoint.spec,
            "upstream_model": endpoint.wire_model,
            "base_url": endpoint.base_url,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "streaming": llm_kwargs.get("streaming", False),
            "params": dict(endpoint.params),
        },
    )
    client = ChatOpenAI(**llm_kwargs)
    runtime.clients[endpoint.spec] = client
    return client


def _log_route_event(kind: str, **info: Any) -> None:
    if kind == "call_ok":
        logger.info(
            "[llm] call ok endpoint={} role={} latency_ms={}".format(
                info.get("endpoint"), info.get("role"), info.get("latency_ms")
            )
        )
    elif kind == "body_rejected":
        # 体级失败：HTTP 成功但正文不可用（内容策略拦截 / 网关错误页当正文 /
        # 稳定吐非目标格式），由调用方回报后补记熔断。单独一档而不并进
        # call_failed：它没有 latency 语义（那次调用是"成功"的，延迟已经计过
        # 一次），并进去会让按 latency_ms 做的统计凭空多出一批 None。
        logger.warning(
            "[llm] body rejected endpoint={} role={} cooldown={}s reason={}".format(
                info.get("endpoint"),
                info.get("role"),
                info.get("cooldown_seconds"),
                info.get("error"),
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
    已有 None 分支）。``require`` 是可选能力过滤（遗留）；现役 vision/
    caption 靠 roles/groups 选型，不再传 ``require=("vision",)``。

    **不再接受任何采样参数**（2026-08-14）：温度随 max_tokens / timeout /
    厂商透传参数一起落到端点（模型别名）上，由 ``_chat_client_for`` 在选中
    端点后解析。同一个上游模型要两种温度就注册两个别名——与思考等级的表达
    方式一致，路由层不必知道「采样」这回事。原先的 ``temperature`` 位置参数
    只有测试在用（生产调用点一律只传 role），一并删除。
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

    return RoutedChatModel(
        runtime.router,
        client_factory=lambda endpoint: _chat_client_for(runtime, endpoint),
        role=role,
        model=model,
        provider=provider,
        require=require,
        on_event=_log_route_event,
    )
