"""P1 sibling-batching unit tests (group_siblings + flatten_atomic_with_parent).

Run: python3 arc/p1_batch_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402


def node(nid, ntype="ATOMIC", children=None):
    return {"id": nid, "name": nid, "type": ntype, "children": children or []}


FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"[ok] {name}")
    else:
        FAILURES.append(name)
        print(f"[FAIL] {name} {detail}")


# --- fixture: mixed depth, long sibling run, root-level atomics ----------
TREE = node("ROOT", "FOLDER", [
    node("REQ-1", "FOLDER", [node("REQ-1.1"), node("REQ-1.2")]),
    node("REQ-2", "FOLDER", [
        node("REQ-2.1"),
        node("REQ-2.3", "FOLDER", [
            node("REQ-2.3.1"), node("REQ-2.3.2"), node("REQ-2.3.3"),
        ]),
        node("REQ-2.7", "FOLDER", [
            node("REQ-2.7.1"), node("REQ-2.7.2"), node("REQ-2.7.3"),
            node("REQ-2.7.4"), node("REQ-2.7.5"),
            node("REQ-2.7.6", "FOLDER", [
                node("REQ-2.7.6.1"), node("REQ-2.7.6.2"),
            ]),
        ]),
    ]),
    node("REQ-9", "ATOMIC"),  # root-level atomic leaf
])

EXPECTED_ORDER = [
    "REQ-1.1", "REQ-1.2", "REQ-2.1", "REQ-2.3.1", "REQ-2.3.2", "REQ-2.3.3",
    "REQ-2.7.1", "REQ-2.7.2", "REQ-2.7.3", "REQ-2.7.4", "REQ-2.7.5",
    "REQ-2.7.6.1", "REQ-2.7.6.2", "REQ-9",
]

flat = [str(n.get("id")) for n in main.flatten_atomic(TREE)]
check("flatten order preserved", flat == EXPECTED_ORDER, f"got {flat}")

with_parent = main.flatten_atomic_with_parent(TREE)
parents = {str(n.get("id")): p for n, p in with_parent}
check("parent tracking",
      parents["REQ-1.1"] == "REQ-1" and parents["REQ-2.7.6.1"] == "REQ-2.7.6"
      and parents["REQ-2.3.1"] == "REQ-2.3" and parents["REQ-9"] == "ROOT",
      f"got {parents}")
check("with_parent order matches flatten",
      [str(n.get("id")) for n, _ in with_parent] == EXPECTED_ORDER)

# max_batch=1 ⇒ one leaf per batch (legacy rollback)
b1 = main.group_siblings(TREE, 1)
check("batch=1 legacy equivalence",
      [[str(n.get("id")) for n in b] for b in b1]
      == [[i] for i in EXPECTED_ORDER])

# max_batch=0/negative clamp to 1
check("batch=0 clamps to legacy",
      main.group_siblings(TREE, 0) and
      all(len(b) == 1 for b in main.group_siblings(TREE, 0)))

# max_batch=2: REQ-1 pair batches; REQ-2.7 ×5 → 2+2+1; 2.7.6 pair intact
b2 = [[str(n.get("id")) for n in b] for b in main.group_siblings(TREE, 2)]
expected_b2 = [
    ["REQ-1.1", "REQ-1.2"],
    ["REQ-2.1"],
    ["REQ-2.3.1", "REQ-2.3.2"],
    ["REQ-2.3.3"],
    ["REQ-2.7.1", "REQ-2.7.2"],
    ["REQ-2.7.3", "REQ-2.7.4"],
    ["REQ-2.7.5"],
    ["REQ-2.7.6.1", "REQ-2.7.6.2"],
    ["REQ-9"],
]
check("batch=2 grouping+chunking", b2 == expected_b2, f"got {b2}")
check("batch=2 flattened == document order",
      [i for b in b2 for i in b] == EXPECTED_ORDER)

# max_batch=4 upper clamp: a run of 6 would still split at 4
long_tree = node("R", "FOLDER", [
    node("P", "FOLDER", [node(f"L{i}") for i in range(1, 7)]),
])
bclamp = main.group_siblings(long_tree, 9)
check("max clamp at 4", [len(b) for b in bclamp] == [4, 2],
      f"got {[len(b) for b in bclamp]}")

# no cross-parent mixing even when adjacent
cross_tree = node("R", "FOLDER", [
    node("A", "FOLDER", [node("A.1")]),
    node("B", "FOLDER", [node("B.1")]),
])
bcross = [[str(n.get("id")) for n in b] for b in main.group_siblings(cross_tree, 2)]
check("no cross-parent batch", bcross == [["A.1"], ["B.1"]], f"got {bcross}")

# --- cap coupling formula (mirrors the inline logic) ----------------------
def coupled_cap(env_value, max_batch):
    cap_base = int(env_value) if env_value else 100
    return max(cap_base, 40 * max_batch)

check("cap coupling: batch2 → 100", coupled_cap("", 2) == 100)
check("cap coupling: batch3 → 120", coupled_cap("", 3) == 120)
check("cap coupling: batch4 → 160", coupled_cap("", 4) == 160)
check("cap coupling: explicit env wins when larger", coupled_cap("200", 2) == 200)
check("cap coupling: explicit 50 raised by batch3", coupled_cap("50", 3) == 120)

# --- turn_wall semantics (replicated from the closure) --------------------
import math
def turn_wall_like(budget, node_wall, remaining, reserve, n):
    scaled = node_wall * max(1, n)
    if budget > 0:
        return max(120, min(scaled, int(remaining - reserve)))
    return max(120, scaled)

check("wall unlimited: batch2 = 7200",
      turn_wall_like(0, 3600, math.inf, 0, 2) == 7200)
check("wall unlimited: batch1 = 3600",
      turn_wall_like(0, 3600, math.inf, 0, 1) == 3600)
check("wall finite budget clamps",
      turn_wall_like(5400, 3600, 1500.0, 600, 2) == 900)
check("wall floor 120",
      turn_wall_like(5400, 3600, 610.0, 600, 2) == 120)


# --- build_spec_run_cmd (run6 fixes: per-batch filter + verification rules) ---
cmd = main.build_spec_run_cmd("/path/to/cli.js", ["A.spec.ts", "B.spec.ts"])
check("spec cmd: files appended as filters",
      cmd.rstrip().endswith("--reporter=line A.spec.ts B.spec.ts"), cmd[-80:])
check("spec cmd: green-stop marker (.arc/acceptance) present",
      ".arc/acceptance/playwright.config.ts" in cmd)
check("spec cmd: git rollback banned",
      "git checkout/reset/restore" in cmd)
# 09-27 目标优先定则:测试用例由模型根据需求自生成——旧"禁止自写
# Playwright"政策翻转,新政策=自写 spec 走被批准命令形状。
check("spec cmd: model-authored specs sanctioned",
      "your OWN Playwright spec" in cmd and "explicit file argument" in cmd,
      cmd[-120:])

cmd_full = main.build_spec_run_cmd("/path/to/cli.js")
# 5623442 起 VERIFICATION_RULES 指引提及 MY-CHECK.spec.ts(正文合法);
# 无过滤断言只看命令行(末行)本身,不再扫整段提示词。
cmd_full_line = cmd_full.rstrip().splitlines()[-1]
check("spec cmd: no filter → full suite (final verification)",
      cmd_full_line.endswith("--reporter=line")
      and not cmd_full_line.endswith(".spec.ts"))

cmd_fallback = main.build_spec_run_cmd(None, ["A.spec.ts"])
check("spec cmd: npx fallback carries filter",
      cmd_fallback.rstrip().endswith("--reporter=line A.spec.ts")
      and "npx --yes @playwright/test" in cmd_fallback)

check("spec cmd: empty file entries dropped",
      main.build_spec_run_cmd("/c", ["", "A.spec.ts"]).rstrip()
      .endswith("--reporter=line A.spec.ts"))

print()
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    raise SystemExit(1)
print("all P1 batching tests passed")
