#!/usr/bin/env python3
"""octos context-log token report — 省.token 工作的统一验收标尺.

输入：一次运行的产品目录（含 .octos/context-logs/<run>/）、单个 run 目录、
或直接一个 llm.jsonl；也可以是包含多个 run 的父目录（逐 run 报告 + 汇总）。

输出指标：
  - 全价输入 Σinput_tokens、缓存读取 Σcache_read_tokens、输出 Σoutput_tokens
  - 等比计费输入 = 全价 + 缓存读取×cache_read_ratio（默认 0.1）
  - full-miss（cache_read 为 0/缺失且 input 超阈值）次数与 token
  - 压缩事件：优先读 context_log 的 compaction_event 字段；旧日志用
    历史字节数骤降（>30%）启发式
  - tail 膨胀率 = Σinput_tokens ÷ Σ真实增量 token（相邻请求逐消息 hash
    差集计算追加量；octos 口径：ASCII/4 + 非 ASCII/1.5）
  - 历史体积曲线（首/峰/末）；tools.jsonl 的 per-tool 调用次数/输出字节/截断

用法：
  python3 scripts/token_report.py arc/arc-output/ticket-booking--xxx
  python3 scripts/token_report.py arc/arc-output/            # 多 run 汇总
  python3 scripts/token_report.py <run>/llm.jsonl --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

FULL_MISS_MIN_INPUT = 15_000
COMPACTION_DROP_RATIO = 0.30


def estimate_tokens(text: str) -> int:
    """octos-llm estimate_tokens 口径：ASCII/4 + 非 ASCII/1.5。"""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return int(math.ceil(ascii_chars / 4 + non_ascii / 1.5))


def json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def content_hash(value) -> str:
    return hashlib.md5(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_llm_jsonl(path: Path):
    requests, responses = {}, {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind")
            if kind == "llm_request":
                requests[rec.get("iteration")] = rec
            elif kind == "llm_response":
                responses[rec.get("iteration")] = rec
    return requests, responses


def message_hashes(request: dict):
    return [
        (m.get("role"), content_hash(m.get("content")))
        for m in request.get("messages") or []
    ]


def appended_tokens(prev_request: dict, curr_request: dict) -> int:
    """相邻两次请求之间真实追加/重写的 token 量。

    用位置对齐的最长公共消息前缀（LCP）计算：LCP 之后的 curr 侧是追加，
    prev 侧剩余是重写（压缩/修剪），两者都要在下一次请求里按全价重付。
    不能用 set 差集——重复内容（空回复、相同占位）会误判为"无新增"。
    """
    prev_hashes = message_hashes(prev_request)
    curr_hashes = message_hashes(curr_request)
    lcp = 0
    for a, b in zip(prev_hashes, curr_hashes):
        if a != b:
            break
        lcp += 1
    appended_chars = [
        json.dumps(m.get("content"), ensure_ascii=False)
        for m in (curr_request.get("messages") or [])[lcp:]
    ]
    rewritten = len(prev_hashes) - lcp
    if rewritten > 0:
        appended_chars += [
            json.dumps(m.get("content"), ensure_ascii=False)
            for m in (prev_request.get("messages") or [])[lcp:]
        ]
    return estimate_tokens("".join(appended_chars)) if appended_chars else 0


def analyze_run(llm_path: Path, cache_read_ratio: float) -> dict:
    requests, responses = load_llm_jsonl(llm_path)
    tools_path = llm_path.parent / "tools.jsonl"
    iterations = sorted(set(requests) & set(responses))

    total_in = total_cache_read = total_cache_write = total_out = 0
    full_misses = []
    compactions = []
    appended_total = 0
    hist_sizes = []
    prev_req = None

    for it in iterations:
        req, resp = requests[it], responses[it]
        usage = resp.get("usage") or {}
        in_tok = usage.get("input_tokens") or 0
        cr = usage.get("cache_read_tokens")
        cw = usage.get("cache_write_tokens") or 0
        out_tok = usage.get("output_tokens") or 0
        total_in += in_tok
        total_cache_read += cr or 0
        total_cache_write += cw
        total_out += out_tok

        if not cr and in_tok >= FULL_MISS_MIN_INPUT:
            full_misses.append({"iteration": it, "input_tokens": in_tok})

        hist = sum(json_bytes(m.get("content")) for m in req.get("messages") or [])
        hist_sizes.append({"iteration": it, "bytes": hist})

        if prev_req is not None:
            # 压缩事件：字段优先，旧日志用骤降启发式
            if req.get("compaction_event") and (req["compaction_event"] or {}).get("triggered"):
                compactions.append({"iteration": it, **req["compaction_event"]})
            elif prev_req is not None and hist_sizes:
                prev_hist = hist_sizes[-2]["bytes"]
                if prev_hist and hist < prev_hist * (1 - COMPACTION_DROP_RATIO):
                    compactions.append({
                        "iteration": it, "path": "heuristic_drop",
                        "from_bytes": prev_hist, "to_bytes": hist,
                    })
            appended_total += appended_tokens(prev_req, req)
        prev_req = req

    billed_equivalent_input = total_in + total_cache_read * cache_read_ratio
    tool_stats = load_tools_jsonl(tools_path) if tools_path.exists() else {}

    return {
        "run": llm_path.parent.name if llm_path.name == "llm.jsonl" else llm_path.stem,
        "llm_calls": len(iterations),
        "input_tokens": total_in,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "output_tokens": total_out,
        "cache_read_ratio": cache_read_ratio,
        "billed_equivalent_input": int(billed_equivalent_input),
        "full_misses": full_misses,
        "full_miss_tokens": sum(m["input_tokens"] for m in full_misses),
        "compactions": compactions,
        "appended_tokens_est": appended_total,
        "tail_inflation": round(total_in / appended_total, 2) if appended_total else None,
        "history_first_bytes": hist_sizes[0]["bytes"] if hist_sizes else 0,
        "history_peak_bytes": max((h["bytes"] for h in hist_sizes), default=0),
        "history_last_bytes": hist_sizes[-1]["bytes"] if hist_sizes else 0,
        "tools": tool_stats,
    }


def load_tools_jsonl(path: Path) -> dict:
    calls, out_bytes, truncated = {}, {}, {}
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            idx = 0
            while idx < len(line):
                try:
                    rec, end = dec.raw_decode(line, idx)
                except json.JSONDecodeError:
                    break
                name = rec.get("tool") or "unknown"
                calls[name] = calls.get(name, 0) + 1
                out_bytes[name] = out_bytes.get(name, 0) + len(str(rec.get("output") or ""))
                if rec.get("output_truncated"):
                    truncated[name] = truncated.get(name, 0) + 1
                idx = end
                while idx < len(line) and line[idx] in " \t":
                    idx += 1
    return {
        name: {"calls": calls[name], "output_bytes": out_bytes[name],
               "truncated": truncated.get(name, 0)}
        for name in sorted(calls, key=lambda n: -out_bytes[n])
    }


def find_llm_jsonls(target: Path) -> list[Path]:
    if target.name == "llm.jsonl":
        return [target]
    if target.is_dir():
        direct = target / "llm.jsonl"
        if direct.exists():
            return [direct]
        return sorted(target.glob("*/.octos/context-logs/*/llm.jsonl")) + sorted(
            target.glob(".octos/context-logs/*/llm.jsonl"))
    raise SystemExit(f"cannot locate llm.jsonl under {target}")


def print_report(rep: dict) -> None:
    print(f"== {rep['run']} ==")
    print(f"  LLM 调用           : {rep['llm_calls']}")
    print(f"  全价输入           : {rep['input_tokens']:,} tok")
    print(f"  缓存读取           : {rep['cache_read_tokens']:,} tok")
    if rep["cache_write_tokens"]:
        print(f"  缓存写入           : {rep['cache_write_tokens']:,} tok")
    print(f"  输出               : {rep['output_tokens']:,} tok")
    share = 100 * (rep["output_tokens"] * 5) / max(
        rep["billed_equivalent_input"] + rep["output_tokens"] * 5, 1)
    print(f"  等比计费输入       : {rep['billed_equivalent_input']:,} tok"
          f"（缓存读取按 {rep['cache_read_ratio']} 折）")
    print(f"  输出等比占比       : {share:.0f}%")
    fm = rep["full_misses"]
    if fm:
        iters = ", ".join(f"iter{m['iteration']}({m['input_tokens']:,})" for m in fm)
        print(f"  full-miss          : {len(fm)} 次 {rep['full_miss_tokens']:,} tok [{iters}]")
    else:
        print("  full-miss          : 0")
    comp = rep["compactions"]
    if comp:
        detail = ", ".join(
            f"iter{c['iteration']}({c.get('path', '?')})" for c in comp)
        print(f"  压缩事件           : {len(comp)} 次 [{detail}]")
    else:
        print("  压缩事件           : 0")
    ti = rep["tail_inflation"]
    if ti:
        print(f"  tail 膨胀率        : {ti}x"
              f"（Σin {rep['input_tokens']:,} ÷ 增量估算 {rep['appended_tokens_est']:,}）")
    print(f"  历史体积 B         : 首 {rep['history_first_bytes']:,} / "
          f"峰 {rep['history_peak_bytes']:,} / 末 {rep['history_last_bytes']:,}")
    tools = rep.get("tools") or {}
    if tools:
        total_out = sum(t["output_bytes"] for t in tools.values())
        top = list(tools.items())[:5]
        summary = ", ".join(
            f"{n} {t['calls']}次/{t['output_bytes']:,}B" for n, t in top)
        print(f"  工具输出 top5      : {summary}（共 {total_out:,}B）")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", help="run 目录 / context-logs 父目录 / llm.jsonl")
    ap.add_argument("--json", action="store_true", help="输出 JSON（多 run 为数组）")
    ap.add_argument("--cache-read-ratio", type=float, default=0.1,
                    help="缓存读取计价折扣（默认 0.1）")
    args = ap.parse_args()

    paths = find_llm_jsonls(Path(args.target))
    if not paths:
        raise SystemExit("no llm.jsonl found")
    reports = [analyze_run(p, args.cache_read_ratio) for p in paths]

    if args.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0],
                         ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print_report(rep)

    if len(reports) > 1:
        agg_in = sum(r["input_tokens"] for r in reports)
        agg_cache = sum(r["cache_read_tokens"] for r in reports)
        agg_out = sum(r["output_tokens"] for r in reports)
        agg_miss = sum(r["full_miss_tokens"] for r in reports)
        print("== 汇总 ==")
        print(f"  runs={len(reports)}  全价输入={agg_in:,}  缓存读取={agg_cache:,}  "
              f"输出={agg_out:,}  full-miss tok={agg_miss:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
