#!/bin/sh
# evolve 本地迭代入口(设计文档 arc/docs/evolve-design.md §11):
# 外层闭环 evolve_loop.py 的薄壳——.env 只承载既有 LLM/端口/预算配置
# (与 create 轮同一份,零 evolve 专属环境变量),evolve 参数全部显式
# 传给 API。
#
# 用法:
#   sh scripts/run-evolution.sh github [max_rounds]
#   sh scripts/run-evolution.sh sheet  [max_rounds]
#   sh scripts/run-evolution.sh github 3 --dry-run   # 仅装配预览
set -e
cd "$(dirname "$0")/.."

PROJ="${1:?usage: run-evolution.sh github|sheet [max_rounds] [--dry-run]}"
ROUNDS="${2:-3}"
shift 2 2>/dev/null || shift $#

TASK="arc/tasks/hackathon-evolution--${PROJ}"
PROJECT="arc/hackathon-projects/${PROJ}"

if [ ! -f "$TASK/requirements.yaml" ]; then
    echo "task not found: $TASK" >&2; exit 1
fi
if [ ! -d "$PROJECT/backend/src" ]; then
    echo "project not found: $PROJECT" >&2; exit 1
fi

# 既有 LLM/端口/预算通道(.env)——evolve 不新增任何环境变量。
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

exec python3 arc/evolve_loop.py "$TASK" --project "$PROJECT" \
    --max-rounds "$ROUNDS" "$@"
