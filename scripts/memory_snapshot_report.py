"""记忆压缩快照报告 — S3 灰度评估（记忆系统契约 §8 S3）。

服务器端手动运行（读 PROMPT_SNAPSHOT_DIR 落盘的快照，本地 SFTP 工作区
没有快照数据）。纯 stdlib，不依赖运行时。用法：

    cd qqbot/
    python scripts/memory_snapshot_report.py                  # 默认快照目录
    python scripts/memory_snapshot_report.py --dir path/      # 指定目录
    python scripts/memory_snapshot_report.py --show-latest 3  # 附最新 3 份摘要正文

汇总维度（只看 kind=memory_compaction）：
  - outcome 分布（ok / parse_error / call_error）——解析失败率是 prompt
    质量的第一信号；
  - 预算命中：从 user_text 抠 <budget max_summary_chars>、从响应 JSON 量
    summary 字数——超预算出稿率高说明模型压不住，先改 prompt 再动预算
    （写库前另有句边界截断兜底，见 memory_compactor._summarize）；
  - 单请求契约：新快照 attempts 必须恰好为 1；>1 表示旧实现遗留或违约；
  - 每 scope 调用数 / 最新代次（由 previous-summary revision 推断）；
  - 延迟与 token 用量。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DIR = Path("runtime_data/prompt_snapshots")


def _load(directory: Path) -> list[dict]:
    snaps: list[dict] = []
    # 文件名以时间戳开头，字典序即时间序。
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("kind") == "memory_compaction":
            data["_file"] = path.name
            snaps.append(data)
    return snaps


def _summary_of(snap: dict) -> str | None:
    """从最后一次可解析的响应里抠 summary（与压缩器同样的宽松解析）。"""
    for attempt in reversed(snap.get("attempts") or []):
        text = attempt.get("response_text")
        if not text:
            continue
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("summary"), str):
            return data["summary"]
    return None


def _budget_of(snap: dict) -> int | None:
    m = re.search(r'max_summary_chars="(\d+)"', snap.get("user_text") or "")
    return int(m.group(1)) if m else None


def _revision_of(snap: dict) -> int:
    m = re.search(
        r'<previous-summary revision="(\d+)"', snap.get("user_text") or ""
    )
    return int(m.group(1)) + 1 if m else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="记忆压缩快照报告（S3 灰度评估）"
    )
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--show-latest", type=int, default=0, metavar="N")
    args = parser.parse_args()

    snaps = _load(args.dir)
    print(f"记忆压缩快照报告  目录={args.dir}  共 {len(snaps)} 份")
    if not snaps:
        print(
            "（无 kind=memory_compaction 快照——确认灰度已开且目标 scope "
            "在 PROMPT_SNAPSHOT_SCOPES 白名单内）"
        )
        return

    outcomes = Counter(s.get("outcome") or "unknown" for s in snaps)
    print("outcome:", "  ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    multi_attempts = sum(1 for s in snaps if len(s.get("attempts") or []) > 1)
    print(f"单触顶多请求（应为 0）: {multi_attempts}/{len(snaps)}")

    lengths: list[int] = []
    over_budget = 0
    for snap in snaps:
        summary = _summary_of(snap)
        if summary is None:
            continue
        lengths.append(len(summary))
        budget = _budget_of(snap)
        if budget is not None and len(summary) > budget:
            over_budget += 1
    if lengths:
        print(
            f"出稿: {len(lengths)} 份可解析  平均 {int(statistics.mean(lengths))} 字"
            f"  超预算出稿 {over_budget} 次（写库前有句边界截断兜底）"
        )

    latencies = sorted(
        a["latency_ms"]
        for s in snaps
        for a in (s.get("attempts") or [])
        if a.get("latency_ms")
    )
    if latencies:
        p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
        print(
            f"延迟: avg={int(statistics.mean(latencies))}ms  p95={p95}ms"
            f"  （共 {len(latencies)} 次往返）"
        )

    usage_total: Counter = Counter()
    for snap in snaps:
        for attempt in snap.get("attempts") or []:
            for key, value in (attempt.get("usage") or {}).items():
                if isinstance(value, int):
                    usage_total[key] += value
    if usage_total:
        print(
            "token 累计:",
            "  ".join(f"{k}={v}" for k, v in sorted(usage_total.items())),
        )

    by_scope: dict[str, list[dict]] = defaultdict(list)
    for snap in snaps:
        by_scope[snap.get("scope_key") or "?"].append(snap)
    print("按 scope:")
    for scope, items in sorted(by_scope.items()):
        latest = items[-1]
        print(
            f"  {scope}  calls={len(items)}"
            f"  最新代次≈{_revision_of(latest)}"
            f"  最近={latest.get('occurred_at')}"
        )

    if args.show_latest > 0:
        print("\n─── 最新摘要正文 ───")
        for snap in snaps[-args.show_latest :]:
            summary = _summary_of(snap) or "（响应不可解析）"
            print(
                f"\n[{snap.get('scope_key')}] 代次≈{_revision_of(snap)}"
                f"  {snap.get('occurred_at')}  文件={snap['_file']}"
            )
            print(summary)


if __name__ == "__main__":
    main()
