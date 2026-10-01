"""真题 requirements.yaml → 独立 .feature 文件(每直接含原子节点的
FOLDER 一个 Feature),落盘 context-sim-out/gherkin/<task>/。

与管线共用 arc/gherkin.py 的确定性清洗;审查两道真题的 Gherkin 形态
时跑这个,不发任何请求。

用法:python3 scripts/yaml_to_gherkin.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "arc"))

import gherkin  # noqa: E402
import main  # noqa: E402

TASKS = ["hackathon--sheet", "hackathon--github"]
OUT = ROOT / "context-sim-out" / "gherkin"


def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for task in TASKS:
        req_dir = ROOT / "arc" / "tasks" / task
        if not (req_dir / "requirements.yaml").is_file():
            print(f"[skip] {task}: requirements.yaml missing")
            continue
        tree = main.load_requirement_tree(req_dir)
        feats = gherkin.feature_files(tree)
        out_dir = OUT / task
        out_dir.mkdir(parents=True, exist_ok=True)
        n_scenarios = 0
        for f in feats:
            (out_dir / f"{f['id']}.feature").write_text(f["text"],
                                                        encoding="utf-8")
            n_scenarios += f["text"].count("Scenario:")
        print(f"[{task}] {len(feats)} feature file(s), "
              f"{n_scenarios} scenario(s) -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
