#!/usr/bin/env python3
"""evolve 外层自优化闭环(设计文档 arc/docs/evolve-design.md §6)。

每轮:记 git HEAD → 调宿主显式 API run_evolve(第 1 轮全家族;后续轮
families 收窄到失败家族)→ 三门判定 → 达标即停;不达标→确定性教训提炼
(零 LLM)追加进 <project>/octos/evolution-lessons.md → 下一轮,直到
达标或轮上限(默认 3,到顶如实报告)。

约束:核心 CHARTER 规则不变异——进化只发生在数据层教训文件(可审、可
diff、用户可随时手工修订);全程 fix-forward,绝不 git reset/revert
项目;零环境变量(参数全显式);LLM 链路经 main.py 现行通道零改动。

用法(本地迭代入口,scripts/run-evolution.sh 即此命令的薄壳):
  python arc/evolve_loop.py <task-dir> --project <project-dir> \
      [--max-rounds 3] [--keep-lessons-rounds 3] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main as arc_main  # noqa: E402  (宿主显式 API 唯一集成面)

LESSONS_HEADER = "evolution lessons (host-appended after each round; " \
                 "edit freely — your edits join the next round)"


def lessons_path(project_dir: Path) -> Path:
    return Path(project_dir) / "octos" / "evolution-lessons.md"


def _git_head(project_dir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(project_dir),
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _doom_tail(project_dir: Path, round_started: datetime,
               limit: int = 6) -> list[str]:
    """本轮时间段内的 doom 取证行(clock.iso() 北京时间 ISO 串;
    解析失败的行保守收入末尾 limit 条——宁可多报不漏报)。"""
    f = Path(project_dir) / "octos" / "doom-events.jsonl"
    if not f.is_file():
        return []
    picked: list[str] = []
    unparsable: list[str] = []
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for ln in lines[-60:]:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            unparsable.append(ln[:200])
            continue
        if ts.tzinfo is not None:
            # clock.iso() 带 +08:00;round_started 是 naive 本地时间
            # (本地即北京时区)——剥时区统一口径(轮 1 崩溃点)。
            ts = ts.replace(tzinfo=None)
        if ts >= round_started:
            cmd = str(rec.get("command") or "")[:120]
            picked.append(
                f"{rec.get('node')}: {rec.get('reason', '')[:80]}"
                + (f" [{rec.get('tool')} x{rec.get('count')}: {cmd}]"
                   if rec.get("tool") else ""))
    out = picked[-limit:] + unparsable[-1:]
    return out


def extract_lessons(verdict, project_dir: Path, round_no: int,
                    round_started: datetime) -> list[str]:
    """确定性教训提炼(零 LLM):三门证据 → 子弹行。信号缺位不造词。"""
    lines: list[str] = [f"- gates: suite="
                        f"{'pass' if verdict.suite and verdict.suite['failed'] == 0 and verdict.suite['passed'] > 0 else 'fail'}"
                        f" walk="
                        f"{'pass' if verdict.walk and not verdict.walk['errors'] and not verdict.walk['missing'] else 'fail'}"
                        f" batches="
                        f"{'pass' if verdict.batch_verdicts and all(v == 'done' for v in verdict.batch_verdicts.values()) else 'fail'}"]
    if verdict.suite:
        reds = verdict.suite.get("failed_titles") or []
        if reds:
            fams = sorted({
                arc_main._spec_family(f) or "?"
                for f in verdict.suite.get("failed_files") or []})
            lines.append(
                f"- self-suite red ({verdict.suite['failed']}): families "
                f"{', '.join(fams)} — " + "; ".join(reds[:8])
                + ("; …" if len(reds) > 8 else ""))
    if verdict.walk:
        if verdict.walk.get("missing"):
            lines.append(
                "- walk: unreachable from home: "
                + ", ".join(map(str, verdict.walk["missing"][:12])))
        if verdict.walk.get("errors"):
            lines.append(
                "- walk errors: "
                + "; ".join(map(str, verdict.walk["errors"][:4])))
    blocked = [nid for nid, v in (verdict.batch_verdicts or {}).items()
               if v != "done"]
    if blocked:
        lines.append(
            "- batches not done: " + ", ".join(blocked[:12]))
    if verdict.phase0:
        p0 = verdict.phase0
        reds = (p0.get("suite") or {}).get("failed")
        lines.append(
            f"- phase0: turn {'ok' if p0.get('turn_ok') else 'blocked'}"
            f" ({p0.get('turn_reason', '?')}), after "
            f"{p0.get('repair_turns', 0)} repair turn(s) "
            f"{(p0.get('suite') or {}).get('passed', '?')}p/"
            f"{reds if reds is not None else '?'}f")
    doom = _doom_tail(project_dir, round_started)
    for d in doom:
        lines.append(f"- doom: {d}")
    cov = verdict.coverage or {}
    lines.append(
        f"- coverage: {cov.get('covered', '?')}/{cov.get('total', '?')} "
        "evolution scenario(s) matched a passing self-test")
    return lines


def append_lessons(path: Path, round_no: int, lines: list[str],
                   keep_rounds: int = 3,
                   now: datetime | None = None) -> None:
    """追加一轮段并只留最近 keep_rounds 段(裁剪=删本文件自己写的旧段,
    项目零触碰)。空文件先落头部说明。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    section = (f"\n\n## Round {round_no} ({stamp})\n"
               + "\n".join(lines) + "\n")
    if not text.strip():
        text = f"# {LESSONS_HEADER}\n"
    text += section
    parts = re.split(r"(?m)^## Round (\d+) ", text)
    # parts = [head, n1, body1, n2, body2, ...]
    if len(parts) > 3:
        head = parts[0]
        rounds = [(int(parts[i]), parts[i + 1])
                  for i in range(1, len(parts) - 1, 2)]
        keep = rounds[-keep_rounds:]
        text = head + "".join(
            f"\n\n## Round {n} " + body.rstrip("\n") + "\n"
            for n, body in keep)
    path.write_text(text, encoding="utf-8")


def _all_families(task_dir: Path, project_dir: Path | None = None) -> list[str]:
    """家族名单唯一解析源=工作区 requirements 副本(main() 按 on_platform
    语义维护;首轮无副本时回退任务目录)——不旁路直读原始任务目录。"""
    base = Path(task_dir)
    if project_dir is not None:
        ws = Path(project_dir) / "requirements"
        if (ws / "requirements.yaml").is_file() or \
                (ws / "requirements.yml").is_file():
            base = ws
    tree = arc_main.load_requirement_tree(base)
    return [str(c.get("id")) for c in tree.get("children") or []
            if isinstance(c, dict) and c.get("id")]


def run_evolution_rounds(task_dir: str | Path, project_dir: str | Path, *,
                         max_rounds: int = 3,
                         keep_lessons_rounds: int = 3,
                         dry_run: bool = False,
                         start_round: int = 1,
                         families: list[str] | None = None,
                         time_budget: int | None = None,
                         log=print) -> list:
    """外层闭环主体(显式 API;每轮一次 run_evolve=一次完整宿主跑)。

    统一迭代策略(10-07 拍板):不区分修复轮/迭代轮——每轮一律全家族
    文档序从 REQ-1 出发,模型自检(spec+截图)已实现即跳过、红则修;
    不做宿主侧跨轮跳批与自动收窄(families 仅作运维手动覆盖)。
    start_round>1=崩溃后续号;time_budget=每轮秒数(显式 API)。"""
    task_dir = Path(task_dir).resolve()
    project_dir = Path(project_dir).resolve()
    families_all = _all_families(task_dir, project_dir)
    log(f"[evolve-loop] task={task_dir.name} project={project_dir.name} "
        f"families={','.join(families_all)} max_rounds={max_rounds} "
        f"start_round={start_round}")
    verdicts: list = []
    last_round = start_round + max_rounds - 1
    for round_no in range(start_round, last_round + 1):
        round_started = datetime.now()
        head = _git_head(project_dir)
        log(f"[evolve-loop] round {round_no} starting "
            f"(families={','.join(families) if families else 'ALL'}, "
            f"git HEAD={head[:8] if head else 'none'})")
        try:
            verdict = arc_main.run_evolve(
                task_dir, project_dir,
                families=families,
                max_repair_turns=2,
                dry_run=dry_run,
                round_index=round_no,
                time_budget=time_budget,
                log=log)
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001 — 轮级兜底
            import traceback
            traceback.print_exc()
            log(f"[evolve-loop] round {round_no} crashed "
                f"({type(exc).__name__}: {exc}); synthesizing failed "
                "verdict and continuing")
            verdict = arc_main.EvolveVerdict(
                ok=False, round_index=round_no,
                phase0={"turn_reason": f"run crashed: {exc}"})
        if not isinstance(verdict, arc_main.EvolveVerdict):
            # 异常路径(main 兜底返回 int):按失败轮处理,工件面取盘上。
            log(f"[evolve-loop] round {round_no} returned no verdict "
                f"({verdict!r}); treating as failed round")
            verdict = arc_main.EvolveVerdict(
                ok=False, round_index=round_no,
                phase0={"turn_reason": "run aborted (no verdict)"})
        verdicts.append(verdict)
        if verdict.dry_run:
            log("[evolve-loop] dry-run complete; no rounds to iterate")
            break
        if verdict.ok:
            log(f"[evolve-loop] round {round_no} GREEN — target met, "
                "loop ends")
            break
        lessons = extract_lessons(verdict, project_dir, round_no,
                                  round_started)
        append_lessons(lessons_path(project_dir), round_no, lessons,
                       keep_rounds=keep_lessons_rounds)
        log(f"[evolve-loop] round {round_no} RED; lessons appended -> "
            f"{lessons_path(project_dir)}")
        if round_no == last_round:
            log(f"[evolve-loop] round cap (round {last_round}) reached; "
                "stopping with artifacts on disk")
            break
        log("[evolve-loop] next round: full document order from REQ-1 "
            "(uniform strategy; the model self-verifies green families)")
    return verdicts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="evolve outer self-optimizing loop (explicit args "
                    "only — no environment variables)")
    parser.add_argument("task_dir")
    parser.add_argument("--project", required=True,
                        help="existing built project dir "
                             "(e.g. arc/hackathon-projects/github)")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--keep-lessons-rounds", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-round", type=int, default=1,
                        help="resume numbering (lessons file keeps prior "
                             "rounds; e.g. 2 after a crashed round 1)")
    parser.add_argument("--families", default=None,
                        help="explicit scope for the FIRST round "
                             "(comma-separated; narrowing takes over "
                             "afterwards)")
    parser.add_argument("--time-budget", type=int, default=None,
                        help="per-round budget seconds (explicit API, "
                             "takes precedence over OCTOS_TIME_BUDGET)")
    args = parser.parse_args(argv)
    verdicts = run_evolution_rounds(
        args.task_dir, args.project,
        max_rounds=args.max_rounds,
        keep_lessons_rounds=args.keep_lessons_rounds,
        dry_run=args.dry_run,
        start_round=args.start_round,
        families=[f.strip() for f in (args.families or "").split(",")
                  if f.strip()] or None,
        time_budget=args.time_budget)
    ok = bool(verdicts and verdicts[-1].ok)
    print(f"[evolve-loop] final: "
          f"{'GREEN' if ok else 'RED'} after {len(verdicts)} round(s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
