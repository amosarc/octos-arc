#!/usr/bin/env python3
"""从平台评分器 playwright-report.json 确定性提取基线修复档案
(baseline-issues.md;evolve Phase 0 注入件,设计文档 §7)。

分桶:①Could not find a visible navigation target named "X"(按目标名
分组列场景)②Test timeout of Nms exceeded ③其余(按错误首行)。输出
带出处头的 markdown;一次生成、人工核校后随任务目录提交。

用法:
  python scripts/make_baseline_dossier.py <playwright-report.json> \
      <output baseline-issues.md>
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

NAV_RE = re.compile(
    r'Could not find a visible navigation target named "([^"]+)"')
TIMEOUT_RE = re.compile(r"Test timeout of (\d+)ms exceeded")


def _walk_tests(node: dict, out: list) -> None:
    for spec in node.get("specs") or []:
        fname = str(spec.get("file") or "").rsplit("/", 1)[-1]
        for t in spec.get("tests") or []:
            results = t.get("results") or []
            if not results:
                continue
            r = results[-1]
            if r.get("status") == "expected":
                continue
            raw = t.get("titlePath") or [t.get("title", "")]
            title = " › ".join(str(x) for x in raw if x)
            err = ""
            for e in r.get("errors") or []:
                err = str(e.get("message") or "")
                if err:
                    break
            out.append((fname, title, err))


def collect(report: dict) -> dict:
    tests: list[tuple[str, str, str]] = []

    def walk(node: dict) -> None:
        _walk_tests(node, tests)
        for sub in node.get("suites") or []:
            walk(sub)

    for s in report.get("suites") or []:
        walk(s)
    nav: dict[str, list[str]] = defaultdict(list)
    timeouts: list[str] = []
    other: dict[str, list[str]] = defaultdict(list)
    for fname, title, err in tests:
        m = NAV_RE.search(err)
        if m:
            nav[m.group(1)].append(f"{title} ({fname})")
            continue
        if TIMEOUT_RE.search(err):
            timeouts.append(f"{title} ({fname})")
            continue
        key = err.splitlines()[0][:120] if err else "(no error message)"
        other[key].append(f"{title} ({fname})")
    return {"tests": tests, "nav": nav, "timeouts": timeouts,
            "other": other}


def render(report: dict, collected: dict) -> str:
    stats = report.get("stats") or {}
    total = (stats.get("expected") or 0) + (collected and 0 or 0)
    failed = len(collected["tests"])
    lines = [
        "# Baseline issues (platform grader, verbatim failures)",
        "",
        f"- Source: platform grader playwright-report.json, started "
        f"{stats.get('startTime', '?')}; {failed} failed test(s) listed "
        "below, bucketed by the grader's own error text.",
        "- The mount telemetry from the same window shows several of "
        "these names DID appear in the DOM — the failures are "
        "visibility/at-the-moment/accessible-name mismatches and "
        "unreachable tabs, NOT missing features. Fix visibility, "
        "synchronous rendering, single-hop reachability, and flow "
        "length (10s per scenario).",
        "",
        "## Navigation targets the grader could not find "
        f"({sum(len(v) for v in collected['nav'].values())} failures)",
        "",
    ]
    for name in sorted(collected["nav"],
                       key=lambda n: -len(collected["nav"][n])):
        titles = collected["nav"][name]
        lines.append(f'- "{name}" — {len(titles)} scenario(s):')
        for t in titles:
            lines.append(f"  - {t}")
    lines += [
        "",
        f"## Hard timeouts ({len(collected['timeouts'])} failures, "
        "10s per-scenario cap)",
        "",
    ]
    for t in collected["timeouts"]:
        lines.append(f"- {t}")
    if collected["other"]:
        lines += ["", "## Other failures", ""]
        for key, titles in collected["other"].items():
            lines.append(f"- {key} — {len(titles)} scenario(s):")
            for t in titles:
                lines.append(f"  - {t}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    src = Path(argv[1])
    dst = Path(argv[2])
    report = json.loads(src.read_text(encoding="utf-8"))
    collected = collect(report)
    dst.write_text(render(report, collected), encoding="utf-8")
    nav_total = sum(len(v) for v in collected["nav"].values())
    print(f"[dossier] {dst}: {nav_total} nav-target + "
          f"{len(collected['timeouts'])} timeout + "
          f"{sum(len(v) for v in collected['other'].values())} other "
          f"({len(collected['tests'])} failed total)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
