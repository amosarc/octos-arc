#!/usr/bin/env python3
"""统计 arc-output run 的 LLM token 消耗(纯标准库,只读)。

用法:
    python3 measure_tokens.py arc/arc-output/<run> [run2 ...]
    python3 measure_tokens.py --all arc/arc-output/

口径与 2026-09-17 基线一致:遍历 <run>/octos-logs/**/llm.jsonl(旧布局 .octos/context-logs/*/llm.jsonl 兼容)的
kind=llm_response 记录,累加 usage;缺 cache_read_tokens 键按 0 计。

缓存失效口径(cache<1000 视为未命中):miss=未命中请求数;fresh=未命中请求的
input_tokens 合计(全价部分,2026-09-18 tok-ticket-1 实测 25 次 miss 占输入
52%,是首要优化对象)。

基线(agnes-3.0-flash,planctl 栈):
    ticket-plan-r4  calls=184  in+cache=10.04M  out=49.5K  22.1min  评分 10/10
    ticket-plan-r1  calls=138  in+cache= 7.90M  out=44.7K  33.6min
    ticket-plan-r2  calls=165  in+cache= 8.90M  out=43.2K  44.6min
    ticket-plan-r3  calls=292  in+cache=17.68M  out=91.1K  71.9min (失控上界)
    smoke-plan-r2   calls= 80  in+cache= 2.53M  out=19.4K   6.4min  评分 1/1
    smoke-plan-r1   calls= 77  in+cache= 2.03M  out=24.7K  10.7min
"""

from __future__ import annotations

import json
import sys
import collections
from pathlib import Path


def _llm_log_files(run: Path) -> list:
    """新布局 octos-logs/(可见,与 backend 同级)优先,兼容旧 .octos/。"""
    files = list(run.glob("octos-logs/**/llm.jsonl"))
    files += run.glob(".octos/context-logs/*/llm.jsonl")
    return sorted(set(files))


def _has_llm_logs(run: Path) -> bool:
    return (run / "octos-logs").is_dir() or (run / ".octos").is_dir()


def scan_run(run: Path) -> dict:
    tot: collections.Counter = collections.Counter()
    tools: collections.Counter = collections.Counter()
    calls = 0
    for f in _llm_log_files(run):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("kind") != "llm_response":
                continue
            calls += 1
            u = r.get("usage") or {}
            cache_read = u.get("cache_read_tokens") or 0
            tin = u.get("input_tokens") or 0
            tot["in"] += tin
            tot["cache"] += cache_read
            tot["out"] += u.get("output_tokens") or 0
            tot["ms"] += r.get("duration_ms") or 0
            if cache_read < 1000:
                tot["miss_calls"] += 1
                tot["fresh"] += tin
            for t in r.get("tool_calls") or []:
                tools[t.get("name", "?")] += 1
    return {"calls": calls, "tot": tot, "tools": tools}


def fmt_n(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}K"


def main(argv: list[str]) -> int:
    runs: list[Path] = []
    for a in argv:
        p = Path(a)
        if a == "--all" or (p.is_dir() and not _has_llm_logs(p)):
            runs.extend(sorted(x for x in p.iterdir() if _has_llm_logs(x)))
        else:
            runs.append(p)
    if not runs:
        print(__doc__)
        return 2
    print(f"{'run':<22}{'calls':>6}{'in':>10}{'cache':>10}{'in+cache':>10}"
          f"{'out':>9}{'miss':>6}{'fresh':>10}{'llm_min':>9}  top-tools")
    for run in runs:
        r = scan_run(run)
        tot = r["tot"]
        top = " ".join(f"{k}={v}" for k, v in r["tools"].most_common(5))
        print(f"{run.name:<22}{r['calls']:>6}{fmt_n(tot['in']):>10}"
              f"{fmt_n(tot['cache']):>10}{fmt_n(tot['in'] + tot['cache']):>10}"
              f"{fmt_n(tot['out']):>9}{int(tot['miss_calls']):>6}"
              f"{fmt_n(tot['fresh']):>10}{tot['ms'] / 60000:>9.1f}  {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
