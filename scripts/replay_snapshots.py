"""Prompt 快照回放 — 程序形态 prompt 改动前后的行为对比。

服务器端手动运行（需要 langchain 运行时 + config/model_providers.json；扁平
LLM_* env 已于 2026-07-28/29 删除，本脚本经 create_llm(role="planner") 走统一
路由），本地 SFTP 工作区跑不了。用法：

    cd qqbot/
    python scripts/replay_snapshots.py                 # 回放 cases 目录全部用例
    python scripts/replay_snapshots.py --limit 5       # 只回放前 5 个
    python scripts/replay_snapshots.py path/to/a.json  # 回放指定快照文件
    python scripts/replay_snapshots.py --system recorded   # 用录制时的 system prompt
    python scripts/replay_snapshots.py --model grok-4.6-high
    python scripts/replay_snapshots.py --system recorded --repeats 2 \\
        --model gpt-5.6-luna-xhigh \\
        --model gemini-3.7-flash-high \\
        --model deepseek-v4-flash-max
    python scripts/replay_snapshots.py --with-images   # 兼容旧命令；Planner 已不收图片

工作流：
  1. 线上开着 PROMPT_SNAPSHOT_ENABLED 积累快照（runtime_data/prompt_snapshots/）。
  2. 把值得钉住的场景（该回复 / 不该插话 / 引用归属 / 工具收口 / 跨拍任务 /
     复读 / 口吻）**挑出来复制**到 runtime_data/prompt_replay/cases/——这一步
     是人工甄选，固定回放集由此沉淀。
  3. 每次改 prompt 后跑本脚本：默认 --system current 用当前代码重新渲染
     system prompt、配录制的行文法 user text 重新请求 LLM，输出与录制行为的
     对比报告（preflight / Query-Effect 调用序列 / 源码 / token / 延迟）。
  4. 比模型：``--model`` 钉死端点别名（可重复），建议配 ``--system recorded``
     排除 prompt 改动；``--repeats`` 压非确定性。不执行 Program。

已知边界（读报告时须心里有数）：
  - Program API 参考位于 system prompt；--system current 会反映当前 registry、
    schema 与工具 .md，--system recorded 则保留录制版本。
  - 投影层改动不会重渲录制的 user text，需要重新采集快照。
  - Planner 自 2026-07-28 起是纯文本模型；--with-images 仅为旧命令兼容，忽略。
  - 回放会真实计费调用 LLM；模型非确定性意味着单次差异要人工复核，
    连续多个用例同向漂移才是信号。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CASES_DIR = REPO_ROOT / "runtime_data" / "prompt_replay" / "cases"
DEFAULT_RUNS_DIR = REPO_ROOT / "runtime_data" / "prompt_replay" / "runs"


def _load_cases(args: argparse.Namespace) -> list[Path]:
    if args.cases:
        return [Path(p) for p in args.cases]
    cases_dir = Path(args.cases_dir)
    if not cases_dir.is_dir():
        print(f"用例目录不存在：{cases_dir}", file=sys.stderr)  # noqa: T201
        print(  # noqa: T201
            "先从 runtime_data/prompt_snapshots/ 挑快照复制进去。",
            file=sys.stderr,
        )
        return []
    files = sorted(cases_dir.glob("*.json"))
    if args.limit:
        files = files[: args.limit]
    return files


def _current_system_prompt(scope_key: str | None) -> str:
    """用当前代码 + 当前 .md 重新渲染 system prompt（与 planner 同一条路径）。"""
    from qqbot.services.agent_loop.llm_planner import (
        build_default_prompt_library,
    )
    from qqbot.services.agent_loop.tools import build_default_registry

    library = build_default_prompt_library(
        tool_registry=build_default_registry()
    )
    scope = (scope_key or "group:0").split(":", 1)[0]
    return library.render(scope=scope)


def _recorded_response(case: dict[str, Any]) -> str | None:
    """录制快照里最后一次成功往返的响应文本。"""
    for attempt in reversed(case.get("attempts") or []):
        text = attempt.get("response_text")
        if text:
            return text
    return None


def _program_shape(
    response_text: str | None,
    scope_key: str | None,
) -> tuple[list[str] | None, str | None]:
    """响应源码 → Query/Effect 调用序列；非法时返回稳定 preflight 错误。"""
    if not response_text:
        return None, "empty_response"
    from qqbot.services.agent_loop.program_ast import (
        ProgramPreflightError,
        preflight,
    )
    from qqbot.services.agent_loop.tools import build_default_registry

    scope = (scope_key or "group:0").split(":", 1)[0]
    try:
        prepared = preflight(response_text, build_default_registry(), scope)
    except ProgramPreflightError as exc:
        return None, f"{exc.info.error_kind}: {exc.info.message}"
    return [
        f"{site.program_kind}:{site.name}" for site in prepared.call_sites
    ], None


async def _replay_one(
    llm: Any, case_path: Path, args: argparse.Namespace
) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    from qqbot.services.agent_loop.prompt_snapshot import extract_usage

    case = json.loads(case_path.read_text(encoding="utf-8"))

    if args.system == "recorded":
        system_prompt = case.get("system_prompt", "")
    else:
        system_prompt = _current_system_prompt(case.get("scope_key"))

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=case.get("user_text", "")),
    ]

    started = time.monotonic()
    error: str | None = None
    response_text: str | None = None
    usage: dict[str, int] | None = None
    try:
        raw = await llm.ainvoke(messages)
        from qqbot.services.agent_loop.llm_planner import _extract_text

        response_text = _extract_text(raw)
        usage = extract_usage(raw)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.monotonic() - started) * 1000)

    recorded_text = _recorded_response(case)
    recorded_calls, recorded_preflight_error = _program_shape(
        recorded_text,
        case.get("scope_key"),
    )
    replay_calls, replay_preflight_error = _program_shape(
        response_text,
        case.get("scope_key"),
    )

    return {
        "case": case_path.name,
        "replay_model": getattr(args, "replay_model", None),
        "repeat": getattr(args, "repeat_index", 1),
        "scope_key": case.get("scope_key"),
        "tick_seq": case.get("tick_seq"),
        "system_mode": args.system,
        "system_prompt_chars": len(system_prompt),
        "recorded_system_prompt_chars": case.get("system_prompt_chars"),
        "error": error,
        "latency_ms": latency_ms,
        "recorded_latency_ms": (case.get("attempts") or [{}])[-1].get(
            "latency_ms"
        ),
        "usage": usage,
        "recorded_usage": (case.get("attempts") or [{}])[-1].get("usage"),
        "recorded_calls": recorded_calls,
        "replay_calls": replay_calls,
        "recorded_preflight_error": recorded_preflight_error,
        "replay_preflight_error": replay_preflight_error,
        "calls_match": (
            recorded_calls == replay_calls
            if recorded_calls is not None and replay_calls is not None
            else None
        ),
        "recorded_response": recorded_text,
        "replay_response": response_text,
    }


def _label(result: dict[str, Any]) -> str:
    model = result.get("replay_model") or "planner"
    repeat = result.get("repeat") or 1
    if repeat != 1:
        return f"{model}#{repeat}"
    return str(model)


def _clip(text: str | None, limit: int = 400) -> str:
    if not text:
        return "-"
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _write_report(out_dir: Path, results: list[dict[str, Any]]) -> Path:
    lines = [
        "# Prompt 回放报告",
        "",
        "| 用例 | 模型 | scope | 调用一致 | 录制调用 | 回放调用 | preflight | 耗时 | tokens |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        match = {True: "✅", False: "❌", None: "—"}[r["calls_match"]]
        usage = r.get("usage") or {}
        preflight_status = r.get("replay_preflight_error") or r.get("error") or "ok"
        lines.append(
            f"| {r['case']} | {_label(r)} | {r.get('scope_key') or '-'} | {match} "
            f"| {','.join(r['recorded_calls'] or []) or '-'} "
            f"| {','.join(r['replay_calls'] or []) or '-'} "
            f"| {preflight_status} "
            f"| {r['latency_ms']}ms "
            f"| {usage.get('total_tokens', '-')} |"
        )
    mismatches = [r for r in results if r["calls_match"] is False]
    lines += [
        "",
        f"共 {len(results)} 次调用，与录制调用序列不一致 {len(mismatches)} 次。",
        "调用一致只是第一道闸——分支、return、发言内容、口吻请对下面原文。",
        "",
        "## 原文对照",
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        grouped.setdefault(r["case"], []).append(r)
    for case_name, rows in grouped.items():
        rec = rows[0].get("recorded_response")
        lines += [
            "",
            f"### {case_name}",
            "",
            f"- recorded calls: {','.join(rows[0].get('recorded_calls') or []) or '-'}",
            f"- recorded: {_clip(rec)}",
        ]
        for r in rows:
            err = r.get("error") or r.get("replay_preflight_error")
            suffix = f" ({err})" if err else ""
            lines.append(
                f"- {_label(r)} [{','.join(r.get('replay_calls') or []) or '-'}]"
                f"{suffix}: {_clip(r.get('replay_response'))}"
            )
    report = out_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


async def _amain(args: argparse.Namespace) -> int:
    cases = _load_cases(args)
    if not cases:
        return 1

    from qqbot.core.llm import create_llm
    from qqbot.core.time import china_now

    models: list[str | None] = list(args.models) if args.models else [None]
    repeats = max(1, int(args.repeats))
    llms: dict[str | None, Any] = {}
    for model in models:
        if model is None:
            llm = await create_llm(role="planner")
        else:
            llm = await create_llm(model=model, provider=args.provider)
        if llm is None:
            target = model or "role=planner"
            print(  # noqa: T201
                f"LLM 未配置或别名不存在：{target}",
                file=sys.stderr,
            )
            return 1
        llms[model] = llm

    out_dir = Path(args.out_dir) / china_now().strftime("%Y%m%dT%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for case_path in cases:
        for model in models:
            for repeat in range(1, repeats + 1):
                tag = model or "planner"
                extra = f" #{repeat}" if repeats > 1 else ""
                print(f"replaying {case_path.name} @ {tag}{extra} ...")  # noqa: T201
                args.replay_model = model
                args.repeat_index = repeat
                try:
                    result = await _replay_one(llms[model], case_path, args)
                except Exception as exc:
                    result = {
                        "case": case_path.name,
                        "replay_model": model,
                        "repeat": repeat,
                        "error": (
                            f"replay crashed: {type(exc).__name__}: {exc}"
                        ),
                        "calls_match": None,
                        "recorded_calls": None,
                        "replay_calls": None,
                        "recorded_preflight_error": None,
                        "replay_preflight_error": None,
                        "latency_ms": -1,
                    }
                results.append(result)
                stem = case_path.stem
                if model:
                    stem = f"{stem}__{model}"
                if repeats > 1:
                    stem = f"{stem}__r{repeat}"
                (out_dir / f"{stem}.result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )

    report = _write_report(out_dir, results)
    print(f"报告：{report}")  # noqa: T201
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prompt 快照回放对比")
    parser.add_argument("cases", nargs="*", help="指定快照文件（缺省扫 cases 目录）")
    parser.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument(
        "--system",
        choices=("current", "recorded"),
        default="current",
        help="current=用当前代码重渲 system prompt（默认）；recorded=用录制版",
    )
    parser.add_argument(
        "--with-images",
        action="store_true",
        help="兼容旧命令；Planner 已是纯文本模型，本参数忽略",
    )
    parser.add_argument("--limit", type=int, default=0, help="至多回放 N 例")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        default=None,
        help="钉死端点别名（可重复）。省略则走 role=planner",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="配合 --model 钉死服务商，一般不用",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="每个用例×每个模型重复次数，默认 1",
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
