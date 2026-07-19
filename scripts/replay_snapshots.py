"""Prompt 快照回放 — prompt 改动前后的行为对比（待办 #11 回归评测基线）。

服务器端手动运行（需要 langchain 运行时 + LLM_* env 配置），本地 SFTP 工作
区跑不了。用法：

    cd qqbot/
    python scripts/replay_snapshots.py                 # 回放 cases 目录全部用例
    python scripts/replay_snapshots.py --limit 5       # 只回放前 5 个
    python scripts/replay_snapshots.py path/to/a.json  # 回放指定快照文件
    python scripts/replay_snapshots.py --system recorded   # 用录制时的 system prompt
    python scripts/replay_snapshots.py --with-images   # 从媒体库按 hash 复原图片

工作流：
  1. 线上开着 PROMPT_SNAPSHOT_ENABLED 积累快照（runtime_data/prompt_snapshots/）。
  2. 把值得钉住的场景（该回复 / 不该插话 / 引用归属 / 工具收口 / 跨拍任务 /
     复读 / 口吻）**挑出来复制**到 runtime_data/prompt_replay/cases/——这一步
     是人工甄选，固定回放集由此沉淀。
  3. 每次改 prompt 后跑本脚本：默认 --system current 用当前代码重新渲染
     system prompt、配录制的 user XML 重新请求 LLM，输出与录制行为的对比
     报告（动作序列 / 发言内容 / token / 延迟）。

已知边界（读报告时须心里有数）：
  - tool-catalog 内嵌在录制的 user XML 里——工具集变更不会反映进回放输入。
  - --system current 需要工具 .md 与代码同版本；回放对比只对 prompt 改动
    敏感，投影层改动需要重新采集快照。
  - 回放会真实计费调用 LLM；模型非确定性意味着单次差异要人工复核，
    连续多个用例同向漂移才是信号。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CASES_DIR = REPO_ROOT / "runtime_data" / "prompt_replay" / "cases"
DEFAULT_RUNS_DIR = REPO_ROOT / "runtime_data" / "prompt_replay" / "runs"
MEDIA_IMG_DIR = REPO_ROOT / "runtime_data" / "media" / "img"


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
        build_default_prompt_registry,
    )
    from qqbot.services.agent_loop.tools import build_default_registry

    registry = build_default_prompt_registry(
        tool_registry=build_default_registry()
    )
    scope = (scope_key or "group:0").split(":", 1)[0]
    return registry.render(scope=scope)


def _image_blocks(case: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """按快照记录的 hash 从媒体库（内容寻址）复原图片 blocks。"""
    from qqbot.services.agent_loop.image_utils import normalize_image_for_llm

    blocks: list[dict] = []
    missing: list[str] = []
    for image in case.get("images") or []:
        file_hash = image.get("hash", "")
        path = MEDIA_IMG_DIR / file_hash[:2] / file_hash
        if not path.is_file():
            missing.append(file_hash)
            continue
        data = path.read_bytes()
        data, mime = normalize_image_for_llm(
            data, image.get("mime") or "image/png"
        )
        b64 = base64.b64encode(data).decode("ascii")
        blocks.append({"type": "text", "text": f"↓ image hash={file_hash}"})
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )
    return blocks, missing


def _recorded_response(case: dict[str, Any]) -> str | None:
    """录制快照里最后一次成功往返的响应文本。"""
    for attempt in reversed(case.get("attempts") or []):
        text = attempt.get("response_text")
        if text:
            return text
    return None


def _action_types(response_text: str | None) -> list[str] | None:
    """响应文本 → 动作类型序列（与 planner 同一解析器）。None = 不可解析。"""
    if not response_text:
        return None
    from qqbot.services.agent_loop.llm_planner import (
        _parse_decision_output,
        _parse_json,
    )

    try:
        output = _parse_decision_output(_parse_json(response_text))
    except Exception:
        return None
    return [a.type for a in output.actions]


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

    human_content: list[dict] = [
        {"type": "text", "text": case.get("user_text", "")}
    ]
    missing_images: list[str] = []
    if args.with_images:
        blocks, missing_images = _image_blocks(case)
        human_content.extend(blocks)

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
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
    recorded_actions = _action_types(recorded_text)
    replay_actions = _action_types(response_text)

    return {
        "case": case_path.name,
        "scope_key": case.get("scope_key"),
        "tick_seq": case.get("tick_seq"),
        "system_mode": args.system,
        "system_prompt_chars": len(system_prompt),
        "recorded_system_prompt_chars": case.get("system_prompt_chars"),
        "missing_images": missing_images,
        "error": error,
        "latency_ms": latency_ms,
        "recorded_latency_ms": (case.get("attempts") or [{}])[-1].get(
            "latency_ms"
        ),
        "usage": usage,
        "recorded_usage": (case.get("attempts") or [{}])[-1].get("usage"),
        "recorded_actions": recorded_actions,
        "replay_actions": replay_actions,
        "actions_match": (
            recorded_actions == replay_actions
            if recorded_actions is not None and replay_actions is not None
            else None
        ),
        "recorded_response": recorded_text,
        "replay_response": response_text,
    }


def _write_report(out_dir: Path, results: list[dict[str, Any]]) -> Path:
    lines = [
        "# Prompt 回放报告",
        "",
        "| 用例 | scope | 动作一致 | 录制动作 | 回放动作 | 回放耗时 | 回放 tokens |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        match = {True: "✅", False: "❌", None: "—"}[r["actions_match"]]
        usage = r.get("usage") or {}
        lines.append(
            f"| {r['case']} | {r.get('scope_key') or '-'} | {match} "
            f"| {','.join(r['recorded_actions'] or []) or '-'} "
            f"| {','.join(r['replay_actions'] or []) or '-'} "
            f"| {r['latency_ms']}ms "
            f"| {usage.get('total_tokens', '-')} |"
        )
    mismatches = [r for r in results if r["actions_match"] is False]
    lines += [
        "",
        f"共 {len(results)} 例，动作不一致 {len(mismatches)} 例。",
        "动作一致只是第一道闸——发言内容 / 口吻 / 事实保持请打开各用例",
        "result JSON 对比 recorded_response 与 replay_response。",
    ]
    report = out_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


async def _amain(args: argparse.Namespace) -> int:
    cases = _load_cases(args)
    if not cases:
        return 1

    from qqbot.core.llm import create_llm
    from qqbot.core.time import china_now

    llm = await create_llm()
    if llm is None:
        print("LLM 未配置（LLM_API_KEY / LLM_MODEL）", file=sys.stderr)  # noqa: T201
        return 1

    out_dir = Path(args.out_dir) / china_now().strftime("%Y%m%dT%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for case_path in cases:
        print(f"replaying {case_path.name} ...")  # noqa: T201
        try:
            result = await _replay_one(llm, case_path, args)
        except Exception as exc:
            result = {
                "case": case_path.name,
                "error": f"replay crashed: {type(exc).__name__}: {exc}",
                "actions_match": None,
                "recorded_actions": None,
                "replay_actions": None,
                "latency_ms": -1,
            }
        results.append(result)
        (out_dir / f"{case_path.stem}.result.json").write_text(
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
        help="按 hash 从 runtime_data/media/img 复原图片一并发送",
    )
    parser.add_argument("--limit", type=int, default=0, help="至多回放 N 例")
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
