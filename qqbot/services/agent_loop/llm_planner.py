"""LLMPlanner — 调真 LLM 输出 DecisionOutput。

复用 qqbot/core/llm.create_llm() 当 LLM 客户端工厂（纯基础设施层，
不含 v1 业务）。Prompt 与解析逻辑均按 v2 任务与决策契约从零写。

System prompt 不再硬编码 —— 完全交给 prompts/catalog.py 装配。默认库
（build_default_prompt_library）取根页 `planner.md`：2026-07-31 删除 Replyer 后
Planner 是唯一的对话消费者，原先切成 `persona` / `system` / `group_chat_rules`
三个文件槽的共享资产已并回该页，页里只剩 `{{envelope}}`（信封语法，纯格式规范，
与投影层成对维护故仍是独立文件）与动态的 `{{tools_usage}}`（内容来自
ToolRegistry，按 scope 过滤）。角色卡在页首（那就是她自己），分析与最终措辞同归
一层。需要迭代人格、职责或参与规则时直接改 `planner.md`，改信封语法改
`envelope.md`，改工具说明改 `tools/<name>.md`，都不需要碰 planner。

错误兜底：LLM 不可用 / 接口报错 / JSON 不可解析 / schema 不符
一律 fallback 为单一 IdleAction，并把错误细节塞进 reasoning。
不抛异常给 AgentLoop —— AgentLoop 的 planner 异常分支当前只是把这
tick 草草收尾，看不到错误，不利于排障。

契约：任务与决策契约.md §2-§4
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.core.time import CHINA_TIMEZONE
from qqbot.services.agent_loop.decision import (
    Action,
    CallToolAction,
    DecisionContext,
    DecisionOutput,
    IdleAction,
)
from qqbot.services.agent_loop.projection import (
    _esc_attr,
    _esc_text,
    _safe_json,
    render_timeline_stream,
)
from qqbot.services.agent_loop.prompts.catalog import PromptLibrary
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    section_stats,
    should_snapshot,
    write_snapshot,
)
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)


# Planner 的正文与顺序全部写在根页 planner.md 里 —— 逻辑递进：你是谁 →
# 你所处的系统 → 输入信封（{{envelope}} 槽）→ 可调工具（{{tools_usage}} 槽）
# → 你需要做什么 → 你的输出。协议参考先给定义，行为规范随后，输出契约收尾
# （2026-08-01 维护者定稿）。分工理由写在 catalog 的 docstring。


def build_default_prompt_library(
    *,
    tool_registry: ToolRegistry | None = None,
) -> PromptLibrary:
    """v2 默认 system prompt 装配 —— 委托 prompts/catalog.py。

    正文、顺序、分隔全写在根页 `planner.md` 里（两个槽 `{{envelope}}` /
    `{{tools_usage}}` 位于系统段与行为规范之间）；这里只是 Planner 侧的入口。页与
    槽都在 render 时才读盘：改 .md 即生效、新增/下架工具立即反映。根页或文件槽
    读出来为空、槽名未登记都直接 raise（部署坏了不静默跑残缺 prompt）；
    tools_usage 未注入注册表时整槽跳过。
    """
    from qqbot.services.agent_loop.prompts.catalog import build_library

    return build_library("planner", tool_registry=tool_registry)


class LLMPlanner:
    """实现 Planner Protocol。线程安全的懒初始化 LLM 客户端。"""

    def __init__(
        self,
        llm_client: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        # 测试场景下可注入一个 stub client（提供 ainvoke(messages) 即可）；
        # 生产场景留 None，首次 decide() 时通过 create_llm() 建好缓存。
        self._llm = llm_client
        self._tool_registry = tool_registry
        # prompt_library 优先：调用方明确传入就用它；否则按 tool_registry
        # 装配默认库（planner 根页 + envelope/tools_usage 两个槽）。
        if prompt_library is None:
            prompt_library = build_default_prompt_library(
                tool_registry=tool_registry
            )
        self._prompt_library = prompt_library
        self._init_lock = asyncio.Lock()

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        llm = await self._ensure_llm()
        if llm is None:
            return DecisionOutput(
                actions=[IdleAction(reason="llm_unavailable")],
                reasoning="LLM client not configured",
            )

        try:
            messages, snapshot = _build_messages(
                context, self._tool_registry, self._prompt_library
            )
            if snapshot is not None:
                snapshot.model = _llm_model_name(llm)
            _log_request(context, messages)
        except Exception as exc:
            logger.warning("[llm_planner] build messages failed: {}", exc)
            return DecisionOutput(
                actions=[
                    IdleAction(reason=f"llm_call_error:{type(exc).__name__}")
                ],
                reasoning=str(exc)[:200],
            )

        # ─── JSON 解析重试（任务与决策契约 §7.1：非法输出同 tick 重试至多
        # 2 次，共 3 次调用）───
        # 输出不是合法 JSON 时，把模型原始输出 + 解析错误追加回对话再问一次，
        # 让模型自己修——一次格式抖动不再没收整拍的响应权。传输层异常（网络/
        # 超时）不重试：可能很慢，维持原 llm_call_error 回退等下一次唤醒。
        #
        # Prompt 快照（待办 #11）：每次往返记录 latency / usage / 响应原文，
        # 任何 return 路径（含异常回退）都由 finally 统一落盘——观测层绝不
        # 改变决策行为，写失败在 write_snapshot 内部吞掉。
        try:
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                started = time.monotonic()
                try:
                    raw = await llm.ainvoke(messages)
                    text = _extract_text(raw)
                    _log_response(context, text)
                except Exception as exc:
                    logger.warning("[llm_planner] LLM call failed: {}", exc)
                    if snapshot is not None:
                        snapshot.add_attempt(
                            latency_ms=_elapsed_ms(started),
                            error=f"{type(exc).__name__}: {exc}"[:300],
                        )
                        snapshot.outcome = "call_error"
                    return DecisionOutput(
                        actions=[
                            IdleAction(
                                reason=f"llm_call_error:{type(exc).__name__}"
                            )
                        ],
                        reasoning=str(exc)[:200],
                    )
                if snapshot is not None:
                    snapshot.add_attempt(
                        latency_ms=_elapsed_ms(started),
                        response_text=text,
                        usage=extract_usage(raw),
                    )
                try:
                    parsed = _parse_json(text)
                except Exception as exc:
                    logger.warning(
                        "[llm_planner] JSON parse failed (attempt {}/{}): {} raw={!r}",
                        attempt,
                        max_attempts,
                        exc,
                        text[:200],
                    )
                    if snapshot is not None and snapshot.attempts:
                        snapshot.attempts[-1].error = (
                            f"json_error:{type(exc).__name__}"
                        )
                    # 体级失败回报（2026-08-02）：模型 HTTP 200 返回了内容但
                    # 不是要求的 JSON——上游内容策略拦截、网关把纯文本错误页
                    # 当正文返回都长这样，路由层只把异常算失败，看到的是
                    # call_ok。不回报的话本拍三次重试会反复打在同一个端点上
                    # （实测：Gemini 被 Google 内容策略拦截时无一次换端点）。
                    # 回报后该端点进冷却排到候选序尾部，重试自动落到 role
                    # 配的下一个回退目标；只有一个候选时冷却只是排序降权，
                    # 行为不变。最后一次失败也照记——让下一拍从别的端点起步。
                    mark_failed = getattr(llm, "mark_last_call_failed", None)
                    if callable(mark_failed):
                        try:
                            mark_failed(f"json_error:{type(exc).__name__}")
                        except Exception:  # 路由记账绝不反噬决策
                            pass
                    if attempt >= max_attempts:
                        if snapshot is not None:
                            snapshot.outcome = "json_error_giveup"
                        return DecisionOutput(
                            actions=[
                                IdleAction(
                                    reason=f"llm_json_error:{type(exc).__name__}"
                                )
                            ],
                            reasoning=(
                                f"unparseable after {max_attempts} attempts: "
                                f"{text[:120]}"
                            ),
                        )
                    from langchain_core.messages import AIMessage, HumanMessage

                    messages = list(messages) + [
                        AIMessage(content=text),
                        HumanMessage(
                            content=(
                                "Your previous response could not be parsed as "
                                f"the required JSON ({type(exc).__name__}: {exc}). "
                                "Re-emit your COMPLETE decision as ONE valid JSON "
                                "object only — no prose, no markdown fences."
                            )
                        ),
                    ]
                    continue
                if snapshot is not None:
                    snapshot.outcome = "parsed"
                return _parse_decision_output(parsed)
            # for 循环内必 return；此处只为类型完备
            return DecisionOutput(
                actions=[IdleAction(reason="llm_json_error:exhausted")],
            )
        finally:
            if snapshot is not None:
                write_snapshot(snapshot)

    async def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        async with self._init_lock:
            if self._llm is None:
                self._llm = await create_llm(role="planner")
            return self._llm


def _build_messages(
    context: DecisionContext,
    tool_registry: ToolRegistry | None,
    prompt_library: PromptLibrary,
) -> tuple[list[Any], PromptSnapshot | None]:
    """构造 chat 输入 + 可选的 Prompt 快照（快照关闭 / scope 不在白名单时为
    None）。langchain_core.messages 在 langchain_openai 已是必依赖。

    System prompt 完全由提示词库输出 —— 默认装配是根页 `planner.md` 的正文，
    其后依次展开 `envelope.md` 与 tools_usage 段（逐工具 `tools/<name>.md`）。

    HumanMessage 的 text block 用 XML 信封而非 JSON 拼装：timeline 里每条
    item 的 render 字段本身就是 `<message ...>` / `<tool-call ...>` /
    `<my-reply ...>` / `<notice ...>` 等独立标签，所以外层再用 `<agent-input>` /
    `<timeline>` 标签嵌套时上下引用关系（reply、at、tool_call ↔ result）
    依然连贯，而不会被 JSON 字符串转义压平成扁平的字段表。

    图片（2026-07-28 起）：Planner 是**纯文本模型**，HumanMessage 只有 XML
    文本，不再附带任何图像 block。群里的图在 EventIngest 落盘时就由专用 VLM
    转录成客观描述，随事件正文进 timeline，渲染成 `<image hash="..."
    desc="..."/>` —— 描述内联在它所属的那条消息里，图文时序天然对齐，旧多模态
    路径靠 `↓ image hash=` label 给图块对位的做法（3 张图以上常错位）随之作废。
    需要就某张图追问具体问题时走 look_at_image 工具现场重看。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # 按当前 loop 的 scope 过滤 catalog **与 tools_usage 文档**：allowed_scopes 限定
    # 的工具（如 ban / respond_to_group_join_request 仅 group）既不出现在别的 scope 的 tool-catalog
    # 里，其用法文档也不进别的 scope 的 system prompt——否则群专用工具的说明会泄漏
    # 到 system loop、system 专用工具的说明会泄漏到群 loop，徒增提示噪音与误判
    # （§2.2；catalog 与 usage 同一把 scope 尺子）。
    scope = context.scope_key.split(":", 1)[0]
    # 片段直接拼起来与 render() 逐字节一致（render() 就是这么实现的）——多要
    # 一份分段统计给快照，不重复求值。片段之间没有额外分隔符：分隔线是根页
    # 正文里的字符。
    sections = prompt_library.render_sections(scope=scope)
    system_content = "".join(sec.text for sec in sections)
    tool_catalog = (
        tool_registry.catalog(scope) if tool_registry is not None else []
    )
    xml_text = _render_input_xml(context, tool_catalog)

    snapshot: PromptSnapshot | None = None
    if should_snapshot(context.scope_key):
        snapshot = PromptSnapshot(
            kind="planner",
            scope_key=context.scope_key,
            tick_seq=context.tick_seq,
            correlation_id=context.correlation_id,
            system_prompt=system_content,
            user_text=xml_text,
            sections=section_stats(sections),
            validation_retry=bool(
                getattr(context, "validation_feedback", None)
            ),
        )

    # content 是**纯字符串**而非单元素 block 数组：Planner 不再有图，多模态
    # 结构没有存在意义，字符串也是各网关兼容性最好的形态。
    return [
        SystemMessage(content=system_content),
        HumanMessage(content=xml_text),
    ], snapshot


def _render_input_xml(
    context: DecisionContext, tool_catalog: list[dict]
) -> str:
    """拼装喂给 LLM 的 XML 信封。

    结构（顺序按**变化频率升序**排列——前缀缓存契约，2026-07-12；
    saved-memes 为唯一显著性例外，见段末说明）：

      <agent-input scope="..." bot_qq="..." bot_role="...">
        <tool-catalog>
          <tool name="..." description="...">
            <arguments-schema>{json schema 文本}</arguments-schema>
          </tool>
        </tool-catalog>
        <timeline>
          <time when="...">
            {item.render ...}           (同秒的行共享一个时刻节点)
          </time>
          ...
        </timeline>
        <saved-memes>
          <meme hash="..." saved_at="...">描述</meme>
        </saved-memes>                                  (有收藏才出)
        <active-tasks>
          <task task_id="..." state="..." description="...">
            <related-tools>tool1,tool2</related-tools>
            <triggered-by event_id="..."/>            (可选)
            <pending-tool-call-ids>tc1,tc2</pending-tool-call-ids>  (有才出)
            <progress-notes>
              <note time="...">...</note>
            </progress-notes>
          </task>
        </active-tasks>
        <current now="..."/>
        <validation-error>attempt N rejected: ...</validation-error>       (仅校验重试)
      </agent-input>

    缓存契约（2026-07-12）：OpenAI 系 API 的自动前缀缓存要求前缀**逐字节
    一致**。now= 每拍必变，曾是 <agent-input> 的头属性，把可缓存前缀
    掐断在 system prompt 末尾——tool-catalog（部署内静态）和 timeline（追加
    为主，窗口起点锚定见 projection）每拍全价重计费。现按变化频率排序：
    头属性只留 scope/bot_qq/bot_role（稳定/极少变）；active-tasks 任务活跃期
    逐拍变（pending_tool_call_ids 随工具收口增删），排到 timeline 之后；
    每拍必变的 now 沉为尾部 <current/>；validation-error 只在同 tick
    校验重试出现（契约 §7.1），放最尾——同拍重试可复用直到 <current/> 的
    前缀，且作为最后一行对模型最显著。saved-memes 是唯一的显著性例外
    （2026-08-01）：它少变、本可留在 timeline 之前吃前缀缓存，但选图发生在
    读完局面之后，目录隔着整条 timeline 就几乎不被想起——移到 </timeline>
    之后、active-tasks 之前，接受它随 timeline 追加逐拍重编码。

    工具结果只在 <timeline> 的 <tool-call status="complete"> 行呈现一次
    （2026-07-02 删除了 <pending-tool-results> 区——同一调用双重渲染、且无
    消费切割地每拍重复出现，是模型复读的直接诱饵）。2026-08-01 起
    decision_emitted.reasoning 只保留在运行日志与快照，不再投影进 timeline；
    跨拍事实与义务分别由客观事件行和 active-tasks 表达。
    """
    parts: list[str] = []
    # bot_qq 可选（值来自 context.bot_user_id）：未注入（启动初期 bot_registry
    # 还空、或测试场景）时不渲染属性；此时模型仍可靠别人
    # <reply ... from_self="true"/> 的服务端标注识别"这条是回复我的"
    # （from_self 由投影层解析，不依赖本属性）。属性名用 _qq 后缀与
    # sender_qq / from_qq 同一 ID 空间记法。
    bot_attr = (
        f' bot_qq="{_esc_attr(context.bot_user_id)}"'
        if context.bot_user_id
        else ""
    )
    # bot_role 同样可选：sweep 未完成时 None，不渲染。这是**折叠快照**，仅作给
    # LLM 的角色提示——真正判 bot 权限时工具会**实时**复查当前角色（见
    # tool_registry._effective_bot_role），快照过期也不会误判。故 prompt 明确要求
    # LLM 不要据此快照"该调不调"：真无权限时工具返回 permission_denied_bot_role，
    # 快照过低但已实际升权的调用照样能过（消除规划层假阴性）。
    role_attr = (
        f' bot_role="{_esc_attr(context.bot_role)}"' if context.bot_role else ""
    )
    # 头属性只留稳定字段（scope 恒定 / bot_qq 启动后恒定 / bot_role 极少变）
    # ——每拍必变的 now 在尾部 <current/>，见本函数 docstring 的缓存契约。
    parts.append(
        f'<agent-input scope="{_esc_attr(context.scope_key)}"'
        f"{bot_attr}{role_attr}>"
    )

    parts.append("<tool-catalog>")
    for tool in tool_catalog:
        name = _esc_attr(str(tool.get("name", "")))
        desc = _esc_attr(str(tool.get("description", "")))
        schema_json = _safe_json(tool.get("arguments_schema") or {})
        # required_permission / required_bot_role 作为属性透出，让 LLM 调用前
        # 即可判断 "我能不能调" 而不必非要触发 tool_failed 再学习。tool_registry
        # 的 catalog() 已经兜底过缺失值；required_bot_role=None 的工具不渲染该
        # 属性（绝大多数工具不需要 bot 是管理员，省 token + 减噪音）。
        req_perm = _esc_attr(str(tool.get("required_permission", "GUEST")))
        req_bot_role = tool.get("required_bot_role")
        role_attr = (
            f' required_bot_role="{_esc_attr(str(req_bot_role))}"'
            if req_bot_role
            else ""
        )
        parts.append(
            f'<tool name="{name}" description="{desc}" '
            f'required_permission="{req_perm}"{role_attr}>'
            f"<arguments-schema>{_esc_text(schema_json)}</arguments-schema>"
            f"</tool>"
        )
    parts.append("</tool-catalog>")

    parts.append("<timeline>")
    # 时间流渲染：行按同秒分组嵌进 <time when="…"> 时刻节点，行内无时间
    # 属性（render_timeline_stream，时间流契约 2026-07-26）。
    parts.extend(render_timeline_stream(context.timeline))
    parts.append("</timeline>")

    # ─── 表情包收藏夹（有才渲染）：meme 工具凭 hash 精确操作收藏的选图目录。
    # 空收藏整段省略——不给模型一个空 <saved-memes> 去好奇。2026-08-01 从
    # timeline 之前移到之后：选图发生在读完局面之后，目录隔着整条 timeline
    # 时几乎不被想起；代价是本段进入 timeline 追加即失效的重编码区——显著性
    # 换缓存，与下方 active-tasks 同型取舍（变化频率升序布局的唯一例外）。───
    saved_memes = getattr(context, "saved_memes", None) or []
    if saved_memes:
        parts.append("<saved-memes>")
        for meme in saved_memes:
            saved_attr = _esc_attr(
                meme.saved_at.isoformat(timespec="seconds")
            )
            parts.append(
                f'<meme hash="{_esc_attr(meme.file_hash)}" '
                f'saved_at="{saved_attr}">'
                f"{_esc_text(meme.description)}</meme>"
            )
        parts.append("</saved-memes>")

    # active-tasks 在 timeline 之后：任务活跃期它逐拍变（pending_tool_call_ids
    # 随工具收口增删），放前面会在多工具工作流里逐拍掐断 timeline 的缓存前缀；
    # 放这里还让"当前承诺"紧邻决策位置，显著性只增不减。
    parts.append("<active-tasks>")
    for task in context.active_tasks:
        parts.append(_render_task_xml(task))
    parts.append("</active-tasks>")

    # <pending-reply> 段已于 2026-07-24 删除（待办#19）——待发稿的调度事实与
    # 授权内容都在 timeline 的 <tool-call name="reply"> 行上（<result> / <args>），
    # 独立状态区属于重复渲染。顺带一个缓存收益：它曾是全信封变化最频繁的业务
    # 段（每次落稿 revision/flush_at 都变、创建/flush 时整段出现消失），撤掉后
    # 从 </active-tasks> 到 <current/> 之间不再有抖动源。

    # ─── 每拍必变的时钟字段，沉底（缓存契约见本函数 docstring）───
    # @tick 已于 2026-07-30 从信封删除（DecisionContext.tick_seq 本身保留——
    # 事件 payload / 日志配对 / 快照文件名都还靠它）。删的理由不是"省字节"：
    # 它与 now= 同处 <current/>，而 now 每拍必变且删不掉，所以 tick 从来没有
    # 额外掐断过任何一段可缓存前缀，缓存收益恒为零。真正的问题是它对模型
    # **无锚点**：全信封只有这一处出现拍号，timeline 的事件行都不带拍号，
    # 模型既减不出步数也定位不了任何行；而进程重启后 _tick_seq 归零、
    # timeline 却从库里重新折叠出满窗历史，tick="1" 配一整段往事是**误导**而
    # 不只是噪音。（历史反证：Replyer 侧的 <current/> 从来不带 @tick 且从未
    # 缺过什么。）若将来要给模型"这是本轮连续推演第几步"的信号，应新增
    # 带锚点的 burst_id/burst_step，不要把这个无锚点的绝对计数加回来。
    #
    # 时区契约：所有暴露给 LLM 的时间都是北京时间（与数据库写入侧 china_now()
    # 一致）。caller 传错时区时 astimezone() 兜底，naive datetime 假设它就是
    # 北京时间。
    now = context.now
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHINA_TIMEZONE)
    else:
        now = now.astimezone(CHINA_TIMEZONE)
    parts.append(f'<current now="{_esc_attr(now.isoformat())}"/>')

    # ─── 同 tick 校验重试反馈（仅重试调用渲染，契约 §7.1）───
    validation_feedback = getattr(context, "validation_feedback", None)
    if validation_feedback:
        parts.append(
            "<validation-error>"
            f"{_esc_text(validation_feedback)}</validation-error>"
        )

    parts.append("</agent-input>")
    return "\n".join(parts)


def _render_task_xml(task: Any) -> str:
    """单条 task → <task ...> 块。
    related_tools / pending_tool_call_ids 用逗号串方便 LLM 扫读；progress_notes
    每条单独 <note>，时间属性走 _esc_attr。"""
    inner: list[str] = []
    related = ",".join(task.related_tools or [])
    if related:
        inner.append(f"<related-tools>{_esc_text(related)}</related-tools>")
    trig = getattr(task, "triggered_by_event_id", None)
    if trig:
        inner.append(f'<triggered-by event_id="{_esc_attr(str(trig))}"/>')
    pending = ",".join(task.pending_tool_call_ids or [])
    if pending:
        inner.append(
            f"<pending-tool-call-ids>{_esc_text(pending)}"
            f"</pending-tool-call-ids>"
        )
    notes = getattr(task, "progress_notes", None) or []
    if notes:
        note_parts = ["<progress-notes>"]
        for n in notes:
            note_parts.append(
                f'<note time="{_esc_attr(n.at.isoformat())}">'
                f"{_esc_text(n.note)}</note>"
            )
        note_parts.append("</progress-notes>")
        inner.append("".join(note_parts))
    # task_id= 而非裸 id=：与 call_tool 及 task 工具 arguments 里的字段名
    # 同名直抄，且与 message_id / event_id 空间区分。
    return (
        f'<task task_id="{_esc_attr(task.task_id)}" '
        f'state="{_esc_attr(task.state)}" '
        f'description="{_esc_attr(task.description)}">'
        f"{''.join(inner)}</task>"
    )


def _elapsed_ms(started_monotonic: float) -> int:
    return int((time.monotonic() - started_monotonic) * 1000)


def _llm_model_name(llm: Any) -> str | None:
    """best-effort 取模型名（ChatOpenAI 是 model_name；stub / 其它实现取不到
    就 None——快照记 null，不猜）。"""
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _log_request(context: DecisionContext, messages: list[Any]) -> None:
    """喂给 LLM 的完整内容打到 INFO 日志，方便核对事件流是否合理。

    布局优先可读：用换行 + 分隔条把 system / user / response 三段切开，
    避免一行 5KB+ 撑爆 terminal。system prompt 只打首尾几行 + 长度，
    因为它基本静态（planner.md 正文 + envelope + tools usage）；
    user 段（XML 信封）是 tick 之间真正变化的东西，原样全打。
    """
    # messages = [SystemMessage, HumanMessage]
    sys_msg, human_msg = messages[0], messages[1]
    system_text = getattr(sys_msg, "content", "") or ""
    human_content = getattr(human_msg, "content", "") or ""

    # 2026-07-28 起 content 恒为 str（Planner 无图）；list 分支留着兜住早期
    # 快照/测试里手工构造的多模态消息，不为它多记一个恒零的 image_blocks 计数。
    if isinstance(human_content, list):
        user_text = "\n".join(
            b.get("text", "")
            for b in human_content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        user_text = str(human_content)

    sep = "=" * 80
    logger.info(
        "\n{sep}\n[llm_planner] → LLM  scope={scope} tick={tick} "
        "system_prompt_chars={syslen} user_xml_chars={ulen}\n"
        "{sep}\n"
        "── system_prompt (head 400 / tail 200) ──\n{sys_head}\n…\n{sys_tail}\n"
        "── user_xml ──\n{user}\n{sep}",
        sep=sep,
        scope=context.scope_key,
        tick=context.tick_seq,
        syslen=len(system_text),
        ulen=len(user_text),
        sys_head=system_text[:400],
        sys_tail=system_text[-200:] if len(system_text) > 600 else "",
        user=user_text,
    )


def _log_response(context: DecisionContext, text: str) -> None:
    sep = "=" * 80
    logger.info(
        "\n{sep}\n[llm_planner] ← LLM  scope={scope} tick={tick} "
        "response_chars={rlen}\n{sep}\n{body}\n{sep}",
        sep=sep,
        scope=context.scope_key,
        tick=context.tick_seq,
        rlen=len(text),
        body=text,
    )


def _extract_text(message: Any) -> str:
    """langchain BaseMessage.content 在多模态/分片场景下可能是 list[dict]，
    统一拍平成 str。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict) and "text" in chunk:
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts)
    return str(content)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _parse_json(text: str) -> Any:
    """容忍 markdown 围栏。LLM 偶尔会无视 "no fences" 指令。"""
    m = _FENCE_RE.match(text)
    body = m.group(1) if m else text
    return json.loads(body)


def _parse_decision_output(parsed: Any) -> DecisionOutput:
    if not isinstance(parsed, dict):
        return DecisionOutput(
            actions=[IdleAction(reason="llm_schema_error:not_object")],
            reasoning=str(parsed)[:200],
        )

    reasoning = parsed.get("reasoning")
    reasoning_str = reasoning if isinstance(reasoning, str) else None
    raw_actions = parsed.get("actions") or []
    if not isinstance(raw_actions, list):
        return DecisionOutput(
            actions=[IdleAction(reason="llm_schema_error:actions_not_list")],
            reasoning=reasoning_str,
        )

    actions: list[Action] = []
    for raw in raw_actions:
        action = _parse_action(raw)
        if action is None:
            return DecisionOutput(
                actions=[IdleAction(reason="llm_schema_error:bad_action")],
                reasoning=f"bad action: {raw}"[:200],
            )
        actions.append(action)

    if not actions:
        actions = [IdleAction(reason="empty_actions")]

    return DecisionOutput(actions=actions, reasoning=reasoning_str)


def _parse_action(raw: Any) -> Action | None:
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    try:
        if t == "idle":
            return IdleAction(reason=str(raw.get("reason", "")))
        if t == "call_tool":
            args = raw.get("arguments") or {}
            return CallToolAction(
                tool_name=str(raw.get("tool_name", "")),
                arguments=args if isinstance(args, dict) else {},
                task_id=raw.get("task_id") or None,
                task_ref=raw.get("task_ref") or None,
                triggered_by_event_id=raw.get("triggered_by_event_id") or None,
            )
        # NOTE: t == "reply" 已弃用——发言现在是工具，走
        # {"type":"call_tool","tool_name":"reply","arguments":{...}}。
    except Exception:
        return None
    return None
