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
python3 arc/local/run-task-local.py arc/tasks/smoke--counter --name try1

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
| `template/` | 提交包携带的初始工作区模板（frontend/+backend/+template.yaml，scaffold 单源；每次 pack.sh 打包时直接现生成） |
| `octos_stdio.py` | 通过 `octos serve --stdio` 驱动内核 |
| `public-tests/<题目>/` | 平台公开的 Playwright 验收测试（会自动喂给模型） |
| `tasks/<题目>/` | 各题需求文件的离线副本 |
| `local/run-task-local.py` / `pack.sh` | 本机做题、打包 |

已知平台细节：容器里 `/workspace/tests` 有验收测试；订票题的测试默认连 3301 端口而平台起在 3000，`main.py` 会要求后端两个端口都监听；容器到 npmjs 很慢，提示词要求零依赖并走 npmmirror。

## planctl 已退役(2026-09-27,与 pi 对齐)

`[arc-plan]` envelope 通道整体移除(内核 planctl 模块 -4722 行,宿主
set_envelope/marker 包装同删):契约、需求地图、进度账本全部印进工单正文,
工单自包含;跨轮记忆由会话历史承接。内核保留 shell
guard(自测命令放行形状/full-spec-run/trace-deny)、OCTOS_TOOL_ALLOWLIST、
折叠/迭代治理。`plan_envelope.py` 瘦身为工具箱(契约文案常量 + specmap +
探针 + stage_specs)。

上下文分两层装配(缓存 0 号位):字节稳定的全局层(root 契约逐字+标题树
+契约+播种契约)经内核 bootstrap 通道落盘 `<data_dir>/profiles/<profile_id>/data/AGENTS.md`
(session 在 profile create 后自动注入)常驻系统行
——每分段(会话重开)付 1 次,轮轮前缀缓存命中;模块层(当前家族完整子树
+`Depends on` 依赖闭包+批节点 scenarios+进度账本)随工单独立到达,工单从
~87KB 瘦到 ~10KB。升级路径 C-lite(分段累积已完成家族 FOLDER 契约)已在
`build_system_row_context` 预留参数位,触发条件=仿真显示模型反复定点重读
已完成家族契约。
**提交范围配置见 [SCOPE.md](SCOPE.md)**——正式提交改 `main.py` 顶部 `COMPETITION_FAMILY_SCOPE` 常量重打包;本地用 env 覆盖。

环境开关:`OCTOS_ARC_FAMILIES`(运行范围,
首跑/快迭代:逗号分隔 id,任意层级前缀——`REQ-1`=整家族/`REQ-2-3`=子
家族/原子 id;范围外家族不生成工单/不计进度/不进 Tier-2 判定面,系统行
追加 RUN SCOPE 段;未设置=全量)、`OCTOS_ARC_HOST_SCAFFOLD`
(宿主落盘基础设施,缺省开)、`OCTOS_ARC_HOST_GRADE`(final 轮 Tier-2 宿主
跑 staged Playwright,缺省开)。宿主优先回退路径(原 `OCTOS_ARC_GOAL_FIRST=0`)
已整体删除:家族工单是唯一调度路径。

## 宿主模板 + schema 信号驱动 codegen(2026-09-17 起;2026-09-18 迁入 scaffold/ 包)

脚手架封装在 `scaffold/` 包(参考 agent-arc-based 的组织:清单单源 + 小公开面),
调用方只 `import scaffold`,不再触碰具体文件路径:

- `scaffold/manifest.py` — **单一事实源**:17 件模板清单(rel → 内容+角色
  host/placeholder/seed/model)、就绪/完整判定键、schema 编译路径、cookie 名、
  npm 源、HOST_STACK_RULES 必须点名的路径清单(RULES_MUST_NAME)。
- `scaffold/backend.py` / `scaffold/frontend.py` — 模板内容常量(纯标准库)。
- `scaffold/codegen.py` — schema.json → schema.js 编译 + Watcher。
- `scaffold/__init__.py` — 公开面:`scaffold()`(幂等落盘)/
  `template_ready()` / `template_complete()` / `prewarm_deps()` /
  `compile_schema` / `Watcher` / `schema_path`。
- `scaffold_test.py` — 单测 + 冒烟 + **防漂移一致性测试**(文字契约点名、
  cookie 单源、清单↔模板咬合)。

请求任何模型之前,宿主完成:完整模板落盘(`scaffold.scaffold`:HTTP/cookie/
存储/校验/auth/CRUD 通用层,backend 零依赖即可启动)→ `npm install`
预装(INSTALL_REGISTRY=npmmirror,`OCTOS_ARC_PREWARM=0` 关)。goal-first(09-27)起 skeleton 轮
恒走:模板只供数据面,App 外壳/路由/家族文件由模型依需求地图自建;
`SCHEMA_DIRECTIVE` 仅在 schema.json 缺席时追加到节点工单(骨架轮提示词
已含同等指引)。schema.json 落盘即信号:`scaffold.Watcher`
轮中 2s 轮询、稳定 3s 即编译为 `src/database/schema.js`(轮末兜底 sync),
engine/auth 在下次 boot 时挂出 `/api/<entity>` CRUD 与 `/api/auth/*`;
tree-sitter repomap 每个 marker turn 全量重扫,宿主直写文件自动进图,
并列入 `scaffolded_by_host`(do NOT rewrite 清单)。开关:
`OCTOS_ARC_HOST_CODEGEN=0` 关 codegen,`OCTOS_ARC_HOST_SCAFFOLD=0` 回退
模型自建骨架(保留 skeleton 重试老路径),`OCTOS_ARC_HOST_GRADE=0` 关
Tier-2(缺省开:final 阶段宿主跑 staged Playwright——容器内直用镜像
全局 @playwright/test(与评测引擎同一份二进制,零安装;09-27 起
local-grader 不再进比赛包),开发机兜底 repo 的 local-grader 安装,
node_modules 软链进临时 run 目录;E2E_BASE_URL 指向
smoke 端口并双听 spec 基端口;全绿跳过 final LLM 轮,红则把失败清单喂给
final 轮;环境跑不了自动降级为模型自查)。教训:JSON reporter 的结果状态
是 `passed`,`expected` 是 outcome 字段,分类要两者都认。

### token 基线与实测口径

`measure_tokens.py`(只读)统计 `.octos/context-logs/*/llm.jsonl`:
`python3 arc/measure_tokens.py arc/arc-output/<run>...`。基线(agnes-3.0-flash,
planctl 栈):ticket r4=184 calls/10.04M in+cache/49.5K out/22.1min(评分 10/10);
ticket r1/r2=138/165 calls,7.90M/8.90M;r3=292/17.68M(失控上界);
smoke r2=80/2.53M(1/1)。input(含 cache)占 99.5%,主杠杆是调用次数。
判据:ticket ×3 全绿且均值 calls ≤165、in+cache ≤8.9M;工具分布中
shell 安装/构建类调用应显著下降。

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

1. 冒烟:`python3 arc/local/run-task-local.py arc/tasks/smoke--counter --name smoke1`。
2. goal-first ×2(缺省全开),记录通过率/LLM 轮数/每工单墙钟。
3. context-logs 断言(`.octos/context-logs/*/llm.jsonl`):USER 行含需求地图
   与契约(工单自包含);无 `[arc-plan]` 残留(通道已删)。
7. 端口专项:grade-local 走 43300,curl 验证 3301 同 handler 在听。
