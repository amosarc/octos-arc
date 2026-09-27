#!/usr/bin/env bash
# 用 hackathon-local-simulation 跑任一题目的通用入口:
#   run-case.sh <run-name> <requirements-dir> <tests-dir>
# workspace 落在 ~/arc-sim-runs/<run-name>;模型配置读模拟器目录的 .env。
set -euo pipefail

SIM=/Volumes/PSSD/CodeProjects/hackathon-local-simulation
ARC=/Volumes/PSSD/CodeProjects/octos-arc
RUN_NAME="${1:?用法: run-case.sh <run-name> <requirements-dir> <tests-dir> [env-file]}"
REQUIREMENTS="${2:?缺少 requirements-dir}"
TESTS="${3:?缺少 tests-dir}"
# 第 4 参可选:env 文件覆盖(默认模拟器 .env)。用于传 OCTOS_ARC_FAMILIES
# 等宿主开关进容器——在副本 .env 上追加即可,不动原始配置。
ENV_FILE="${4:-$SIM/.env}"
WORKSPACE="$HOME/arc-sim-runs/$RUN_NAME"

if ! docker image inspect arcbench-runner:local-base >/dev/null 2>&1; then
  echo "== base 镜像缺失,先构建(amd64)"
  (cd "$ARC/scripts/local-sim" && docker build --platform linux/amd64 \
    -t arcbench-runner:local-base .)
fi
# 模拟器上游(2026-09-23 4e62690)把基础镜像引用改成了
# gyataro/arcbench-runner:local-base;双名指向同一镜像,新旧版本模拟器都兼容。
docker tag arcbench-runner:local-base gyataro/arcbench-runner:local-base 2>/dev/null || true

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
  --env-file "$ENV_FILE" || status=$?

# 宿主侧消费 .arc/ 追踪工件(只读摘要, 不影响判定/退出码)。
if [ -d "$WORKSPACE/template/.arc" ]; then
  echo
  echo "== trace summary"
  python3 "$ARC/scripts/local-sim/trace-summary.py" "$WORKSPACE" || true
fi
exit $status
