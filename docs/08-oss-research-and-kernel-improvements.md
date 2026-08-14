# 开源调研与内核改进方案 v2（已按评审意见修订）

> 状态：**v2（2026-08-10）**。v1 经第三方 LLM 评审（见
> [docs/09-kernel-improvement-review-feedback.md](09-kernel-improvement-review-feedback.md)），
> 本版已逐条核验并吸收其事实校正与方案 verdict。v1 的关键不准确表述已在 §7 列出并改正。
> 本版结论较 v1 有实质变化：**方向诊断与基线正确性进入近期实施；route energy 先做离线
> telemetry/ablation；Advisor 触发器缩为单触发器实验；写入硬限速暂缓**。
>
> 调研对象（不变）：
> 1. [FishCodeTech/muteki](https://github.com/FishCodeTech/muteki)（D-Swarm 上游内核）
> 2. [Armur-Ai/Pentest-Swarm-AI](https://github.com/Armur-Ai/Pentest-Swarm-AI) 及[实施计划](https://github.com/Armur-Ai/Pentest-Swarm-AI/blob/main/IMPLEMENTATION_PLAN.md)
> 3. [深入拆解Pentest-Swarm-AI：群体智能自动化渗透测试架构解析](https://cloud.tencent.com/developer/article/2709729)

---

## 0. 结论（v2）

1. **上游 muteki 是 D-Swarm 的祖先**，两线独立演进。上游的并行 race 与多引擎驱动**不采纳**
   （目标函数是成本均衡而非首血速度，见 §6）。
2. **信息素数学与 Board 展示层已存在，但未进入正常 Reason 主规划输入，也不是"只差一根线"**：
   真正接入还缺 route 归属、原始 event 时间、投影覆盖、durable consumer、复杂度控制。
3. **提案分六批**（评审 §4）：先修三个基线正确性问题（priority 精度、max_workers 并发、
   append-only 语义与守护测试），再做方向可观测性，再 token 预算接线，再 route 数据完整性，
   energy 与 Advisor 一律"先离线、先 telemetry、先单点实验"。
4. 总红线改为评审建议的精确表述：**不修改 provenance gate；不让派生注意力成为证据；
   尽可能不增加模型调用；新增计算与 token 成本必须可测量、可开关、可回滚。**

---

## 1. D-Swarm 内核背景（评估者 grounding）

| 层 | 模块 | 一句话职责 |
|---|---|---|
| 事件脊 | `core/events.py` `event_bus.py` `session_store.py` | 有序类型化事件流 + JSONL 持久化，前端是哑订阅者 |
| 证据图 | `swarm/shared_graph.py` | 事件溯源 SQLite；facts/intents 是物化视图 |
| 通知总线 | `swarm/insight_bus.py` | 跨 worker 已验证事实/死路/flag 的内存 fan-out |
| 执行器 | `solver/cli_driver.py` `cli_solver.py` | shell 出 `pi` CLI（单发 worker） |
| 规划/评审 | `solver/reason.py` `swarm/reason_scheduler.py` `review_flow.py` | 中央 Reason 规划 + Reviewer 仲裁 |

**append-only 语义的当前真实状态（评审 §1.10，v2 已核实）**：`events` 表事实内容只 INSERT，
但存在两处派生元数据 UPDATE——`add_evidence` 的 candidate→verified 提升（`UPDATE events SET
verified=1`）与 `record_fact_summary` 的 zh gist 写回（`UPDATE events SET payload`）。
代码注释将其解释为"派生 metadata"。这与 AGENTS.md "never overwrite in place" 的严格表述
存在张力。**第一批工作之一就是明确二选一语义**（见 §5.4），在此之前文档不再使用
"严格 append-only" 一词，改用"**事实内容只增不改，派生元数据可更新（待正式定稿）**"。

当前派发主循环（`reason_scheduler.py::ReasonSwarm.run`）：`initial recon（单 worker，方向 =
category）→ while 未完成：Reason 读图提 intent → 保留模型返回顺序截断到
max_intents_per_reason → 对 fresh decisions 直接 gather 并发执行 → 投影 → 循环`。
注意（评审 §1.7/§1.8，v2 已核实）：**该 gather 没有 max_workers 并发约束**，且**主派发顺序
是模型返回顺序，不是 SharedGraph 的 priority DESC 查询顺序**——两条事实都影响 §5.1 的设计。

---

## 2. 参考材料一：FishCodeTech/muteki（上游）

### 2.1 它是什么

[Project Muteki（無敵）](https://github.com/FishCodeTech/muteki)：异构多模型 CTF 解题 agent
swarm，shell 出 claude/codex/cursor 三个闭源 CLI。核心三件套与 D-Swarm 同源：**异构引擎 +
共享黑板 + provenance gate**。worker 与平台的唯一数据通道是内建 `muteki-blackboard` skill。

### 2.2 关键成绩（上游自述，能力快照）

- NYU CTF Bench 200/200 = 100%（30 分钟/题，累计 ~370M tokens、~$214，中位 2–4 分钟）
- 引擎分布 cursor 80 · claude 75 · codex 45 —— 盲区互补是上游 200/200 的核心论据
- RIFFHACK 2026 第 8 名；blackmaze 首血；HTB Insane/Hard 全类目 AK
- 设计哲学 "less is more"：不捆绑安全工具、网络全开、worker 自行安装依赖

### 2.3 架构（四阶段 + 每 tick 协作环）

| 阶段 | 何时 | 做什么 |
|---|---|---|
| ① Prepare | run 开始 | 建黑板、挂附件、健康检查、装 skill、起容器 |
| ② Recon Race | 冷启动 | **多引擎并行单发**整题，广覆盖侦察（flag→快速通道，或一批事实） |
| ③ 协调主循环 | recon 未解时 | 读黑板 → Reason 规划 → intent 上黑板 → worker 认领执行 → 回写（约 2s 一圈） |
| ④ Wind-down | 收尾 | 持久化 winner、释放 claims、终止事件、清理 |

另有 **Reviewer**（上游致谢 l4n 引入）：执行中周期性复核已记录事实、及时纠偏，是上游跳出
死循环的关键。

### 2.4 与 D-Swarm 的关系

D-Swarm（`h1kibi/D-Swarm`）在更早时点 fork（当时上游叫 dswarm，现已改名 muteki），两线
独立演进。逐文件 diff：

- **D-Swarm 领先**：`gate.py` 15KB vs 9.3KB（多 `_TEST_MARKER_RE` 等防护）；`btw.py` 45KB vs
  28.7KB；swarm 拆成 8 个 mixin（上游是 266KB 单体）；另有 modelgateway、方向 skills、容器探针。
- **上游领先**：`_run_race`/`_run_race_scout`（D-Swarm 已删）；claude/codex/cursor 三引擎驱动
  （D-Swarm 在 pi-only 提交中移除）；0.2.4 自定义 endpoint 健康检查跑真实 CLI 回合；0.2.5
  探测镜像真实 UID/GID 再 chown；容器内强制 container backend、拒绝静默回退 host。

---

## 3. 参考材料二：Armur-Ai/Pentest-Swarm-AI

Go 1.24、AGPL-3.0，自定位"第一个真正的 swarm"。三原则：

1. **Stigmergy**：agent 靠读写共享黑板间接协调；finding 带信息素权重，按类型半衰期衰减。
2. **Emergence**：攻击链从黑板状态涌现，不由中央规划。
3. **Decentralization**：每 agent 只有自己的触发器谓词；调度器只做并发/预算/关闭。

工程护栏：Postgres+pgvector 黑板、per-type 半衰期（`pheromones.yaml`）、scope 双层强制、
cleanup registry（逆序、SIGINT 也跑）、Claude prompt caching、per-agent token 预算
（软警告+硬上限）、CVSS v3.1、md/html/json/SARIF 报告。记忆投毒四层防御（Ed25519 签名、
MINJA clamp、MemoryGraft 看门狗、token-bucket 限速）。Verified-PoC 门：finding 必须带
`Reproduction{Command, HTTPRequest, ExpectedIndicator}` 重放验证。

---

## 4. 参考材料三：腾讯云文章（MS08067）

第三方 PSA 解读，价值在**中立工程视角与争议清单**：去中心化复杂度更高、调试更难；项目仍
alpha；Go 生态相对小。未来方向：工具链扩展、向量学习、可视化调试、Benchmark。

---

## 5. 改进方案（v2，按评审 §4 六批优先级重排）

> 通用约束（v2 修订版，评审 §3.6）：**不修改 provenance gate；不让派生注意力成为证据；
> 尽可能不增加模型调用；新增计算与 token 成本必须可测量、可开关、可回滚。**
> 所有"在线生效"的改动都有确定性测试；所有"实验性"改动都走 telemetry/ablation。

### 5.1 第一批：基线正确性（先修地基，再做加法）

**A. priority 持久化精度**（评审 §1.6，已核实）：`propose_intent` 里 `int(raw_priority or 0)`
+ INTEGER 列，0.5/0.9 全部变 0。Reason 的浮点 priority 在 DB 层已无意义。
- 方案：`priority` 改为 REAL 存储（迁移：新列或 ×100 整数），`_open_intents` 的
  `ORDER BY priority DESC` 保持语义；`DispatchDecision.priority` 直接透传不转 int。
- 价值：在讨论任何"按优先级派发"之前，先把优先级本身修对——否则 energy 实验的基线不可靠。
- 验收：`0.5/0.9` 持久化后不丢精度（确定性测试）。

**B. `max_workers` 并发约束**（评审 §1.7，已核实）：ReasonSwarm 的 `gather` 并发上限是
`max_intents_per_reason`，`max_workers` 只在 provider 告警里当统计数用。
- 方案：`gather` 前加 semaphore（容量 `max_workers`），超出部分按顺序排队。
- 价值：这直接命中维护者的核心关切（成本均衡、provider 限流、worker 生命周期、UI 上
  "Worker 策略"的可信度）。这是比 energy 更优先的修复。
- 验收：`max_active_workers <= max_workers` 有确定性测试。

**C. append-only 语义定稿 + 守护测试**（评审 §1.10，已核实）：`test_architecture.py` 当前
只查 core 不 import apps、LF、WSL，没有 append-only/provenance 守护；而代码存在元数据
UPDATE（verified 提升、summary 写回）。
- 方案：二选一定稿——(i) 严格事件不可变（提升/摘要写旁表或新事件）；或 (ii) "原始事实
  不可变、派生字段可更新"并在 AGENTS.md 明确。选定后补一个真正的 architecture guard 测试
  （断言 `events` 表的 `kind/payload.fact/actor/ts` 字段不被 UPDATE；允许的元数据列白名单化）。
- 价值：provenance 是项目圣物；"append-only"一词在文档、注释、代码间语义不一致时，任何
  新功能都可能在不自知中破坏溯源。先把语义钉死。
- 验收：guard 测试存在且绿；白名单外的 UPDATE 被测试拒绝。

### 5.2 第二批：方向路由可观测性（评审 §2.1 修改后采纳）

**现状核实**（v2 修正）：decision 层路由测试**已存在**
（`tests/test_reason_swarm.py::test_reason_decisions_route_direction_to_profile`，crypto→
pi-crypto / 空→category / rev→pi-rev），v1 "无回归测试"的说法错误。真实缺口是：
(a) **非法 direction 在 parser 层就丢失**——`parse_reason_reply` 里
`direction=canonical_direction(raw.get("direction"))`，`"reversing"` 进 scheduler 前已变空串，
`_decisions_from_reason` 拿不到原始错误词；(b) 缺 decision → profile → engine/镜像/skills/
prompt 的 runtime 层端到端接线测试。

- 方案 A（parser 层）：`ReasonResult` 保留 `raw_direction` + `direction_diagnostic`
  （"canonicalized:reversing→unknown" / "empty:fallback-to-category" / "invalid:xyz"），
  进 `propose_intent` payload 与黑板 delta，操作员可见。
- 方案 B（fallback 规则）：机械规则**只对空/非法 direction 做高置信 fallback**；模型给出
  合法 direction 时默认尊重模型，冲突只记 telemetry 不覆盖（评审例证："extract RSA key from
  binary" 同时命中 crypto+rev，"decrypt cookie through web endpoint" 同时命中 web+crypto，
  关键词覆盖模型的误判率高）。所有 direction 规则（aliases、canonical id、profile、category
  aliases、关键词）收敛到**单一权威结构**（`worker_profiles.py` 扩展），避免双表漂移。
- 方案 C（misc）：第一版**不做 LLM triage**（评审 §2.1：与"尽可能不加模型调用"矛盾），用
  机械 seed（description/attachment 扩展名关键词）；flash triage 单独标注为**有成本可选项**。
- 方案 D：runtime 接线测试——断言 direction 最终传到正确 engine/镜像/skills/prompt。

- 价值：把"规划器打错方向"从静默故障变成可观测事件；只在不牺牲模型合法决策的前提下兜底；
  成本零新增调用（第一版）。
- 验收：非法 raw direction 可观测；合法 direction 不被低置信关键词覆盖；direction 最终传到
  正确 profile/engine/runtime。

### 5.3 第三批：token budget 接线（评审 §2.4 优先采纳）

**现状**：`MemoryBoard.agent_budget`/`charge_agent` 状态机完整，但 `reason_scheduler._one`
里传 `tokens=0`；`SolveOutcome` 没有 token delta 字段。v1 称"一行改动"，不准确。

- 方案：`SolveOutcome` 增 `tokens: int`（`CliSolver.run()` 结束写 `_tokens_spent()`）；
  `_one` 在 `_absorb_outcome` 前按 worker 实例结算一次；recon/普通/retry/recovery 统一走
  同一结算路径；恢复/重试按**新 worker 实例**结算（天然去重，不跨实例累计）；agent 维度
  用 solver_id（每 spawn 唯一，与 board 的 charge_agent 键一致）。
- 价值：复活已存在的 per-worker 预算告警（软警告→硬上限→暂停派发），是成本均衡的直接抓手；
  也是 provider error 关联分析的数据源。
- 验收：token 对 recon、worker、retry、recovery 恰好结算一次（确定性测试）。

### 5.4 第四批：route 数据完整性与 telemetry（energy/Advisor 的地基）

**现状核实**（评审 §1.2/§1.3，已核实）：`BoardProjector.project_event` 只投影 `fact_added`；
`FLAG_FOUND`/`POC_SAVED`/`dead_end` 不进入 Board；`Finding` 无 `route_hash` 字段、`created_at`
是投影时刻而非原始 event 时间（重建投影会重置衰减时钟）；`MemoryBoard.subscribe` 不自动
读写 `cursor`/`commit_cursor`（API 存在但 durable consumer 语义未封装——v1 的说法不准）。
注意：`SQLiteSharedGraph.subscribe_events(after_seq)` **已存在**，是原始事件流，比 Board
订阅更适合做 Advisor 的事件源。

- 方案：① 投影补 route_hash（从 `payload.route_hash`/`finding` 数据提取）与原始 event ts；
  ② 定义 dead-end/flag/PoC/route-less finding 的归属规则；③ write-rate、dedupe 命中率、
  summary 膨胀等 telemetry 指标；④ 建立可重放 benchmark fixture（事件日志回放）。
- 价值：energy 与 Advisor 的正确性都依赖"route 归属 + 真实时间"，没有这层数据任何实验都是
  在错误基线上测；telemetry 本身就是 §5.5 写限速的证据来源。
- 验收：投影保留原始 event 时间与 route_hash；重放 fixture 可复现。

### 5.5 第五批：route energy —— 先离线实验，后 tie-breaker（评审 §2.2）

**v1 公式的问题（评审 §2.2，全部成立）**：dead-end 公式 `-penalty·(1-exp(-age/τ))` 随
时间**增强**惩罚，与注释"随时间衰减"矛盾；正贡献求和后 clamp 易饱和（无法区分一条强证据
与大量同源回声）；actor cap 0.4 无 trace 支撑；冷启动（全 route≈0）未定义；review/verifier
"不抢热"可能饿死。

- 修订公式（v2 草案，仍属实验参数，不做生产常量）：
  - 正贡献：`E_route = 1 - Π(1 - w_i·exp(-age_i/τ))`（概率合并式，天然防饱和、有上界）；
  - dead-end：`-penalty·exp(-age/τ)`（惩罚随时间减弱；**图内 suppression 不受影响**——衰减
    只发生在注意力视图）；
  - 冷启动：E 全为 0 或方差 < ε 时**回退 planner 原顺序**；
  - review/verifier：独立 lane，不参与热度竞争（保底服务额度）。
- 落地路径（严格分阶段）：
  1. 只输出 route heat **telemetry**（黑板 delta + 日志），不改任何派发；
  2. 用 benchmark/replay fixture 做 ablation（权重/半衰期/cap 扫描）；
  3. 证明有效后，在线第一版**只作 planner priority 相同或接近时的 tie-breaker**；
  4. 永不覆盖 planner priority；route heat 永不写回 evidence graph。
- 价值：能量排序的目标（把既定预算投向证据最厚的路线）不变，但 v1 直接进生产派发是拿
  未验证公式去替换一个已工作的调度器；分阶段后收益可测、风险可控。
- 验收：冷启动保持 planner 原顺序；dead-end 惩罚随时间减弱；review/verifier 不因热度饥饿；
  能量计算不得每 0.5s 全量扫历史（复杂度约束/缓存策略必须有）。

### 5.6 第六批：Advisor 触发器 —— 缩为单触发器实验（评审 §2.3）

**v1 的核心矛盾（评审 §2.3，成立）**："Advisor 调 `propose_intent`" 与"Reason 仍是唯一
裁决者"不可兼得——`propose_intent` 写的是正式 intent，派发器消费它则 Advisor 就是决策者，
不消费则是一堆不执行的记录。必须二选一：

- **AdvisorySuggestion 模型**：Advisor 只写非执行性建议，Reason 下一轮决定是否转化
  （Reason 仍是唯一裁决者，但没有"事件即执行"的 OODA 收益）；
- **Formal Intent 模型**：Advisor 直接写可 claim intent（OODA 快，但必须承认决策主体
  多元化，需要完整仲裁与预算协议）。

- v2 建议：**第一版只做一个单触发器实验**——选因果关系最确定、低重复、低 token 风险的
  **flag-scout**（multi-flag 梯子题：`FLAG_FOUND` → propose 下一层侦察），采用
  AdvisorySuggestion 模型 + 事件源用 `shared_graph.subscribe_events`（已存在，不依赖
  Board 投影扩展）。补齐：stable idempotency key（`flag-scout::{flag}`）、global fanout
  budget（≤N 次/run）、cooldown、pause/stop/resume 生命周期合规。四触发器体系等调度仲裁
  统一后再设计。
- 价值：保留"事件即反应"的 OODA 收益，但把仲裁/预算/幂等/生命周期的完整协议作为前置条件
  而不是事后补丁；单触发器实验产出真实 trace 后再决定是否扩面。
- 验收：相同 trigger 重放不重复生成 intent；不绕过 operator pause/stop/resume。

### 5.7 注意力卫生（评审 §2.4 拆分采纳）

| 子项 | v2 结论 |
|---|---|
| token accounting | **优先采纳**（§5.3），但需要正式 outcome/accounting 接口与去重结算，不是"一行改动" |
| write-rate telemetry | **采纳**：先记录不拒绝——每 worker 每分钟 fact/dead_end 数、verified/candidate 比例、dedupe 命中率、route-less 比率、summary 膨胀、provider error 与 burst 关联 |
| 写入硬限速 | **暂缓**：`60s/30 条` 可能误伤回合末批量抽取事实/扫描结果的合法高产 worker；存储层静默拒绝伤害审计性。将来若做，需结构化结果码（不复用 -1）、UI/Reviewer 可见、SQLite/Postgres 语义一致、不丢关键 dead-end/witness、阈值有真实 trace 支撑 |
| burst 指纹 / actor cap | **降级为实验参数**：run-75377 的主因可能已被 identity normalization 修复，不能用旧事故证明"近义重复仍是当前主因"，需新 trace；actor cap 不做硬编码 0.4 |

---

## 6. 已讨论并否决的方案（决策上下文，v2 维持不变）

| 否决项 | 理由 |
|---|---|
| 并行 race/recon（上游 `_run_race`） | 上游目标函数是首血（时间最贵）；D-Swarm 是成本均衡。并发预算留给窄 scope explore intent |
| 异构模型补齐引擎异构 | 内核已支持 per-profile provider/model/effort，是配置问题非内核优化 |
| 多见证共识（≥2 actor 同事实 → 升级标注） | worker 开局读 board，第二 actor 的确认多是回声而非验证；主动验证版 = 已有 review/verifier intent，重复工作 |
| 全面去中心化（废中央 Reason） | 腾讯云文章争议（调试难、alpha）+ 上游 200/200 实为协调器+Reviewer 架构 |
| 重上 claude/codex/cursor 闭源驱动 | 与 pi 引擎 + BTFly 路线冲突 |
| Postgres+pgvector 替换 SQLite 黑板 | 单文件事件溯源是卖点；postgres_board 已是可选后端 |
| 捆绑 ProjectDiscovery 8 工具适配器 | 违背 "less is more"（worker 自带工具） |

---

## 7. v1 不准确表述与修正（对应评审 §6，逐条核实后确认）

| v1 表述 | 核实结果 | v2 修正 |
|---|---|---|
| "`MemoryBoard.subscribe` 完整实现 cursor+commit_cursor 光标幂等" | subscribe 不自动读写 cursor（已读代码确认） | "subscribe 与 cursor API 都存在，但 durable consumer 语义未封装" |
| "四个 Advisor 复用 subscribe，零额外读开销" | `project_event` 只投影 fact_added；FLAG/POC/dead_end 不进 Board（已确认） | "复用部分基础设施；需补事件投影、cursor、幂等、预算与复杂度控制" |
| "中央 Reason 仍是唯一裁决者，同时 Advisor 调 propose_intent" | 逻辑矛盾（评审论证成立） | 二选一模型（§5.6） |
| "misc triage 符合零新增 LLM 调用" | 与总约束矛盾 | "有成本可选项；第一版用机械 seed" |
| "charge_agent 是一行改动" | `SolveOutcome` 无 token delta 字段（已确认） | "需要 outcome 接口与 retry/recovery 去重结算" |
| "append-only 由 test_architecture.py 同类测试守护" | 该测试只查 import 方向/LF/WSL（已确认） | "当前尚无 append-only/provenance architecture guard（第一批补）" |
| "复合题 crypto intent → pi-crypto profile 无回归测试" | `test_reason_decisions_route_direction_to_profile` 已存在（已确认） | "decision 层已有，缺 runtime/factory 端到端接线测试" |

---

## 8. 验收标准（替换 v1 §7，采用评审 §5 清单）

- [ ] `max_active_workers <= max_workers` 有确定性测试
- [ ] `0.5/0.9` priority 持久化后不丢精度
- [ ] 非法 raw direction 可观测
- [ ] 合法 direction 不被低置信关键词错误覆盖
- [ ] direction 最终传到正确 profile/engine/runtime
- [ ] token 对 recon、worker、retry、recovery 恰好结算一次
- [ ] energy 冷启动保持 planner 原顺序
- [ ] dead-end 惩罚随时间减弱
- [ ] review/verifier 不因 route heat 饥饿
- [ ] energy 计算不能每 0.5 秒全量扫描全部历史（复杂度约束或缓存策略）
- [ ] Advisor 相同 trigger 重放不会重复生成 intent
- [ ] Advisor 不能绕过 operator pause/stop/resume 生命周期
- [ ] 所有 attention/pheromone 结果只能影响调度，不能绕过 provenance gate
- [ ] 离线 eval 零假 flag
- [ ] 相同 token/worker 预算下 solve-rate 不退化，并提供成本与等待时间对照

---

## 9. 留给下一轮评估者的开放问题（v1 §8 的延续）

1. **append-only 语义选哪边**？(i) 严格事件不可变（verified 提升/summary 写旁表或新事件）
   还是 (ii) "原始事实不可变、派生字段可更新"并在 AGENTS.md 明确定稿？这影响 guard 测试
   与所有未来图功能。
2. **AdvisorySuggestion vs Formal Intent**：单触发器实验选哪个模型？flag-scout 用
   Suggestion 模型会损失多少 OODA 收益，值不值？
3. **priority 迁移**：REAL 列 + ×100 整数两种方案，对 `_open_intents` 排序、web 设置页、
   已有 DB 迁移的影响哪种更小？
4. **energy 的概率合并式**（`1-Π(1-w·exp(-age/τ))`）在 route 无归属的 fact（route-less
   ratio 当前占比？）上的行为：route-less 事实进哪个 bucket？
5. **telemetry 的载体**：write-rate/膨胀指标走黑板 delta 还是独立 metrics 文件/表？如何
   让离线 ablation 可复现？
