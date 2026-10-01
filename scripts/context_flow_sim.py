"""上下文流程模拟器:真实需求 yaml × 真实装配代码 × mock LLM。

不发起任何模型请求。把一次完整 run 里模型实际看到的提示词栈逐字节
落盘(两形式,用户参考件 arc/prompts/*):

  00a 系统提示词-gateway 层(main._GATEWAY_PROMPT_OVERRIDE → gateway.md,
      内核 base 替换:6 工具对齐 + 工具纪律)
  00b 系统行 AGENTS.md(build_system_row_context:Background + children
      map + RULES & ARCHITECTURE + 任务 FIXTURE 段)
      ——内核装配顺序:gateway base → 日期行 → AGENTS.md → memory(压缩态)
  t01.. 每家族 TURN(BOOTSTRAP 前言[首 turn] + Gherkin Feature + 完成要求
      一句话)

每个文件附标注为 MOCK 的脚本化回复,构成完整对话轮。

用法:python3 scripts/context_flow_sim.py [--out context-sim-out]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "arc"))

import fixture_manifest  # noqa: E402
import main  # noqa: E402
import plan_envelope  # noqa: E402

TASKS = ["hackathon--sheet", "hackathon--github"]
SMOKE = 3100
TOOLCHAIN_CLI = "/tmp/octos-local-grader/node_modules/@playwright/test/cli.js"


def _mock(tag: str) -> str:
    return (f"\n\n---\n[MOCK MODEL RESPONSE — 本次模拟未发起任何模型请求,"
            f"以下为脚本化回复,仅用于展示对话轮完整形态]\n{tag}\n")


def _header(no: str, title: str) -> str:
    return f"# [turn {no}] {title}\n\n"


def render_task(task: str, out_root: Path) -> None:
    task_dir = ROOT / "arc" / "tasks" / task
    out = out_root / task
    out.mkdir(parents=True, exist_ok=True)
    files: list[tuple[Path, str]] = []

    tree = main.load_requirement_tree(task_dir)
    family_scope = main.parse_family_scope(main.COMPETITION_FAMILY_SCOPE)
    family_of = main.family_index(tree)
    node_batches = main.group_families(tree, 6)
    if family_scope:
        node_batches = [b for b in node_batches
                        if main.batch_in_scope(b, family_scope, family_of)]

    # scope 化树(与 main 一致):fixture 段/种子基线只看 scope 内家族
    scoped_tree = tree
    if family_scope:
        kept = [c for c in (tree.get("children") or [])
                if c and main.batch_in_scope(main.flatten_atomic(c),
                                             family_scope, family_of)]
        scoped_tree = {**tree, "children": kept}

    parent_of: dict[str, dict] = {}

    def _index_parents(node: dict, parent: dict) -> None:
        for c in node.get("children") or []:
            if not isinstance(c, dict):
                continue
            if str(c.get("type", "")).upper() == "ATOMIC":
                parent_of[str(c.get("id"))] = parent
            else:
                _index_parents(c, c)

    _index_parents(tree, tree)

    fixture_data = fixture_manifest.extract(scoped_tree)
    try:
        fixture_compiled = fixture_manifest.compile_seed(fixture_data)
    except Exception:
        fixture_compiled = None
    fixture_stats = {
        "uncovered": len((fixture_compiled or {}).get("uncovered") or ()),
        "arrange": len((fixture_compiled or {}).get("arrange") or ()),
        "seeded": len(((fixture_compiled or {}).get("db") or {})
                      .get("users", [])),
        "canonical": bool((fixture_compiled or {}).get("canonical")),
        "kind": (fixture_compiled or {}).get("kind") or "none"}
    fixture_rules = fixture_manifest.render_extra_rules(
        fixture_data, fixture_stats)
    # 任务层贡献规则与全局法则经 build_rules 去重合并(与 main 同构)
    task_rules = fixture_manifest.task_rules(fixture_data, fixture_stats)

    # ---- 00a gateway 层(内核 base 替换) ----
    body = _header("00a", "系统提示词-gateway 层(内核 base 替换)")
    files.append((out / "00a-system-prompt-gateway.md",
                  body + main._GATEWAY_PROMPT_OVERRIDE
                  + _mock("(gateway 层不产生回复——常驻上下文)。")))

    # ---- 00b 系统行 AGENTS.md ----
    rules_text = plan_envelope.render_rules(task_rules)
    system_row = main.build_system_row_context(
        tree, rules_text, fixture_rules, scope=family_scope or None)
    body = _header("00b", "系统行 AGENTS.md(总纲:Background + children map "
                          "+ RULES & ARCHITECTURE + FIXTURE)")
    files.append((out / "00b-system-row-AGENTS.md",
                  body + system_row
                  + _mock("(系统行不产生回复——常驻上下文)。")))

    turn_no = 0

    def next_no() -> str:
        nonlocal turn_no
        turn_no += 1
        return f"{turn_no:02d}"

    # ---- 会话调度(真实 group_families + scope 过滤;family policy) ----
    sessions: list[dict] = []
    for batch in node_batches:
        fid = str(family_of[str(batch[0].get("id"))].get("id"))
        if sessions and sessions[-1]["family"] == fid:
            sessions[-1]["batches"].append(batch)
        else:
            sessions.append({"family": fid, "batches": [batch]})

    completion = main.GOAL_FIRST_TURN_COMPLETION.format(smoke=SMOKE)

    for si, sess in enumerate(sessions, 1):
        fid = sess["family"]
        for bi, batch in enumerate(sess["batches"], 1):
            ids = [str(n.get("id")) for n in batch]
            features = main.build_turn_features(batch, parent_of)
            prompt = features
            if si == 1 and bi == 1:
                prompt = main.BOOTSTRAP_PREAMBLE + prompt
            prompt += completion
            no = next_no()
            title = (f"家族会话 {si}({fid})批 {bi}/{len(sess['batches'])}"
                     f" TURN — 节点 {', '.join(ids)}")
            body = _header(no, title)
            reply = (f"(mock) 已实现 {', '.join(ids)}:spec 先行写绿,"
                     f"冷启动自测全绿,节点收尾。")
            files.append((out / f"t{no}-{fid}-b{bi}-turn.md",
                          body + prompt + _mock(reply)))

    for path, text in files:
        Path(path).write_text(text, encoding="utf-8")
    print(f"[{task}] {len(files)} file(s) -> {out}"
          f" | batches={len(node_batches)} sessions={len(sessions)}")


def run_sim() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "context-sim-out"))
    args = ap.parse_args()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "README.md").write_text(
        "# context-sim-out\n\n"
        "真实需求 yaml × 真实装配代码 × mock LLM 的全轮次提示词落盘。\n"
        "系统提示词栈(模型真实视野,按装配顺序):00a gateway 层(内核 base "
        "替换)→ 日期行(内核自加)→ 00b AGENTS.md 总纲 → memory(压缩态)。\n"
        "每个 t*/n*/r*/b1/f1 文件 = 宿主发给模型的完整用户消息(逐字节出自"
        "装配函数)+ 标注为 MOCK 的回复。\n"
        "未覆盖:内核 harness 检查点打回、wall/auth 切断提示、SPEC REGRESSION"
        "(仅 staged-specs 任务)。\n",
        encoding="utf-8")
    for task in TASKS:
        render_task(task, out_root)
    return 0


if __name__ == "__main__":
    sys.exit(run_sim())
