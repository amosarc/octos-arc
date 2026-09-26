#!/usr/bin/env python3
"""A/B 对比:read 历史折叠 开/关 两次 run 的 token 账本对比。

用法: python3 fold-ab-compare.py <run-on> <run-off>

读各 run 的 .arc/octos-events.jsonl,输出:
- token_estimate 曲线(每 turn)与 Σ(≈总输入 token 下界)
- 工具调用分布(read_file 重复读计数、bash 次数)
- STALE READ 存根与 [nudge] 的出现次数(验证折叠/软纠正生效)
- 时长与失败调用
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path


def load(run: str):
    root = Path.home() / "arc-sim-runs" / run
    f = root / "template" / ".arc" / "octos-events.jsonl"
    if not f.is_file():
        f = root / "octos-logs" / "octos-events.jsonl"
    started: dict[str, tuple[str, dict]] = {}
    ctx_series: list[int] = []
    calls: Counter = Counter()
    read_paths: Counter = Counter()
    stale_stubs = 0
    nudges = 0
    fails = Counter()
    preview_stale = 0
    first_ts = last_ts = None
    for line in f.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        m, p = e.get("method"), e.get("params", {})
        if m == "context/normalization_reported":
            te = (p.get("context_state") or {}).get("token_estimate") or (
                p.get("context") or {}
            ).get("state", {}).get("token_estimate")
            if te:
                ctx_series.append(te)
        elif m == "tool/started":
            started[p["tool_call_id"]] = (p.get("tool_name"), p.get("arguments", {}))
            calls[p.get("tool_name")] += 1
            if p.get("tool_name") == "read_file":
                read_paths[p.get("arguments", {}).get("path", "?")] += 1
        elif m == "tool/completed":
            name, _args = started.get(p.get("tool_call_id"), (None, None))
            if name:
                if not p.get("success", True):
                    fails[name] += 1
                prev = p.get("output_preview", "")
                if "[STALE READ]" in prev:
                    preview_stale += 1
                if "[nudge]" in prev:
                    nudges += 1
    rereads = sum(c for c in read_paths.values() if c >= 3)
    return {
        "run": run,
        "series": ctx_series,
        "total_input_est": sum(ctx_series),
        "peak": max(ctx_series) if ctx_series else 0,
        "calls": calls,
        "read_total": calls.get("read_file", 0),
        "reread_calls": rereads,
        "hot_files": read_paths.most_common(5),
        "stale_in_preview": preview_stale,
        "nudges": nudges,
        "fails": dict(fails),
    }


def fmt_row(label: str, on: object, off: object):
    def s(v):
        return f"{v:,}" if isinstance(v, int) else str(v)

    delta = ""
    if isinstance(on, int) and isinstance(off, int) and off:
        pct = 100 * (on - off) / off
        delta = f"  ({pct:+.1f}% vs off)"
    print(f"  {label:34s} on={s(on):>14s}  off={s(off):>14s}{delta}")


def main():
    run_on, run_off = sys.argv[1], sys.argv[2]
    on, off = load(run_on), load(run_off)
    print(f"===== A/B: read 历史折叠  ON={run_on}  OFF={run_off}\n")
    fmt_row("总输入 token(Σ快照,下界)", on["total_input_est"], off["total_input_est"])
    fmt_row("上下文峰值", on["peak"], off["peak"])
    fmt_row("turn 数(快照数)", len(on["series"]), len(off["series"]))
    fmt_row("read_file 调用", on["read_total"], off["read_total"])
    fmt_row("其中重复读(≥3次文件)", on["reread_calls"], off["reread_calls"])
    fmt_row("bash 调用", on["calls"].get("bash", 0), off["calls"].get("bash", 0))
    fmt_row("edit_file 调用", on["calls"].get("edit_file", 0), off["calls"].get("edit_file", 0))
    fmt_row("失败调用", sum(on["fails"].values()), sum(off["fails"].values()))
    print("\n  ON 侧折叠/软纠正信号: [STALE READ] 出现:", on["stale_in_preview"],
          "| [nudge] 出现:", on["nudges"])
    print("  OFF 侧应无: [STALE READ]:", off["stale_in_preview"],
          "| [nudge]:", off["nudges"])
    print("\n  ON 曲线:", on["series"])
    print("  OFF 曲线:", off["series"])


if __name__ == "__main__":
    main()
