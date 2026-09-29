# 提交范围配置（OCTOS_ARC_FAMILIES / COMPETITION_FAMILY_SCOPE）

一句话：**想只提交某个家族（如 REQ-1），改 `arc/main.py` 顶部的一个常量，重打包即可**；本地仿真用环境变量覆盖，不用改代码。

## 正式提交配置（平台通道）

平台运行时**无法传环境变量**，范围必须烤进提交包：

1. 打开 `arc/main.py`，文件顶部有醒目的横幅注释块：

```python
COMPETITION_FAMILY_SCOPE = "REQ-1"
```

2. 改成你要的范围，然后重打包：

```sh
sh arc/pack.sh
# 自验:确认包里的 main.py 带着这个值
unzip -p octos-arc-bundle.zip main.py | grep -m1 COMPETITION_FAMILY_SCOPE
```

3. 提交 zip。

**⚠️ 最终冲分提交前务必改回 `""`（全量）**——范围外的家族不生成、不计分，忘改等于主动放弃那些家族的分数。提交前 checklist 见文末。

## 范围取值（任意层级前缀，逗号分隔）

| 取值 | 含义 |
|---|---|
| `"REQ-1"` | 只实现 REQ-1 整个家族 |
| `"REQ-2-3"` | 只实现子家族 REQ-2-3（含它的整个工单批） |
| `"REQ-1,REQ-3"` | 家族组合 |
| `""` | 全量（所有家族） |

## 范围生效时系统行为（自动联动，无需其他配置）

- 范围外家族**不生成工单**（宿主调度层硬过滤，17→1 级别收敛）；
- 进度账本只列范围内家族（无"永远 pending"噪音）；
- Tier-2 判定面收窄到范围内节点的 specs（全量套件对未实现家族必然全红，会带偏 final 修复轮）；
- 系统行追加 `RUN SCOPE` 段，模型明确知道其他表面缺失是预期的；
- 参考图通道退役（09-29）：yaml 载入即剥图记号、内核自动附图删除——视觉支持保留在内核（显式媒体通道 + VISUAL_MODEL 路由，调用方决定）；
- **种子仍全量播种**（db.json 含所有家族种子行，schema 暴露全部实体）——这是评测冷启动需要，不是范围违规。

## 本地仿真覆盖（env 通道）

```bash
# 构造 env 副本(不动模拟器原始 .env)
cp /Volumes/PSSD/CodeProjects/hackathon-local-simulation/.env /tmp/sim-env.req1
echo 'OCTOS_ARC_FAMILIES=all' >> /tmp/sim-env.req1   # "all"/"*" = 显式全量
# 或 echo 'OCTOS_ARC_FAMILIES=REQ-1,REQ-3' >> ...     # 指定范围

# 第 4 参传 env 文件
sh scripts/local-sim/run-case.sh <run-name> <requirements-dir> <tests-dir> /tmp/sim-env.req1
```

优先级：**env 显式设置 > `COMPETITION_FAMILY_SCOPE` 烤入默认**。env 未设置（如平台）→ 用烤入值；env=`all`/`*` → 强制全量（覆盖烤入值做本地全量仿真）。

## 提交前 checklist

- [ ] `COMPETITION_FAMILY_SCOPE` 的值与本次提交意图一致（快迭代=具体家族；**冲分=`""`**）
- [ ] `sh arc/pack.sh` 后 `unzip -p` 自验常量值
- [ ] 同范围本地仿真预演通过（观察日志 `[scope]` 行与 `[system-row] injected` 行）
- [ ] bin/octos 为当前内核源码重编产物（改过内核时）
