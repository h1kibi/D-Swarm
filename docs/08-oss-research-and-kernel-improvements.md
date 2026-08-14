# 开源调研与内核改进方案 v3（吸收两轮评审后定稿）

> 状态：**v3（2026-08-14）**。v1 经首轮评审（[docs/09](09-kernel-improvement-review-feedback.md) §1-7）
> 修订为 v2；v2 经第二轮复评（docs/09 §10）后修订为本版。本版按复评 §10.6 的八项
> 修订动作逐条落地，并保留两轮评审中确认正确的部分。
> **实现级设计见 [docs/10-v4-kernel-improvement-implementation.md](10-v4-kernel-improvement-implementation.md)（v4）。**
>
> **本版关键结论**：方案定位为**分阶段研究路线**，不整包实施。批次结论：
> 第一批 Conditional Go / 第二批 Go after model correction / 第三批 Redesign before Go /
> 第四批 Conditional Go / 第五批 Offline Go + Online No-Go / 第六批 No-Go。
>
> 调研对象（不变）：
> 1. [FishCodeTech/muteki](https://github.com/FishCodeTech/muteki)（上游）
> 2. [Armur-Ai/Pentest-Swarm-AI](https://github.com/Armur-Ai/Pentest-Swarm-AI) 及[实施计划](https://github.com/Armur-Ai/Pentest-Swarm-AI/blob/main/IMPLEMENTATION_PLAN.md)
> 3. [深入拆解Pentest-Swarm-AI：群体智能自动化渗透测试架构解析](https://cloud.tencent.com/developer/article/2709729)

---

## 0. 结论与路线图

1. 上游 muteki 是 D-Swarm 祖先；其并行 race 与多引擎驱动**不采纳**（成本均衡 vs 首血速度，见 §6）。
2. 信息素数学与 Board 展示层已存在，但未进入 Reason 主规划输入；接入还缺 route lineage、
   event time、durable consumer 与复杂度控制（复评确认）。
3. **六批路线（v3 定稿）**：

| 批次 | 结论 | 实施前必须补齐 |
|---|---|---|
| 第一批：基线正确性 | **Conditional Go** | 先移除 priority 的 Python `int()` 截断；并发按 ordinary/review 双 lane；append-only 定稿为严格 event-row immutable |
| 第二批：方向可观测性 | **Go after model correction** | diagnostics 逐 Intent 建模（不在 ReasonResult 上放单值字段） |
| 第三批：token 预算接线 | **Redesign before Go** | 唯一账本、稳定 usage 幂等键、instance/profile/provider 三层维度 |
| 第四批：route 数据完整性 | **Conditional Go** | lineage、event time、route-less 分类、durable replay、独立 telemetry 载体 |
| 第五批：energy | **Offline Go / Online No-Go** | 去相关、稳定排序、exact-equal tie-break、统计显著性与 feature-off 等价 |
| 第六批：单 Advisor | **No-Go** | 先证明 suggestion 的唤醒/消费路径与相对 baseline 的延迟收益 |

4. 总红线（两轮评审一致保留）：**不修改或弱化 provenance gate；派生 attention/energy/Advisor
   结果永远不是 flag provenance；capability eval 全程离线、零假 flag；新机制有 feature flag、
   关闭后恢复 baseline，额外 CPU/IO/内存/prompt 成本可测量。**

---

## 0.1 思想溯源映射（从三个资源到本方案的显式对应）

本节把「从开源项目/文章吸取了什么 → 落在方案哪里」画成显式映射，供评估者核对
思想吸收的完整性。**注意两点诚实标注**：(a) 六批里可实施的前三批主要来自内部工程债
（两轮评审揪出），而非外部思想；(b) 外部思想主要落在第四~六批（Conditional/Offline/No-Go）。

| 来源 | 吸取的思想 | v3 落点 | 状态 |
|---|---|---|---|
| PSA + 腾讯文 | 信息素衰减：注意力按类型半衰期衰减、自动聚焦新鲜高价值发现 | §5.5 route energy（`[0,1]` clamp、actor/source 去相关、virtual time、exact-equal tie-break） | Offline Go / Online No-Go |
| PSA + 腾讯文 | 触发器谓词：环境变化直接唤醒行动，不等中央规划周期 | §5.6 Advisor（AdvisorySuggestion 模型，不直接派发） | No-Go（缺消费协议与延迟证据） |
| PSA | 写入门槛与记忆投毒防御：clamp、限速、burst 指纹、类型越权 | §5.7 注意力卫生 + §5.4 write-rate telemetry（独立 metrics 载体；硬限速/actor cap 先测量后决定） | 部分采纳（先 telemetry） |
| PSA | per-agent token 预算：软警告 → 硬上限 → 暂停派发 | §5.3 token accounting 三层 identity 重设计 | Redesign before Go |
| PSA | Verified-PoC 门：finding 必须带 Reproduction 重放验证 | **§5.8-1（本轮补回挂起项）** | 待办（v1→v3 重构时漏挂，见 §5.8） |
| PSA | scope 双层强制 → 事后审计形态 | **§5.8-2（本轮补回挂起项）** | 待办（同上） |
| PSA | cleanup registry：清理先注册、逆序执行 | **§5.8-3（本轮补回挂起项）** | 待办（同上） |
| upstream muteki | 并行 race/recon（多引擎单发冲刺） | §6 否决（成本均衡 vs 首血速度） | 否决（维护者决策） |
| upstream muteki | 多引擎异构盲区互补 | §6 否决（per-profile provider/model/effort 已是配置问题） | 否决 |
| upstream muteki | custom-endpoint 健康检查跑真实 CLI 回合（0.2.4） | **§5.8-4（本轮补回挂起项）** | 待办合并补丁 |
| 腾讯文 | 去中心化的调试成本 / alpha 成熟度争议 | §6「否决全面去中心化」的论据 | 佐证 |
| upstream muteki | Reviewer 机制 | 非提案——D-Swarm 已有 `review_flow.py` | 已有 |

另注：D-Swarm 在调研前已**自发拥有** PSA 的部分思想（`board.py` 的
`Finding.pheromone()`/`PheromoneSettings`/`FindingPredicate`/`subscribe`），这是"接电"
而非"移植"的判断基础，两轮评审均确认该判断成立。

---

## 1. D-Swarm 内核背景（评估者 grounding）

| 层 | 模块 | 职责 |
|---|---|---|
| 事件脊 | `core/events.py` `event_bus.py` `session_store.py` | 有序事件流 + JSONL 持久化 |
| 证据图 | `swarm/shared_graph.py` | 事件溯源 SQLite；facts/intents 为物化视图 |
| 通知总线 | `swarm/insight_bus.py` | 跨 worker 已验证事实 fan-out |
| 执行器 | `solver/cli_driver.py` `cli_solver.py` | shell 出 `pi` CLI（单发 worker） |
| 规划/评审 | `solver/reason.py` `swarm/reason_scheduler.py` `review_flow.py` | 中央 Reason + Reviewer 仲裁 |

**append-only 语义（v3 定稿，不再开放）**：按 AGENTS.md 现有不变式，选择**严格 event-row
immutability**——`events` 的 `ts/actor/kind/payload/artifact_id/verified/confidence/dedupe_key`
均不可原位修改；candidate→verified 提升写新事件或写可由 event log 重建的 projection/state
表；summary 写 side table 或追加新事件；`intents` 等物化投影可更新但必须声明为非 canonical。
守护测试静态禁止**所有** `UPDATE events`，并验证 replay 可重建相同投影状态。

当前派发主循环：`initial recon（单 worker，方向=category）→ while 未完成：Reason 读图提
intent → 截断到 max_intents_per_reason → 对 fresh decisions 直接 `asyncio.gather` 并发 →
投影 → 循环`。已核实：该 gather **没有 max_workers 约束**（复评 §10.3.2 确认缺口成立），且
主派发顺序是模型返回顺序。

---

## 2. 参考材料一：FishCodeTech/muteki（上游）

（内容与 v1/v2 相同，摘要）异构多模型 CTF swarm（claude/codex/cursor），NYU 200/200
（~$214、~370M tokens、中位 2-4 分钟；引擎分布 cursor 80 / claude 75 / codex 45），四阶段
架构（Prepare / Recon Race / 协调主循环 2s 一圈 / Wind-down）+ Reviewer。与 D-Swarm 的关系：
同源 fork 各自演进——D-Swarm 在 gate 加固（`_TEST_MARKER_RE` 等）、btw、模块化领先；上游在
race 模式、多引擎驱动、custom-endpoint 真实健康检查（0.2.4）、容器一致性强制领先。

## 3. 参考材料二：Armur-Ai/Pentest-Swarm-AI

（内容与 v1/v2 相同，摘要）Go + Postgres/pgvector 黑板，stigmergy 三原则（信息素衰减/
涌现/触发器谓词去中心化），工程护栏（scope 双层强制、cleanup registry、prompt caching、
per-agent 预算、Verified-PoC 门），记忆投毒四层防御（Ed25519 签名、MINJA clamp、MemoryGraft
看门狗、token-bucket 限速）。

## 4. 参考材料三：腾讯云文章（MS08067）

（内容与 v1/v2 相同，摘要）PSA 第三方拆解；争议清单（去中心化难调试、alpha 成熟度、Go
生态小）是 §6 否决项的重要旁证。

---

## 5. 六批方案（v3，按复评 §10.3 修正后的最终形态）

### 5.1 第一批：基线正确性（Conditional Go）

**A. priority：先删 Python 截断，不先迁 schema**（复评 §10.3.1，已核实）
- 事实：`propose_intent`（`int(raw_priority or 0)`）与 `dispatchable_intents`
  （`item["priority"] = int(...)`）两处截断；另有摘要块 `int(priority or 0)` 多处。
  SQLite INTEGER affinity 实际可保存 REAL（0.5/0.9 不丢）。
- 方案：① 删除全部 `int()` 截断；② Python/API 层统一 `float`；③ 对新旧 SQLite DB 做
  持久化/排序/replay 测试；④ 仅当跨后端 DDL 契约确需时才迁列类型。**不默认 ×100**（planner
  priority 不保证两位小数，固定缩放引入精度上限与双尺度转换）。
- 另须定义 operator priority（100/50/0/-10 映射）与 planner priority（0..1）的**统一比较
  尺度契约**——两套尺度当前混用，不能只修存储。

**B. 并发：ordinary/review 双 lane**（复评 §10.3.2，已核实 `test_swarm.py:925`
`test_review_worker_uses_reserved_capacity_when_ordinary_slots_full` 存在）
- 约束（v3 定稿，替换 v2 的 `max_active_workers <= max_workers`）：
  ```text
  ordinary_active_workers <= max_workers
  review_active_workers    <= review_policy.max_concurrent
  total_active_workers     <= max_workers + review_policy.max_concurrent
  ```
- 实现：两把独立 semaphore；明确 recon/explore/recovery/operator-fallback 属 ordinary lane、
  review worker 属 review lane；取消/异常/重试/恢复全部经同一容量控制并可靠释放 permit。

**C. append-only：严格 event-row immutable**（复评 §10.3.3，§1 已定稿；删除 v2 的
`(i)/(ii)` 开放选择）。守护测试：静态禁止所有 `UPDATE events` + 行为测试（verification/
summary/review 后原行字段或稳定哈希不变；replay 重建相同投影）。provenance gate 及其测试
保持原样。

### 5.2 第二批：方向可观测性（Go after model correction）

- **diagnostics 逐 Intent 建模**（复评 §10.3.4）：在 `Intent` 上增加：
  ```text
  raw_direction
  canonical_direction
  direction_resolution ∈ {empty, explicit_auto, recognized_alias, invalid,
                          mechanical_fallback, category_fallback}
  ```
  原始值进事件/UI 前做长度限制；机械 fallback 只处理空/非法 direction，**不得覆盖**合法
  canonicalize 的模型输出；方向 registry 为单一权威来源，用 typed fields（profile/镜像/
  prompt/关键词各自类型化），不做无类型巨型 dict。
- misc 机械 seed 第一版；flash triage 保持为有成本可选项。
- 补 decision → profile → worker factory → runtime 全链路测试（decision 层测试已存在）。

### 5.3 第三批：token 预算接线（Redesign before Go）

复评 §10.3.5 推翻 v2 的"每次 spawn 唯一 solver_id 即天然去重"设计，理由成立：retry/recovery
新 ID 会重置 per-agent cap，且无法限制同一 profile/provider/account 累计消耗。v3 契约：

- **唯一账本**：扩展 `CostController`/`COST_UPDATE` 为唯一事实源；`MemoryBoard`/UI 只做投影。
- **usage 幂等键**：`usage::<worker_instance_id>::<provider_call_id>`（或 ledger event id），
  防 outcome 重放 / backend restart / recovery / provider 内部重试重复计费。
- **三层身份**：`worker_instance_id`（单次生命周期归因）、`profile_id/direction`（调度预算）、
  `provider/account_id`（配额、错误聚合、暂停派发）。
- **unknown usage 语义**：provider 未返回 usage → 记 `unknown/estimated`，不得静默记为 0。
- 如实改写能力现状：`MemoryBoard.charge_agent` 仅累计+warned，`_budget_exhausted` 只查
  challenge global；"per-agent 软告警→硬上限→暂停派发"状态机**尚不存在**，属本批待建。

### 5.4 第四批：route 数据完整性与 telemetry（Conditional Go）

- **route lineage**：优先 `intent_id → intents.route_hash` 解析，不单信 payload 中可缺失的
  `route_hash`；区分 explicit route / intent-inherited / route-less（结构化原因枚举），
  route-less 进独立 `unattributed` bucket，不自动归入热门 route。
- **时间分离**：同时保存 event time 与 projection write time；benchmark replay 用
  **virtual time**，防重放当天时钟改变 pheromone/energy。
- **telemetry 载体**（复评 §10.4-5）：高频 write-rate/重复率/膨胀等原始数据写**独立
  append-only metrics artifact/table**；UI 只收低频聚合 delta；**不写回 evidence graph、
  不扩大 Reason prompt**。
- durable consumer：crash/restart 从 checkpoint 恢复，重放不重复产生派生记录。

### 5.5 第五批：energy（Offline Go / Online No-Go）

- 公式前置条件（复评 §10.3.7）：正贡献每项先 clamp 到 `[0,1]`；计算前做 identity dedupe +
  actor/source 相关性分组（组内取 max 或折扣，再跨独立来源组合）；定义多条 dead-end 的
  合并、正负贡献尺度与最终 score 边界；同 route 同 priority 的**稳定排序键**；feature off 时
  与 baseline decision-for-decision 一致。
- **在线语义 v3 定稿**：第一版只允许 **priority 完全相等**时 energy tie-break。排序键：
  ```text
  lifecycle_lane -> planner_priority -> energy_if_priority_equal -> original_index
  ```
  v2 的"接近时 tie-break"与"永不覆盖 planner priority"自相矛盾（0.90 vs 0.85 交换顺序即已
  覆盖），删除；near-equal 若未来要做须显式定义 epsilon/bucket 并承认是有界重排。
- 复杂度：不得按固定短周期全量扫历史；缓存失效策略有基准数据。
- 离线实验口径：相同 benchmark/model/provider/token-worker 预算/离线网络；报告多 seed、
  flag latency、worker starts、tokens、provider errors、route churn 与置信区间；样本不足
  不得据此批准在线调度。

### 5.6 第六批：单 Advisor（No-Go，设计阻塞）

复评 §10.3.8 的两个矛盾成立：① AdvisorySuggestion 不直接执行，没有"事件即执行"收益；
② ReasonSwarm 等本轮 `gather` 全部完成才进下一 Reason cycle，慢 worker 阻塞快路径。
`subscribe_events()` 只解决事件可见性，未解决谁被唤醒、何时消费、如何中断等待、如何受
pause/stop/budget 约束。且 multi-flag 的 Reason prompt 已能看到已捕获 flag，flag-scout
是否比下一轮 Reason 更快**尚无证据**。

- 当前：**不实施**。恢复研究的前置：完整消费协议 + 延迟对照 trace
  `flag_found → suggestion → Reason consume → focused dispatch`，测量
  `time(flag_found → next focused dispatch)` 与 baseline，记录接受/拒绝原因，验证
  restart/replay 幂等与 pause/stop 后零新 spawn；幂等键用 source event seq/hash，不拼接
  原始 flag；budget/cooldown/durable cursor 不得只存进程内变量。

### 5.7 注意力卫生（继承 v2 拆分，无变化）

token accounting 并入第三批（redesign 版）；write-rate telemetry 并入第四批（独立 metrics
载体）；硬限速与 burst 指纹/actor cap 仍为"先测量后决定"，不做生产常量。

### 5.8 OSS 遗产待办（v1→v3 批次重构时漏挂，本轮显式补回；不进六批，独立小项）

以下四项来自调研、在 v1 方案中提出、但在 v2/v3 的六批重构中失去了落点。它们不是六批的
前置条件，也不阻塞六批实施，作为**显式挂起项**记录，防止思想吸收链条断裂：

1. **Pentest 模式 Verified-PoC 门**（PSA §4.3 → flag gate 哲学迁移）：高严重度 finding 只有
   通过 verifier intent 重跑 `{Command, HTTPRequest, ExpectedIndicator}` 且指标出现在真实
   执行输出中才能 verified=true。与既有 `POC_SAVE=` 标记、`claim_poc/conclude_poc`、
   witness gate 组合实现。
2. **scope 事后审计**（PSA 工具层+执行层双层强制 → shelled-CLI 架构下的等价物）：扫描
   provenance corpus，检测 out-of-scope 主机/资产引用，命中则事实标记 `scope_violation`、
   从报告排除、HITL 提示操作员。前置：第三批的 provenance corpus 可复用性与第四批的
   metrics 载体。
3. **cleanup registry**（PSA §1.3.1 → 目标侧产物治理）：worker 在目标侧创建的产物
   （listener 端口、上传文件、残留会话）登记进 shared_graph（`resource_locks` 已有雏形），
   wind-down 逆序释放 + 报告清单。
4. **上游合并补丁**（muteki 0.2.4/0.2.5）：custom-endpoint 健康检查跑真实 CLI 回合、开跑前
   暴露 LiteLLM/DeepSeek schema 错误；探测 worker 镜像真实 UID/GID 再 chown；容器内强制
   container backend、拒绝静默回退 host。均为小合并，各自独立验证。

---

## 6. 已否决方案（决策上下文，累积三版）

| 否决项 | 理由 |
|---|---|
| 并行 race/recon（上游） | 成本均衡 vs 首血速度 |
| 异构模型补齐引擎异构 | 配置问题，非内核优化 |
| 多见证共识 | 回声≠验证；主动验证版=已有 review/verifier |
| 全面去中心化 | 调试难、alpha；上游实为协调器+Reviewer |
| 重上闭源引擎驱动 | 与 pi 路线冲突 |
| Postgres+pgvector 替换 SQLite | 单文件溯源是卖点 |
| 捆绑工具适配器 | less is more |
| **energy "near-equal" 在线重排**（v2 提出，本轮否决） | 与"不覆盖 planner priority"自相矛盾；v3 只允许 exact-equal |
| **Advisor 借 `subscribe_events` 直接上实验**（v2 提出，本轮否决） | 缺唤醒/消费协议与延迟证据，No-Go |
| **append-only 放宽语义 `(ii)`**（v2 开放选项，本轮否决） | 与 AGENTS.md 不变式冲突；严格 immutable 定稿 |
| **per-agent 预算以 solver_id 为唯一维度**（v2 提出，本轮否决） | retry/recovery 新 ID 重置预算；需三层 identity |

---

## 7. 验收标准（替换 v2 §8，采用复评 §10.5 清单）

**基线正确性**
- [ ] priority 在 parser/event payload/intent projection/dispatch API/UI/replay 全链路保持浮点精度
- [ ] `0.5/0.9` 持久化、重启、重放、排序后不丢精度
- [ ] operator 与 planner priority 的尺度、覆盖规则、稳定排序有明确契约
- [ ] `ordinary_active_workers <= max_workers`；`review_active_workers <= review_policy.max_concurrent`；`total <= 两者之和`
- [ ] 取消/异常/retry/recovery 后所有 semaphore permit 释放且不超发
- [ ] 源码与 SQL guard 禁止所有 `UPDATE events`
- [ ] verification/summary/review 后原 event 行字段/稳定哈希不变
- [ ] 仅从 event log replay 可重建相同 verified/summary projection

**方向路由**
- [ ] 同一 ReasonResult 中多 intent 各自保留正确 raw/canonical/diagnostic
- [ ] diagnostic 用结构化枚举；非法 raw 值进事件前限长
- [ ] 合法 direction 不被低置信机械规则覆盖
- [ ] direction 经 decision → profile → worker factory → runtime 全链路一致

**token 与预算**
- [ ] recon/ordinary/review/provider retry/worker recovery 的 usage 进同一 ledger
- [ ] 相同 usage/outcome/event 重放不重复计费
- [ ] retry/recovery 不通过新 solver_id 重置 profile/provider/account 预算
- [ ] CostController、Board projection、API/UI 的 token/cost 总量一致
- [ ] provider 未返回 usage → 显示 `unknown/estimated`，不静默记为 0
- [ ] 软告警/硬上限/暂停派发/恢复条件有确定性状态机测试

**route 与 telemetry**
- [ ] explicit / intent-inherited / unattributed route 可区分；route-less 原因结构化枚举
- [ ] event time 与 projection write time 分离；replay 用 virtual time 结果稳定
- [ ] telemetry 原始数据不进 evidence graph、不扩大 Reason prompt
- [ ] durable consumer 崩溃重启后从 checkpoint 恢复，重放不重复派生

**energy 离线实验**
- [ ] 正贡献项组合前 clamp `[0,1]`；weight/τ/dead-end 合并/score 边界有定义
- [ ] 同 actor/source 回声不把 route energy 刷满
- [ ] 冷启动及 feature off 与 planner baseline decision-for-decision 一致
- [ ] 第一版仅 exact-equal priority tie-break；相同输入多次排序完全稳定
- [ ] review/verifier 不受 route heat 排序影响（独立容量与生命周期 lane）
- [ ] 无固定短周期全量扫描；复杂度与缓存失效有基准
- [ ] ablation 同 benchmark/模型/provider/预算/离线网络；报告多 seed 与置信区间；样本不足不批准在线

**Advisor 实验（第六批恢复研究时）**
- [ ] `flag_found → suggestion → Reason consume → dispatch` 完整 trace
- [ ] `flag_found → next focused dispatch` 延迟对照 baseline
- [ ] 接受/拒绝原因可追踪；被拒 suggestion 不生成正式 intent
- [ ] source event 重放、进程重启、cursor 恢复不重复建议/派发
- [ ] pause/stop/budget exhausted 后绝不触发新 worker

**总红线与评估口径**
- [ ] 不修改或弱化 provenance gate；派生 attention/energy/Advisor 结果永远不是 flag provenance
- [ ] capability eval 全程离线、零假 flag
- [ ] "solve-rate 不退化"用预定义非劣界、相同预算、多 seed/置信区间判断（不要求每次 run 绝对不退化）
- [ ] 新机制有 feature flag，关闭后恢复 baseline，可测量额外 CPU/IO/内存/prompt 成本

---

## 8. 验证状态与诚实记录

**测试基线冲突已在第三轮复评（docs/09 §11）中定论，本工作区已按 §11 修复并验证。**
v3 初版的归因（"`test_planner_forwards_base_url` 任何环境下确定性失败，原因是 claude
profile 被 pi-only 拒绝"）**经 §11 复核后证明是错的**，更正后的因果链（已在本工作区
逐条复现确认）：

- 4 个 LLM 测试（connectivity ×3 + planner base_url）的真实失败原因是 **ambient
  credential 依赖**：`resolve_reason_llm_endpoint` 的 `has_api_key` 预检在构造（被 mock 的）
  LLMClient 之前执行；无 key → 不构造 client → `__aenter__` 不执行 → `_StopHere` 不抛出。
  注入占位 key 后**旧 claude fixture 也能通过**——claude fixture 确属 pi-only 迁移残留应当
  迁移，但不是失败的直接原因。
- tsc 测试的真实原因是 `find_tsc()` 的 PATH 优先顺序（TS_BIN → PATH → repo-local），会被
  其他项目/全局的 TS6 遮蔽；与 tsconfig 的 `baseUrl` 弃用叠加导致机器相关。
- 评审方记录的 `exit code 0` 是真实结果；其遗漏是没有记录兼容变量 `DEEPSEEK_API_KEY`
  在评审环境中已设置。

**已应用的修复（限定 4 个文件，对应 §11 建议）并验收：**

| 文件 | 修复 | 验收结果 |
|---|---|---|
| `tests/test_connectivity_probes.py` | 3 个 LLM 测试内注入占位 key + 删除 ambient `DEEPSEEK_API_KEY` | 两 key 均为空时 4 个 LLM 测试全过；有/无真实 key 结果一致 |
| `tests/test_llm_base_url_wiring.py` | planner 测试同时完成两件事：注入占位 key + 旧 claude fixture 迁移为合法 pi profile | 同上 |
| `docker/worker-pi/scripts/check_pi_extensions.py` | `find_tsc()` 顺序改为 TS_BIN → repo-local → PATH | tsc 测试稳定使用 repo-local 5.9.3；`TS_BIN` 显式覆盖保留（TS6 CI） |
| `docker/worker-pi/pi-config/extensions/tsconfig.json` | 删除 `baseUrl`，`paths` 改为显式相对路径（仅删 baseUrl 会 TS5090，故不采用 `ignoreDeprecations` 掩盖） | repo-local 5.9.3 通过 |

完整 `uv run pytest -q`（两 key 均为空的环境）结果见工作区实测：**0 失败**（本轮执行）。

**遗留清理项（与前两轮审计相关，不影响本方案审批）：** `public_eval/RESULTS.md` 仍为上游
Muteki 数据需标注归属；`stale_reason_limit`/`stall_seconds` 死参数；`DSWARM_WORKER_TASK_KIND`
写而不读；3 个文件的 61 处 U+9225 mojibake；README 的 upstream remote 指引与
`server.py` 的 `docs/_local` 悬空引用。
