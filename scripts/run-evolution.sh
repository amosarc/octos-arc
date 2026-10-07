#!/bin/sh
# evolve 本地迭代入口(设计文档 arc/docs/evolve-design.md §11):
# 外层闭环 evolve_loop.py 的薄壳——.env 只承载既有 LLM/端口/预算配置
# (与 create 轮同一份,零 evolve 专属环境变量),evolve 参数全部显式
# 传给 API。
#
# 基线三态(10-08 拍板:接续为默认,重掷须显式):
#   默认        接续——PROJECT 原地续跑,轮号由 evolve-verdicts 自动推导
#               (evolve_loop --start-round 缺省);已跑过的工程从此单调
#               累积而不是重掷全部场景(sheet-0.9→1.0 重掷 7 分净额实证)。
#   --fresh     重掷——现 PROJECT 归档为 <PROJECT>.archived-<ts>,从初始
#               工程重拷,轮号回 1。
#   --base DIR  定基——现 PROJECT 归档后从 DIR(上一轮交付)拷入,接续
#               语义照旧(保留 .git 与 octos/,排除 node_modules)。
#   --initial DIR  重掷的初始源(默认 arc/initial-projects/<proj>)。
#
# 用法:
#   sh scripts/run-evolution.sh github [max_rounds] [--fresh|--base DIR]
#   sh scripts/run-evolution.sh github 3 --dry-run   # 仅装配预览
set -e
cd "$(dirname "$0")/.."

PROJ="${1:?usage: run-evolution.sh github|sheet [max_rounds] [--fresh|--base DIR]}"
ROUNDS="${2:-3}"
shift 2 2>/dev/null || shift $#

TASK="arc/tasks/hackathon-evolution--${PROJ}"
PROJECT="arc/hackathon-projects/${PROJ}"

# 工作区三态参数从余参里摘出,其余原样透传 evolve_loop.py。
FRESH=0
BASE=""
INITIAL="arc/initial-projects/${PROJ}"
PASS_THROUGH=""
for arg in "$@"; do
    case "$arg" in
        --fresh) FRESH=1 ;;
        --base) BASE=1 ;;                      # 值在下一参,下方再取
        *) if [ "$BASE" = "1" ] && [ -z "$BASE_DIR" ]; then
               BASE_DIR="$arg"; BASE=2
           else
               PASS_THROUGH="$PASS_THROUGH $arg"
           fi ;;
    esac
done
BASE="$BASE_DIR"

if [ ! -f "$TASK/requirements.yaml" ]; then
    echo "task not found: $TASK" >&2; exit 1
fi

archive_project() {
    if [ -d "$PROJECT" ]; then
        ARCH="${PROJECT}.archived-$(date +%Y%m%d-%H%M%S)"
        mv "$PROJECT" "$ARCH"
        echo "[run-evolution] archived prior project -> $ARCH"
    fi
}

copy_in() {  # $1=源目录;排除 node_modules(装回靠 npm,磁盘 ~200MB/工程)
    mkdir -p "$PROJECT"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude node_modules "$1"/ "$PROJECT"/
    else
        cp -R "$1"/ "$PROJECT"/
        find "$PROJECT" -name node_modules -type d -prune -exec rm -rf {} + 2>/dev/null || true
    fi
    echo "[run-evolution] seeded project from $1"
}

if [ "$FRESH" = "1" ]; then
    if [ ! -d "$INITIAL" ]; then
        echo "initial project not found: $INITIAL (pass --initial <dir>)" >&2
        exit 1
    fi
    archive_project
    copy_in "$INITIAL"
elif [ -n "$BASE" ]; then
    if [ ! -d "$BASE" ]; then
        echo "base dir not found: $BASE" >&2; exit 1
    fi
    archive_project
    copy_in "$BASE"
fi

if [ ! -d "$PROJECT/backend/src" ]; then
    echo "project not found: $PROJECT (seed it with --fresh or --base)" >&2
    exit 1
fi

# 既有 LLM/端口/预算通道(.env)——evolve 不新增任何环境变量。
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

# shellcheck disable=SC2086
exec python3 arc/evolve_loop.py "$TASK" --project "$PROJECT" \
    --max-rounds "$ROUNDS" $PASS_THROUGH
