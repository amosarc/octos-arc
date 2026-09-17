# arc/ — 在这一个仓库里完成 ARC-Bench 的改、测、交

这个目录是 Octos 参加 ARC-Bench 的全部外围：平台适配包、公开验收测试、本地做题和打分脚本。
学员只需要这一个仓库。内核源码在上一级（`crates/`），适配包在这里。

## 五步

```sh
# 0. 准备（一次）
pip install -r arc/requirements.txt          # pyyaml、arcbench-runtime
export ARCBENCH_API_KEY=ak_...               # arc-bench.com 个人页的 API key
export NODE_BIN=/opt/homebrew/opt/node@24/bin # 你的 node 目录（Linux 一般不用设）

# 1. 拿一个 Octos 二进制：官方版，或自己编的魔改版
export OCTOS_BIN=/path/to/octos              # 不设则用 ../target/release/octos
cargo build --release -p octos-cli --no-default-features --features api   # 编魔改版时

# 2. 本机做题（约 5 分钟，不到一分钱）
python3 arc/run-task-local.py arc/tasks/smoke--counter --name try1

# 3. 用平台原版 Playwright 测试打分（首次会自动装 Playwright）
python3 arc/grade-local.py arc/arc-output/try1 smoke--counter

# 4. 改一处：main.py 的提示词 / 环境变量，octos_stdio.py 的启动参数，或 crates/ 里的内核
#    改完回到第 2、3 步，改前改后各跑一次，比数字

# 5. 打包上传
sh arc/pack.sh                               # 得到 octos-arc-bundle.zip
# 到 arc-bench.com 对应比赛页 New submission 上传，模型填 deepseek-v4-flash，
# Base URL 填 https://api.arc-bench.com/v1，然后选题、Run
```

## 改了内核怎么让平台用上

平台运行时按 `main.py` 里 `OCTOS_RELEASE_URL` 现场下载 Octos。改了 `crates/` 之后必须：
编译 Linux x86_64 版 → 在本仓库发一个 Release 挂上 tar.gz → 把 `OCTOS_RELEASE_URL` 改成那个地址 → 重新 `pack.sh` 上传。
否则平台跑的仍是官方版，改了等于没改。

## 目录

| 文件 | 作用 |
|---|---|
| `main.py` | 平台入口：读需求、驱动 Octos、整理产物、上报进度 |
| `octos_stdio.py` | 通过 `octos serve --stdio` 驱动内核 |
| `public-tests/<题目>/` | 平台公开的 Playwright 验收测试（会自动喂给模型） |
| `tasks/<题目>/` | 各题需求文件的离线副本 |
| `run-task-local.py` / `grade-local.py` / `pack.sh` | 本机做题、打分、打包 |

已知平台细节：容器里 `/workspace/tests` 有验收测试；订票题的测试默认连 3301 端口而平台起在 3000，`main.py` 会要求后端两个端口都监听；容器到 npmjs 很慢，提示词要求零依赖并走 npmmirror。

## 宿主 Planning 信号节点(planctl / `[arc-plan]`)

`plan_envelope.py` 把需求树、验收契约、端口与 host_signals 编成 envelope,经
`[arc-plan]` marker 随 turn 下发;内核 ≥2.0.3(planctl)剥离 marker 并把计划渲染进
系统行尾部常驻段,写类工具批次后以 `<arc-workspace>` 尾块推送增量信号。门控:
`octos --version` 基元组 ≥(2,0,3)(rc 计入);`OCTOS_ASSUME_ARC_PLAN=0` 强制旧提示词
(基线实测),`=1` 强制开启;旧 v2.0.2 内核自动回退旧提示词,不会裸漏 JSON。

环境开关:`OCTOS_ARC_PLAN`(内核 marker 协议,缺省开)、`OCTOS_ARC_HOST_SCAFFOLD`
(宿主落盘基础设施五件套,缺省开)、`OCTOS_ARC_HOST_GRADE`(final 轮 Tier-2 宿主
跑 staged Playwright,缺省关)、`OCTOS_ARC_WINDOW_TOKENS` /
`OCTOS_ARC_PLAN_FULL_RATIO=0.50` / `OCTOS_ARC_PLAN_SKELETON_RATIO=0.62`(压力降级)。

### Linux x86_64 发布(内核改动后必做)

```sh
# glibc 兼容容器内构建(默认 features = api+ast,不可 --no-default-features)
docker run --rm --platform linux/amd64 -v "$SRC":/src -w /src \
  -e CARGO_TARGET_DIR=/tmp/target rust:1-slim-bookworm \
  sh -c "cargo build --release -p octos-cli && strip /tmp/target/release/octos \
         && cp /tmp/target/release/octos /src/arc/bin/octos"
chmod 755 arc/bin/octos && arc/bin/octos --version   # 冒烟
sh arc/pack.sh                                        # zip 含 bin/ 与 plan_envelope.py
shasum -a 256 ../octos-arc-bundle.zip                 # 记录 sha
```

`octos-sandbox` 不打包(ARC 用 `OCTOS_DANGER_FULL_ACCESS=1`);保留 v2.0.2
`OCTOS_RELEASE_URL` 兜底,门控会自动回退旧提示词。

### 验收协议(ticket-booking,§11)

1. 冒烟:`python3 arc/run-task-local.py arc/tasks/smoke--counter --name smoke1`。
2. 基线 ×3:`OCTOS_ASSUME_ARC_PLAN=0`,记录通过率/LLM 轮数/token。
3. 新路径 ×3(全开),判据 3/3 全绿且通过率 ≥ 基线。
4. 降级才跑开关矩阵(SCAFFOLD/GRADE/动作摘要 各 1 次)。
5. context-logs 断言(`.octos/context-logs/*/llm.jsonl`):USER 行无 `[arc-plan]`;
   system 行含 CONTRACT+REPO MAP+HOST REPORT;改码轮 `<arc-workspace>` 出现且
   system 行逐字节不变;final 跳过或瘦身;节点验证 tool rounds=0。
6. 宿主信号对账:HOST REPORT 判定 vs `grade-local.py` 实际评分,0 误报 0 误拦。
7. 端口专项:grade-local 走 43300,curl 验证 3301 同 handler 在听。
