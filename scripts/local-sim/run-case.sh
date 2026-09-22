#!/usr/bin/env bash
# 用 hackathon-local-simulation 跑任一题目的通用入口:
#   run-case.sh <run-name> <requirements-dir> <tests-dir>
# workspace 落在 ~/arc-sim-runs/<run-name>;模型配置读模拟器目录的 .env。
set -euo pipefail

SIM=/Volumes/PSSD/CodeProjects/hackathon-local-simulation
ARC=/Volumes/PSSD/CodeProjects/octos-arc
RUN_NAME="${1:?用法: run-case.sh <run-name> <requirements-dir> <tests-dir>}"
REQUIREMENTS="${2:?缺少 requirements-dir}"
TESTS="${3:?缺少 tests-dir}"
WORKSPACE="$HOME/arc-sim-runs/$RUN_NAME"

if ! docker image inspect arcbench-runner:local-base >/dev/null 2>&1; then
  echo "== base 镜像缺失,先构建(amd64)"
  (cd "$ARC/scripts/local-sim" && docker build --platform linux/amd64 \
    -t arcbench-runner:local-base .)
fi

if ! docker image inspect arcbench-local-submit:latest >/dev/null 2>&1; then
  echo "== 构建 local wrapper 镜像"
  ARCBENCH_LOCAL_PLATFORM=linux/amd64 "$SIM/build-image.sh"
fi

echo "== 模拟提交: $RUN_NAME (requirements=$REQUIREMENTS)"
rm -rf "$WORKSPACE"
status=0
python3 "$SIM/local_submit.py" run \
  --competition "$(basename "$(dirname "$REQUIREMENTS")")" \
  --task "$(basename "$REQUIREMENTS")" \
  --agent "$ARC/octos-arc-bundle.zip" \
  --requirements-dir "$REQUIREMENTS" \
  --tests-dir "$TESTS" \
  --workspace "$WORKSPACE" \
  --env-file "$SIM/.env" || status=$?

# 宿主侧消费 .arc/ 追踪工件(只读摘要, 不影响判定/退出码)。
if [ -d "$WORKSPACE/template/.arc" ]; then
  echo
  echo "== trace summary"
  python3 "$ARC/scripts/local-sim/trace-summary.py" "$WORKSPACE" || true
fi
exit $status
