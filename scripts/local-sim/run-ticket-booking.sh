#!/usr/bin/env bash
# 用 hackathon-local-simulation 跑真实案例 ticket-booking 的完整流程:
# 构建 local 镜像(复用重建的 base)→ 组装 workspace → 容器内跑 agent → Playwright 评测。
# 用法: run-ticket-booking.sh <run-name>   (workspace 落在 ~/arc-sim-runs/<run-name>)
set -euo pipefail

SIM=/Volumes/PSSD/CodeProjects/hackathon-local-simulation
ARC=/Volumes/PSSD/CodeProjects/octos-arc
RUN_NAME="${1:?用法: run-ticket-booking.sh <run-name>}"
WORKSPACE="$HOME/arc-sim-runs/$RUN_NAME"

if docker image inspect arcbench-runner:local-base >/dev/null 2>&1; then
  echo "== base 镜像已就绪,复用 arcbench-runner:local-base"
else
  echo "== base 镜像缺失,先构建(amd64)"
  (cd "$ARC/scripts/local-sim" && docker build --platform linux/amd64 \
    -t arcbench-runner:local-base .)
fi

echo "== 构建 local wrapper 镜像"
cd "$SIM"
ARCBENCH_LOCAL_PLATFORM=linux/amd64 ./build-image.sh

echo "== 模拟提交: $RUN_NAME"
rm -rf "$WORKSPACE"
status=0
python3 "$SIM/local_submit.py" run \
  --competition ticket-booking --task ticket-booking \
  --agent "$ARC/octos-arc-bundle.zip" \
  --requirements-dir "$ARC/arc/tasks/ticket-booking--ticket-booking" \
  --tests-dir "$ARC/arc/public-tests/ticket-booking--ticket-booking" \
  --workspace "$WORKSPACE" \
  --env-file "$SIM/.env" || status=$?

# 宿主侧消费 .arc/ 追踪工件(只读摘要, 不影响判定/退出码)。
if [ -d "$WORKSPACE/template/.arc" ]; then
  echo
  echo "== trace summary"
  python3 "$ARC/scripts/local-sim/trace-summary.py" "$WORKSPACE" || true
fi
exit $status
