# octos-arc

黑客松自动编程赛题的参赛工程:Python 宿主(`arc/main.py`,负责需求装配、批循环、
回归网、演进轮编排)+ 随包分发的 Rust 编码内核(`crates/`,编译产物
`arc/bin/octos`,linux/amd64)。本文只记录**比赛期间对内核做的修改与调优**;
宿主侧改动不在本文范围。

内核派生自上游全量编码智能体内核,vendored 进本仓独立演进;版本串
`2.0.5-rc.1`,内核单测全绿(820+)。以下改动按主题归类,括号内为对应提交。

## 1. 内核瘦身与工具面收敛

比赛任务只用到全部 35 个工具中的少数,大内核=大提示词=大上下文税。

- **vendored 瘦身**:slim6→15 系列死代码清扫,累计净删约 2.8 万行
  (sandbox/macos 测试去重 1414→934、transport 假 auth stub、休眠机制、
  config watcher、僵尸链终扫等)。
- **planctl 整体退役**(095f5d2e,-4722 行):规划信号不再走内核通道,
  契约/需求/进度全部印进自含工单。
- **testgen 管线与 test_writer 角色退役**(ec39c3c9):测试用例由模型
  依需求自编,生成管线成死代码。
- **工具白名单 `OCTOS_TOOL_ALLOWLIST`**(43e9180e):平台 trace 实测
  1333 次工具调用仅用到 9/35 个,ARC 场景下 LLM 的工具 spec 直接收敛到
  7 个。

## 2. 循环治理(loop_detect / loop_runner)

弱模型长会话尾部会退化成不动点循环(同消息同调用逐字重复)、调试独白、
shell-spiral(同形大输出反复喂重试判据)。治理目标=可检、可救、可续跑,
且**治理杀必须与模型正常收尾可区分**(否则宿主把被杀批次记成 done)。

- **检测键=工具名+args JSON**(助手文本不在检测面);阶梯:同签名
  3..=5 次温和 nudge → 6 软恢复 → 7 终局硬杀。
- **doom 两阶段**(920449ce):第 3 次软警告跳过、第 4 次终局;幂等验证
  命令豁免(build/git status/curl GET 等 streak 3..=5 只 nudge,cap 5)——
  防"反复跑验证"被当循环误杀。
- **ERROR_STREAK**(920449ce/06c5da35):同错首行 8 连击发一次性 hint
  (提示"换参数撞同败");签名行跳过空行与 Exit code 回执头,shell 家族
  全家族恢复武装。
- **shell-spiral**:[SHELL SPIRAL DETECTED] 标记行随终局消息下发;
  "(no output)" 哨兵在 Exit code 回执头置后复活(06c5da35);失败占多数
  退化门——窗口内失败不占多数不强杀,1 次良性 grep 失败不再终局 turn。
- **green-stop**(c08ff27d):连续双绿且无编辑即停,命令归一化防同义
  命令绕过。
- **page-snapshot 折叠**(920449ce):浏览器快照只保留最新 2 条(曾实测
  297 条快照永驻上下文)。
- **checkpoint 反思信封**(ac488496):软反思轮追加"turn 未完成,以计划
  收尾=失败"——对症"把反思计划当交付提前收工"。
- **MaxTokens 独白软恢复**(7843e09c):有内容无工具调用=调试独白,
  nudge 限 2 次,耗尽回落 output_truncated 交宿主重入兜底。

## 3. 工具执行层修复

比照 pi 的工具设计逐项修复(d9543c0b,均有回归测试):

- **shell 超时不丢输出**:读管道改有界尾捕获任务(8MB cap,保尾丢头+
  UTF-8 接缝对齐),超时后已捕获输出随 "Command timed out" 一并返回。
- **进程组修复**:前台/后台 spawn 加 `process_group(0)`——此前子进程留在
  内核自身进程组,超时路径的组杀信号一直打在空组上静默失效。
- **Exit code 回执前置到结果头部**:下游尾部截断不再丢失退出码。
- **sandbox 解释器 sh→bash**(NoSandbox 模式):runner 镜像 `/bin/sh`
  是 dash,模型的 bash 语法必然报错。
- **edit_file BOM 剥离+CRLF 归一化**:显示层吞 `\r` 且 BOM 不可见,
  模型永远写不出含 `\r`/`\uFEFF` 的 old_string,CRLF/BOM 文件此前必然
  编辑失败。
- **grep/list_dir 加固**:literal 参数(元字符字面匹配)、limit 钳制
  (limit=0 假 "No matches")、截断误标修复(恰好 N 不再标 limited)、
  list_dir 默认 limit 500+截断注记、循环层盲切后补 truncation_recovery。
- **pkill 自匹配死法根治**(471a65b7):模型命令写入 `.octos/cmd/*.sh`
  以 `sh <路径>` 执行(script_exec_command),命令文本离开执行 shell 的
  argv,`pkill -f "node src/index.js"` 类自匹配自杀整灭;信号死亡改报
  `signal N (NAME)`+自匹配提示,替换零信息的 `Exit code: -1`。

## 4. 治理可观测性

- **Success-kind spiral 分支补治理标记**(edb56a2):从噪声里提取的
  shell 输出曾被裸当终局回复(ok=True)——宿主词表对不上前缀→误记
  model end_turn→假 PASSED。三分支收敛到共享入口,治理杀必有标记。
- **上下文全程记录**(0ab6a778):LLM 请求/响应+工具调用 JSONL 落盘
  `.octos/context-logs/`,法医取证不依赖内存。
- **终局归因**(7843e09c 配套):内核治理中止的合成收尾行不进
  message 流,宿主凭词表前缀甄别"内核代收尾"与"模型自选收束"。

## 5. Token 与上下文预算

- **工具输出可见上限 4K→32K**(7843e09c):4K 截断会截掉 shell 收据尾注
  (退出码在尾部),是 spiral 误火的元凶层;同时 Exit code 前置双保险。
- **输出上限 16K→128K 通道**(7843e09c):16K 默认值下长调试独白撞顶即
  output_truncated 终局;宿主可 env 覆盖,"0"=回内核默认。
- **压缩阈值窗口防御**(4383a6f8):env override clamp 到窗口×7/10,
  只能收紧不能放水;压力帽跟随阈值自动继承。
- **token 计量与折叠**(f1a477a9/d84f7ac0/94ffe726):miss 计量、read
  历史折叠、重复读 nudge、artifact 阈值 3KB、edit hunk 预览、pinned
  tail 回退、repomap 新鲜度;compaction 后清 file-state 缓存,token
  校准比率透传 context-manager。
- **Cline 对标六项**(b1624c0b):token 实测校准、并行引导、审批拒绝
  定性、空响应快重试、429 上限放宽、溢出收缩校验。
- **看门狗放宽**(99ec7b16):idle 600s / cap 1800s(原值会把长流式
  turn 掐死在半路)。

## 6. 思考与视觉通道

- **思考参数直暴露**(af08c656):`thinking`/`reasoning_effort` 成为
  请求级参数,分级思考整层退役;deepseek lane 主动发 `thinking: disabled`。
- **视觉通道重构**(d35ec799/d105a6bf):内核图片直连+`ChatConfig.model`
  请求级指定(截图核验轮路由到视觉模型)。
- **UTF-8 与读护栏**(7a369e40):UTF-8 截断接缝、追踪区关读、读护栏、
  窗口元数据。

## 复现与验证

- 内核测试:`cargo test`(内核 820+ 绿;历史个别挂为容器环境性,同 HEAD
  复现)。
- 内核 bin 重编(docker 直挂仓库路径,linux/amd64):
  `docker run --rm --platform linux/amd64 -v <repo>:/src -v ~/octos-build/target:/target -w /src rust:1-slim-bookworm cargo build --release --locked --bin octos`
  产物拷至 `arc/bin/octos` 随 bundle 分发。
- 提交打包前必核对 `arc/bin/octos` 与 `crates/` 最新提交时间一致。
