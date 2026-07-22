"""api_lab —— napcat/OneBot 响应结构观测台（待办 #7；仅 ``qqbot.main_test`` 加载）。

**写给 Claude 的调试器官**：把 napcat 侧的一切原始信息不加裁剪地暴露出来，
并全部落成 SFTP 工作区可直接 Read 的文件。两条信息流：

1. 被动 —— 事件四通道全量转储：message / notice / request / meta 全接
   （priority=1、block=False，不影响命令匹配），逐事件把适配器解析出的
   pydantic 模型**全字段** dump（raw_message、message 段、reply、sub_type、
   operator_id……一字不漏），写 loguru 日志 + ``runtime_data/api_lab/
   events-YYYYMMDD.jsonl``。心跳例外：第 1 次全量、之后每
   ``_HEARTBEAT_EVERY`` 次记一行计数（防刷屏）。注意：适配器解析失败的
   推送到不了 matcher，这里看不到——那类问题去 nonebot 自身日志找。

2. 主动 —— API 实验命令（一律 SUPERUSER 专用；COMMAND_START 含空串，群里
   直接打 ``api ...`` 也可触发）：

   - ``/api <action> [JSON对象参数]`` —— 直调 ``bot.call_api``。成功回显完整
     原始返回；失败把异常的 ``info``（NapCat BaseResponse：retcode /
     message / wording / status / stream）全量透出——与
     ``_onebot_common.call_action`` 折进 ``upstream_action_failed`` 的字段
     一一对应，实测值可直接对照契约假设。参数字符串值支持显式占位符
     （精确匹配才替换、所见即所得，不做任何静默注入）：``"$group_id"`` =
     当前群号、``"$user_id"`` = 发命令人、``"$self_id"`` = bot 自己。
   - ``/kick <user_id|@某人> [reject]`` —— 踢人 API 验证快捷方式（当前待办
     焦点）：先 dump 目标 ``get_group_member_info``（踢前 role 快照），再
     ``set_group_kick``（带 ``reject`` 词 → ``reject_add_request=true``）。
     踢后到达的 ``notice.group_decrease``（sub_type / operator_id）看事件
     转储；``reject`` 是否真生效 = 让目标重新申请入群、看 request 通道
     还有没有事件进来。
   - ``/probe`` —— 连通性与版本速查：get_login_info / get_version_info /
     get_status / get_group_list（群列表聊天里只报总数与前 5 个，完整在
     JSONL）。

聊天回显限长 ``_REPLY_CLIP`` 字符（QQ 单条消息保护），**完整内容永远在**
日志与 ``runtime_data/api_lab/api_calls-YYYYMMDD.jsonl``（ts / caller /
action / 替换后最终参数 / 延迟 ms / 成败 / 完整 result 或异常细节含
traceback）。文件落盘 best-effort：写失败只 warning，绝不影响调用与回显。
"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from typing import Any

from nonebot import (
    get_driver,
    on_command,
    on_message,
    on_metaevent,
    on_notice,
    on_request,
)
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now

logger = get_logger(__name__)

# 转储目录：与 prompt_snapshots 同款 CWD 相对路径（从项目根启动 →
# <项目根>/runtime_data/api_lab/）。
_LAB_DIR = Path("runtime_data") / "api_lab"

_REPLY_CLIP = 1800  # 聊天回显截断长度；完整内容恒在日志 + JSONL
_HEARTBEAT_EVERY = 100  # 心跳：第 1 次全量，之后每 N 次记一行计数
_GROUP_LIST_PREVIEW = 5  # /probe 聊天回显里最多列几个群

_ACTION_RE = re.compile(r"^[A-Za-z0-9_.]+$")


# ── 序列化 helpers ──────────────────────────────────────────────────────────


def _jsonable(value: Any) -> Any:
    """任意对象 → JSON 可序列化形态；不可序列化的原子值退化为 repr（信息不丢）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _event_payload(event: Any) -> Any:
    """事件模型 → 全字段 dict。

    优先走 pydantic 的 JSON 序列化（自带 Message / MessageSegment 的自定义
    encoder），v2（model_dump_json）/ v1（json）双兼容；再退 dict dump；全部
    失败退 repr —— 无论如何信息不丢。
    """
    for meth in ("model_dump_json", "json"):
        fn = getattr(event, meth, None)
        if callable(fn):
            try:
                return json.loads(fn())
            except Exception:  # noqa: BLE001 —— 观测层：单条序列化路径失败换下一条
                continue
    for meth in ("model_dump", "dict"):
        fn = getattr(event, meth, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:  # noqa: BLE001
                continue
    return {"repr": repr(event)}


def _describe_event(event: Any) -> dict[str, Any]:
    """事件 → 记录 dict：类名 + 三个访问器 + 全字段 payload。"""
    info: dict[str, Any] = {"class": type(event).__name__}
    for key, meth in (
        ("post_type", "get_type"),
        ("event_name", "get_event_name"),
        ("session_id", "get_session_id"),
    ):
        try:
            info[key] = getattr(event, meth)()
        except Exception:  # noqa: BLE001 —— 部分 meta 事件没有 session 等概念
            info[key] = None
    info["payload"] = _event_payload(event)
    return info


def _write_jsonl(stream: str, record: dict[str, Any]) -> None:
    """按天追加 JSONL。best-effort：失败只 warning，绝不影响主流程。"""
    try:
        _LAB_DIR.mkdir(parents=True, exist_ok=True)
        path = _LAB_DIR / f"{stream}-{china_now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_jsonable(record), ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[api_lab] JSONL 落盘失败（{}）：{}", stream, exc)


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return repr(value)


def _clip(text: str) -> str:
    if len(text) <= _REPLY_CLIP:
        return text
    return text[:_REPLY_CLIP] + (
        f"\n……(已截断 {len(text) - _REPLY_CLIP} 字符；"
        f"完整内容见日志与 {_LAB_DIR}/api_calls-*.jsonl)"
    )


# ── 被动信息流：事件四通道全量转储 ──────────────────────────────────────────

_heartbeat_seen = 0

_tap_message = on_message(priority=1, block=False)
_tap_notice = on_notice(priority=1, block=False)
_tap_request = on_request(priority=1, block=False)
_tap_meta = on_metaevent(priority=1, block=False)


def _record_event(channel: str, event: Event) -> None:
    global _heartbeat_seen
    is_heartbeat = getattr(event, "meta_event_type", None) == "heartbeat"
    if is_heartbeat:
        _heartbeat_seen += 1
        if _heartbeat_seen > 1 and _heartbeat_seen % _HEARTBEAT_EVERY != 0:
            return
    record: dict[str, Any] = {
        "ts": china_now().isoformat(),
        "channel": channel,
        **_describe_event(event),
    }
    if is_heartbeat:
        record["heartbeat_seen"] = _heartbeat_seen
    logger.info(
        "[api_lab] EVENT {} | {}",
        channel,
        json.dumps(_jsonable(record), ensure_ascii=False),
    )
    _write_jsonl("events", record)


@_tap_message.handle()
async def _tap_on_message(event: Event) -> None:
    _record_event("message", event)


@_tap_notice.handle()
async def _tap_on_notice(event: Event) -> None:
    _record_event("notice", event)


@_tap_request.handle()
async def _tap_on_request(event: Event) -> None:
    _record_event("request", event)


# 命名保留 "_on_meta" 子串：core/logging 的 _suppress_heartbeat_noise 按该
# 子串抑制 nonebot 对心跳 matcher 的生命周期日志洪水（每 3s 5 行）。
@_tap_meta.handle()
async def _tap_on_meta(event: Event) -> None:
    _record_event("meta", event)


# ── 主动信息流：API 实验命令 ────────────────────────────────────────────────


async def _call_and_record(
    bot: Bot, action: str, params: dict[str, Any], *, caller: str
) -> dict[str, Any]:
    """直调 napcat 动作并全量记录（日志 + api_calls JSONL）。

    返回记录 dict：成功含 ``result``（原始返回，一字不漏）；失败含
    ``error_class`` / ``error_str`` / ``error_info``（ActionFailed.info =
    NapCat BaseResponse 全文）/ ``traceback``。实验台语义：任何失败都是
    要观测的数据，不 raise、不重试。
    """
    record: dict[str, Any] = {
        "ts": china_now().isoformat(),
        "caller": caller,
        "action": action,
        "params": params,
    }
    started = time.monotonic()
    try:
        result = await bot.call_api(action, **params)
    except Exception as exc:  # noqa: BLE001
        record.update(
            ok=False,
            latency_ms=round((time.monotonic() - started) * 1000),
            error_class=type(exc).__name__,
            error_str=str(exc),
            error_info=_jsonable(getattr(exc, "info", None)),
            traceback=traceback.format_exc(),
        )
    else:
        record.update(
            ok=True,
            latency_ms=round((time.monotonic() - started) * 1000),
            result=_jsonable(result),
        )
    logger.info(
        "[api_lab] CALL {} | {}",
        action,
        json.dumps(_jsonable(record), ensure_ascii=False),
    )
    _write_jsonl("api_calls", record)
    return record


def _format_call(record: dict[str, Any]) -> str:
    """调用记录 → 聊天回显文本（截断由调用侧统一做）。"""
    head = "✅" if record.get("ok") else "❌"
    lines = [f"{head} {record['action']} ({record.get('latency_ms')}ms)"]
    if record.get("ok"):
        lines.append("result: " + _pretty(record.get("result")))
    else:
        lines.append(
            f"exception: {record.get('error_class')}: {record.get('error_str')}"
        )
        lines.append("info: " + _pretty(record.get("error_info")))
    return "\n".join(lines)


def _caller_of(event: MessageEvent) -> str:
    gid = getattr(event, "group_id", None)
    where = f"group:{gid}" if gid is not None else "private"
    return f"{where}/user:{event.user_id}/msg:{event.message_id}"


def _substitute_tokens(value: Any, event: MessageEvent, bot: Bot) -> Any:
    """参数值里的显式占位符替换（只动这三个精确字符串，其余原样透传）。

    私聊里用 ``"$group_id"`` 会原样发出去（napcat 会报错——这本身就是可
    观测信息，不静默兜底）。记录进 JSONL 的是替换后的最终参数，所见即所得。
    """
    if isinstance(value, dict):
        return {k: _substitute_tokens(v, event, bot) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_tokens(v, event, bot) for v in value]
    if value == "$group_id":
        return getattr(event, "group_id", value)
    if value == "$user_id":
        return event.user_id
    if value == "$self_id":
        try:
            return int(bot.self_id)
        except (TypeError, ValueError):
            return bot.self_id
    return value


async def _send(matcher: Any, text: str) -> None:
    """聊天回显 best-effort：发送失败（超长被拒等）只记日志——完整信息本就
    在日志与 JSONL 里，回显只是便利层。"""
    try:
        await matcher.send(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[api_lab] 聊天回显发送失败：{}", exc)


_USAGE = (
    "api_lab 用法（SUPERUSER 专用）：\n"
    "/api <action> [JSON对象参数] —— 直调 OneBot/napcat 动作，"
    "回显完整原始返回或失败 info\n"
    '  例：/api set_group_kick {"group_id":"$group_id",'
    '"user_id":123456,"reject_add_request":true}\n'
    '  例：/api get_group_member_info {"group_id":"$group_id",'
    '"user_id":"$user_id","no_cache":true}\n'
    "  例：/api get_version_info\n"
    '  占位符（字符串值精确匹配才替换）："$group_id"=本群 '
    '"$user_id"=发命令人 "$self_id"=bot\n'
    "/kick <user_id|@某人> [reject] —— 踢人验证：先 dump 目标成员信息，"
    "再 set_group_kick\n"
    "/probe —— get_login_info / get_version_info / get_status / "
    "get_group_list 速查\n"
) + f"完整转储：日志 + {_LAB_DIR}/events-*.jsonl 与 api_calls-*.jsonl"

_api_cmd = on_command("api", permission=SUPERUSER, priority=5, block=True)
_kick_cmd = on_command("kick", permission=SUPERUSER, priority=5, block=True)
_probe_cmd = on_command("probe", permission=SUPERUSER, priority=5, block=True)


@_api_cmd.handle()
async def _handle_api(
    bot: Bot, event: MessageEvent, raw: Message = CommandArg()
) -> None:
    text = raw.extract_plain_text().strip()
    if not text:
        await _send(_api_cmd, _USAGE)
        return
    action, _, param_text = text.partition(" ")
    if not _ACTION_RE.fullmatch(action):
        await _send(_api_cmd, f"非法 action 名：{action!r}（允许字母/数字/_/.）")
        return
    params: dict[str, Any] = {}
    param_text = param_text.strip()
    if param_text:
        try:
            parsed = json.loads(param_text)
        except json.JSONDecodeError as exc:
            await _send(_api_cmd, f"参数 JSON 解析失败：{exc}\n原文：{param_text}")
            return
        if not isinstance(parsed, dict):
            await _send(
                _api_cmd, f"参数必须是 JSON 对象（收到 {type(parsed).__name__}）"
            )
            return
        params = parsed
    params = _substitute_tokens(params, event, bot)
    record = await _call_and_record(bot, action, params, caller=_caller_of(event))
    await _send(_api_cmd, _clip(_format_call(record)))


def _extract_target(raw: Message) -> tuple[int | None, list[str]]:
    """从 /kick 参数抽目标：优先第一个 ``<at>`` 段，否则第一个纯数字 token。

    返回 ``(user_id, 剩余纯文本 token 列表)`` —— 剩余 token 判 reject 用。
    """
    tokens = raw.extract_plain_text().split()
    for seg in raw:
        if seg.type == "at":
            try:
                return int(seg.data.get("qq")), tokens
            except (TypeError, ValueError):  # "@全体成员" 等非数字目标
                return None, tokens
    for i, tok in enumerate(tokens):
        if tok.isdigit():
            return int(tok), tokens[:i] + tokens[i + 1 :]
    return None, tokens


@_kick_cmd.handle()
async def _handle_kick(
    bot: Bot, event: MessageEvent, raw: Message = CommandArg()
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await _send(_kick_cmd, "/kick 只能在群里用（目标群 = 当前群）")
        return
    target, rest = _extract_target(raw)
    if target is None:
        await _send(_kick_cmd, "用法：/kick <user_id|@某人> [reject]")
        return
    reject = any(t.lower() in {"reject", "true", "1", "yes"} for t in rest)
    caller = _caller_of(event)
    pre = await _call_and_record(
        bot,
        "get_group_member_info",
        {"group_id": event.group_id, "user_id": target, "no_cache": True},
        caller=caller,
    )
    kick = await _call_and_record(
        bot,
        "set_group_kick",
        {"group_id": event.group_id, "user_id": target, "reject_add_request": reject},
        caller=caller,
    )
    tail = "👉 后续 notice.group_decrease 看事件转储（sub_type/operator_id）"
    if reject:
        tail += "；reject 已开：让目标重新申请入群，观察 request 通道是否还有事件"
    reply = (
        "【踢前成员信息】\n"
        + _format_call(pre)
        + "\n【set_group_kick】\n"
        + _format_call(kick)
        + "\n"
        + tail
    )
    await _send(_kick_cmd, _clip(reply))


@_probe_cmd.handle()
async def _handle_probe(bot: Bot, event: MessageEvent) -> None:
    caller = _caller_of(event)
    parts: list[str] = []
    for action in ("get_login_info", "get_version_info", "get_status"):
        record = await _call_and_record(bot, action, {}, caller=caller)
        parts.append(_format_call(record))
    groups = await _call_and_record(bot, "get_group_list", {}, caller=caller)
    result = groups.get("result")
    if groups.get("ok") and isinstance(result, list):
        preview = ", ".join(
            f"{g.get('group_id')}({g.get('group_name')})"
            for g in result[:_GROUP_LIST_PREVIEW]
            if isinstance(g, dict)
        )
        parts.append(
            f"✅ get_group_list：共 {len(result)} 群，"
            f"前 {_GROUP_LIST_PREVIEW}：{preview}（完整见 JSONL）"
        )
    else:
        parts.append(_format_call(groups))
    await _send(_probe_cmd, _clip("\n".join(parts)))


# ── 生命周期 ────────────────────────────────────────────────────────────────

_driver = get_driver()


@_driver.on_bot_connect
async def _on_bot_connect(bot: BaseBot) -> None:
    _write_jsonl(
        "events",
        {
            "ts": china_now().isoformat(),
            "channel": "lifecycle",
            "event": "bot_connect",
            "self_id": bot.self_id,
            "adapter": type(bot.adapter).__name__,
        },
    )
    logger.info("[api_lab] ★ bot {} 已连接，实验台就绪\n{}", bot.self_id, _USAGE)


@_driver.on_bot_disconnect
async def _on_bot_disconnect(bot: BaseBot) -> None:
    _write_jsonl(
        "events",
        {
            "ts": china_now().isoformat(),
            "channel": "lifecycle",
            "event": "bot_disconnect",
            "self_id": bot.self_id,
        },
    )
    logger.warning("[api_lab] ☆ bot {} 断开", bot.self_id)


logger.info(
    "[api_lab] 观测台已加载：事件四通道全量转储 + /api /kick /probe"
    "（SUPERUSER={}）；转储目录 {}",
    sorted(_driver.config.superusers),
    _LAB_DIR.resolve(),
)
