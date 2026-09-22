#!/usr/bin/env python3
"""宿主侧 trace 消费端(本地模拟的验证工具, 不进 agent 包)。

"宿主尽算"的消费面演示: agent 只往 .arc/ 追加原始工件——
  .arc/runner-events.jsonl   状态事件 + 刷新信号
  .arc/octos-events.jsonl    内核每 turn 的原始协议帧
  .arc/traceability/*.json   快照表
本脚本在 run 结束后把它们聚合成人类可读摘要。所有计算都在这里,
agent 侧零聚合。同时兼容旧调试位置 octos-logs/octos-events.jsonl,
历史 run 也能用。

用法:
  python3 trace-summary.py <workspace-or-project-dir> [--json]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


def find_project_dir(root: Path) -> Path:
    """workspace 根下取 template/, 直接给项目目录则原样返回。"""
    if (root / "template" / ".arc").is_dir():
        return root / "template"
    return root


def iter_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_timestamp(value: str) -> float:
    """ISO 时间戳('2026-09-21T18:29:43.702+00:00' 或 'YYYY-MM-DD HH:MM:SS')→ epoch 秒。"""
    from datetime import datetime
    text = str(value).replace(" ", "T")
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.timestamp()
    except ValueError:
        return 0.0


def summarize_runner_events(path: Path) -> dict:
    """节点状态轨迹 / runner_state 序列 / signal 计数。"""
    node_timeline: list[str] = []
    runner_states: list[str] = []
    signals = collections.Counter()
    other_writers = collections.Counter()  # runner 自己写的行(无 type 字段)
    for row in iter_jsonl(path):
        kind = row.get("type")
        if kind == "requirement_state":
            message = " ".join(str(row.get("message") or "").split())
            if len(message) > 80:
                message = message[:77] + "..."
            node_timeline.append(
                f"{row.get('timestamp', '?'):23s} {row.get('node_id', '?'):12s} "
                f"{row.get('phase', '?'):10s} {row.get('status', '?'):10s} {message}")
        elif kind == "runner_state":
            runner_states.append(f"{row.get('timestamp', '?')} {row.get('state')}")
        elif kind == "signal":
            signals[row.get("reason", "?")] += 1
        else:
            other_writers[str(row.get("stage", "unknown-writer"))] += 1
    return {
        "node_timeline": node_timeline,
        "runner_states": runner_states,
        "signals": dict(signals),
        "other_writer_lines": dict(other_writers),
    }


def summarize_octos_events(path: Path) -> dict:
    """协议帧统计 + 按 turn 聚合(工具调用数 / token / 近似耗时)。"""
    method_counts = collections.Counter()
    tool_counts = collections.Counter()
    turns: dict[str, dict] = {}
    started_at: dict[str, float] = {}
    completed_order: list[str] = []
    errors: list[str] = []

    for row in iter_jsonl(path):
        method = row.get("method", "?")
        params = row.get("params") or {}
        method_counts[method] += 1
        turn_id = params.get("turn_id")
        if method == "turn/started":
            started_at[turn_id] = parse_timestamp(params.get("timestamp", ""))
            turns.setdefault(turn_id, {"tools": 0, "tool_failures": 0})
        elif method == "tool/started":
            tool_counts[str(params.get("tool_name", "?"))] += 1
            if turn_id in turns:
                turns[turn_id]["tools"] += 1
        elif method == "tool/completed":
            if turn_id in turns and not params.get("success", True):
                turns[turn_id]["tool_failures"] += 1
        elif method == "turn/completed":
            entry = turns.setdefault(turn_id, {"tools": 0, "tool_failures": 0})
            entry["tokens_in"] = params.get("tokens_in")
            entry["tokens_out"] = params.get("tokens_out")
            entry["result"] = params.get("session_result")
            completed_order.append(turn_id)
        elif method == "turn/error":
            errors.append(f"{params.get('code', '?')}: {params.get('message', '')}")
            completed_order.append(turn_id)

    # 每 turn 近似耗时: turn/started 时间戳 → 同 turn 的下一帧 turn/started
    # 之前(协议流按序落盘), 取后续 turn 的 started 时间差; 最后一个 turn
    # 无法闭合, 标 None。
    start_seq = list(started_at.items())
    for i, (tid, t0) in enumerate(start_seq):
        entry = turns.get(tid)
        if entry is None or t0 <= 0:
            continue
        t1 = start_seq[i + 1][1] if i + 1 < len(start_seq) and start_seq[i + 1][1] > 0 else None
        entry["approx_seconds"] = round(t1 - t0, 1) if t1 else None

    total_in = sum(e.get("tokens_in") or 0 for e in turns.values())
    total_out = sum(e.get("tokens_out") or 0 for e in turns.values())
    return {
        "total_frames": sum(method_counts.values()),
        "method_counts": dict(method_counts.most_common()),
        "tool_counts": dict(tool_counts.most_common()),
        "turns": turns,
        "turn_display_order": [tid for tid, _ in start_seq],
        "turn_order": completed_order,
        "turn_errors": errors,
        "tokens_in": total_in,
        "tokens_out": total_out,
    }


def load_node_states(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def humanize(summary: dict) -> str:
    project = summary["project_dir"]
    lines = []
    lines.append(f"== trace summary: {project}")
    lines.append("")

    runner = summary["runner_events"]
    lines.append(f"-- runner-events.jsonl ({len(runner['node_timeline'])} node transitions, "
                 f"{len(runner['runner_states'])} runner states)")
    for row in runner["runner_states"]:
        lines.append(f"   {row}")
    for row in runner["node_timeline"]:
        lines.append(f"   {row}")
    if runner["signals"]:
        lines.append(f"   signals: {runner['signals']}")
    lines.append("")

    octos = summary["octos_events"]
    lines.append(f"-- octos-events.jsonl ({octos['total_frames']} frames, "
                 f"{len(octos['turns'])} turns, "
                 f"tokens in/out {octos['tokens_in']}/{octos['tokens_out']})")
    lines.append("   frames by method:")
    for method, count in octos["method_counts"].items():
        lines.append(f"     {count:7d}  {method}")
    lines.append("   top tools:")
    for name, count in list(octos["tool_counts"].items())[:12]:
        lines.append(f"     {count:7d}  {name}")
    lines.append("   turns:")
    for index, tid in enumerate(octos.get("turn_display_order") or [], 1):
        entry = octos["turns"].get(tid, {})
        approx = entry.get("approx_seconds")
        approx_text = f"{approx:>8.1f}s" if approx is not None else "     n/a "
        outcome = entry.get("result") or ("(error)" if tid in octos.get("turn_order", []) else "(cut: no terminal frame)")
        lines.append(f"     #{index:02d} {tid[:8]} tools={entry.get('tools', 0):4d} "
                     f"fail={entry.get('tool_failures', 0):3d} "
                     f"in={entry.get('tokens_in') or 0:8d} out={entry.get('tokens_out') or 0:7d} "
                     f"{approx_text} {outcome}")
    for err in octos["turn_errors"]:
        lines.append(f"     turn/error {err}")
    lines.append("")

    states = summary["node_states"]
    if states:
        lines.append(f"-- traceability node_states (final snapshot, {len(states)} nodes)")
        for node_id, row in sorted(states.items()):
            lines.append(f"   {node_id:12s} {row.get('state', '?'):12s} "
                         f"(updated {row.get('updated_at', '?')})")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="workspace 根(含 template/)或项目目录(含 .arc/)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本摘要")
    args = parser.parse_args()

    project = find_project_dir(args.root.expanduser().resolve())
    arc_dir = project / ".arc"
    octos_events = arc_dir / "octos-events.jsonl"
    legacy_octos_events = project / "octos-logs" / "octos-events.jsonl"
    used_legacy = False
    if not octos_events.is_file() and legacy_octos_events.is_file():
        octos_events = legacy_octos_events
        used_legacy = True

    summary = {
        "project_dir": str(project),
        "octos_events_path": str(octos_events),
        "used_legacy_octos_events": used_legacy,
        "runner_events": summarize_runner_events(arc_dir / "runner-events.jsonl"),
        "octos_events": summarize_octos_events(octos_events),
        "node_states": load_node_states(arc_dir / "traceability" / "node_states.json"),
        "debug_log_lines": _count_lines(arc_dir / "debug.log"),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(humanize(summary))
        if summary["debug_log_lines"]:
            print(f"\n-- debug.log: {summary['debug_log_lines']} lines")
        if used_legacy:
            print("\n(note: octos-events.jsonl read from legacy octos-logs/ location)")
    return 0


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


if __name__ == "__main__":
    sys.exit(main())
