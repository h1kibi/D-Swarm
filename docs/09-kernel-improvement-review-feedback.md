# D-Swarm Swarm 内核改进方案评审反馈

> 目标读者：DeepSeek 或其他准备修订 `docs/08-oss-research-and-kernel-improvements.md` 的第三方评估者。
> 目的：基于 D-Swarm 当前代码状态，对原方案进行事实校正、风险评估和可落地改写建议。
> 评审方式：不按外部建议盲改；所有关键结论均对照当前仓库代码、测试与项目不变式核查。

---

## 0. 评审结论摘要

原文档的问题意识是有价值的：它抓住了 D-Swarm 当前在方向路由、注意力分配、事件反应速度、worker 写入卫生和预算反馈方面的真实改进空间。但当前版本不能按 §7 直接实施，原因是若干基础事实被高估或描述不准确，且部分提案之间存在硬性矛盾。

建议将原文档结论从：

> 四个提案分两批落地。

改为：

> 方向诊断和 token 预算接线进入近期实施；route energy 进入离线 telemetry/ablation；Advisor reactors 和写入硬限速等待基础正确性、route 数据完整性与真实 trace 支持。

总体 verdict：

| 方案 | 评审结论 |
|---|---|
| 方向路由审计 | **修改后采纳，高优先级**。先补 raw direction diagnostics 和更深的 runtime 接线测试；机械规则只做空/非法 direction 的高置信 fallback。 |
| route/intent 级能量 | **保留概念，当前设计不可直接采纳**。先修 priority 精度、`max_workers` 并发约束、route lineage，再做离线 ablation。 |
| Advisor 事件触发器 | **整体延后，或缩成单触发器实验**。当前事件源、仲裁、执行入口、去重、预算模型都未定义完整。 |
| 注意力卫生 | **拆分采纳**。token accounting 优先；write-rate telemetry 其次；硬限速和 actor cap 暂作为实验参数。 |

红线不变：不能弱化 provenance gate；不能让黑板/注意力/共识结果绕过 flag gate；SharedGraph append-only 语义需要先澄清并测试守护。

---

## 1. 必须修正的代码事实

### 1.1 “信息素已有半套”部分成立，但没有进入正常 Reason 主链路

当前确实已有：

- `dswarm/swarm/board.py::Finding.pheromone()`；
- `PheromoneSettings.defaults()`；
- `FindingPredicate`；
- `Board.subscribe()`；
- `BoardProjector.sync()`；
- 前端 finding/pheromone 展示。

但正常有 SharedGraph 时，Reason 使用的是：

```python
graph.to_reason_summary()
```

而不是：

```python
_board_summary()
```

`_board_summary()` 才会直接查询 Board finding/pheromone。因此应改写为：

> 信息素计算和展示层已经存在，但尚未进入 SharedGraph 驱动的正常 Reason 主规划输入。

不要写成“只差接一根线”。真正接入还缺：route 归属、原始 event 时间、投影覆盖、增量聚合、复杂度控制和派发队列定义。

### 1.2 `subscribe()` 没有实现文档所称的完整持久游标幂等

原文说：

> `MemoryBoard.subscribe(predicate)` 完整实现订阅/谓词匹配/历史重放/`cursor`+`commit_cursor` 光标幂等。

这不准确。

当前 `MemoryBoard.subscribe()` 做了：

- 建立内存队列；
- 回放 `query_findings(predicate)` 的历史 finding；
- 后续写入时推送匹配 finding。

但它不会自动读取 `cursor()`，也不会自动调用 `commit_cursor()`。

`PostgresBoard.subscribe()` 也只是使用局部 cursor 轮询新 finding，同样不会自动提交持久消费 cursor。

建议原文改成：

> Board 已有订阅、谓词过滤、历史回放和独立 cursor API，但还没有一个 crash-safe durable consumer abstraction。若 Advisor 需要可靠消费，必须显式定义 cursor 读取、commit、重放和幂等键。

### 1.3 `BoardProjector` 覆盖不足以支撑四个 Advisor

当前投影主要处理结构化 `fact_added`。原方案中的四类 Advisor 事件并非都已可由 Board 统一订阅得到：

- `FLAG_FOUND` 不一定由 projector 投影为 Board finding；
- `POC_SAVED` 不一定投影；
- `dead_end` 不一定投影；
- `NEW_SURFACE` 只有结构化 finding 才可能被投影。

此外还有两个数据完整性问题：

- 投影通常使用投影发生时的时间作为 `created_at`，没有稳定保留原始 graph event 时间；重建投影可能重置 pheromone 衰减时钟。
- Finding 通常不保留稳定 `route_hash`，无法仅靠 Board 可靠计算 route-level energy。

因此 Advisor 不是“复用 subscribe 零额外读开销”即可完成。

### 1.4 方向路由已有回归测试，原文“无测试”结论错误

当前已有：

```python
def test_reason_decisions_route_direction_to_profile():
```

该测试验证：

- `direction="crypto"` → `pi-crypto`；
- 空 direction → challenge category 的 profile；
- `direction="rev"` → `pi-rev`。

所以原文 §5.0 缺口 3 不应写“无回归测试”。更准确的缺口是：

> 已有 scheduler decision 层测试，但还缺 worker factory/runtime 层的深层接线测试，例如 profile 是否最终传给正确 engine、镜像、skills 和 prompt。

### 1.5 非法 direction 在 scheduler 前已丢失，不能只改 `_decisions_from_reason`

当前 parser 中直接执行：

```python
direction=canonical_direction(raw.get("direction"))
```

如果模型输出 `direction="reversing"`，进入 scheduler 前已经变成空字符串。此时 `_decisions_from_reason()` 不知道原始错误词是什么。

因此若要实现 `direction_override`、invalid-direction telemetry 或机械 fallback，不能只改 scheduler。需要在 Reason parse 层保留：

- `raw_direction`；
- canonical 后 direction；
- parse diagnostic / fallback reason。

### 1.6 `Intent.priority` 持久化存在精度丢失

`Intent.priority` 是 float，默认 `0.5`。但 SharedGraph 当前持久化时会执行类似：

```python
priority = int(raw_priority or 0)
```

数据库物化表中 priority 也是整数语义。这导致：

- `0.5 → 0`；
- `0.9 → 0`；
- `1.0 → 1`。

在讨论 route energy 如何修复 priority/FIFO 前，必须先修正这个基础问题。否则 energy 实验的基线本身不可靠。

### 1.7 `max_workers` 当前没有严格约束 ReasonSwarm 的 worker 并发

当前 ReasonSwarm 主流程是：

```python
capped = decisions[: self.max_intents_per_reason]
fresh = ...
await asyncio.gather(*[_one(d) for d in fresh], return_exceptions=True)
```

未见以 `self.max_workers` 为容量的 semaphore 或 fresh 截断。因此在 `max_workers=2`、`max_intents_per_reason=4` 这类配置下，一轮 Reason 可能同时启动 4 个 worker。

这比 route energy 更优先，因为它直接影响：

- 成本；
- provider 限流；
- worker 生命周期；
- UI 上“系统 Worker 策略”的可信度；
- 大批 provider error 的告警阈值。

### 1.8 ReasonSwarm 主派发并不是简单的 SharedGraph FIFO

原文把现状描述为：

> `_open_intents` 按 `ORDER BY priority DESC, created_seq`，priority 相等时退化 FIFO。

这只覆盖 SharedGraph open-intent 查询路径，不等于 ReasonSwarm 当前主派发行为。当前主链路更接近：

1. Reason 返回 JSON intents；
2. 保留模型返回顺序；
3. 截断到 `max_intents_per_reason`；
4. 对 fresh decisions 直接 `gather()`。

因此如果要引入 energy，必须先定义它作用于哪一层：

- Reason 本轮返回的 decisions；
- SharedGraph 中所有 open intents；
- operator directives；
- recovery intents；
- review/verifier intents。

否则会出现多条派发路径各自排序、互相绕过的情况。

### 1.9 `charge_agent` 接电不是“一行改动”

当前确实存在 `tokens=0` 的未接线问题。但 `SolveOutcome` 没有稳定暴露本次 worker 执行的 token delta。

`CliSolver._tokens_spent()` 目前主要用于生命周期事件，不等于 scheduler 可以安全使用的结算接口。

真正修复至少需要定义：

- worker outcome 暴露 token delta 或起止快照；
- recon、普通 worker、retry、recovery 统一结算；
- 避免恢复/重试重复计费；
- aggregate budget 和 per-agent budget 同步；
- agent 维度是 profile、solver id 还是具体 worker instance。

所以这是高价值工作，但不是“一行改动”。

### 1.10 当前 append-only 语义本身需要先澄清

项目不变式要求 SharedGraph append-only。但当前代码存在修改旧 event 的行为，例如：

```sql
UPDATE events SET verified=1, confidence=? WHERE seq=?
UPDATE events SET payload=? WHERE seq=?
```

其中 summary 写回会直接修改旧 event payload。代码注释将其解释为派生 metadata，但这与 AGENTS.md 中“never overwrite in place”的严格表述存在张力。

在原文继续把“append-only 不变式”作为总红线前，必须先明确项目采用哪一种语义：

1. **严格事件不可变**：verification upgrade、summary 都写新事件或旁表，旧 event 一字不改。
2. **原始事实不可变，派生字段可更新**：允许更新 verified/confidence/summary 等 metadata，但文档必须明确这不是严格 append-only。

同时，原文称 `test_architecture.py` 已守护 append-only，也不准确。当前该测试主要检查 core 不导入 apps、shell LF 和 WSL guard，并没有 append-only/provenance architecture guard。

---

## 2. 四项提案逐项评审与改写建议

### 2.1 方向路由审计：修改后采纳

采纳方向：

- 保留 raw direction 与 canonical direction；
- 非法 direction 产生可观测 diagnostic；
- 增加 scheduler 到 worker runtime 的深层测试；
- 建立单一权威方向规则定义。

不建议原样采纳：

> 模型 direction 与机械关键词冲突时，机械预判胜出。

原因：关键词很容易误判。例如：

- “extract RSA key from binary” 同时命中 crypto 和 rev；
- “decrypt cookie through web endpoint” 同时命中 web 和 crypto；
- “binary protocol fuzzing” 不一定是 rev。

建议改成：

- 模型 direction 合法时默认尊重模型；
- direction 为空或非法时，才允许高置信机械 fallback；
- 合法 direction 与机械规则冲突时只记录 telemetry，不直接覆盖；
- 若连续低价值或 worker 失败，再交给 Reason 重规划。

关于 misc triage：原文提出一次 flash LLM 调用，但这与 §5 的“零新增 LLM 调用点”矛盾。建议第一版不要做 LLM triage，而是机械 seed；若以后要做 flash triage，应单独标注为“有成本可选策略”。

### 2.2 route/intent 能量：保留概念，先离线实验

当前公式不能直接进入生产派发。

主要问题：

1. dead-end 公式方向错误。原公式：

   ```text
   -penalty * (1 - exp(-age/tau))
   ```

   会让旧 dead-end 惩罚随时间越来越强。若希望惩罚随时间减弱，应更接近：

   ```text
   -penalty * exp(-age/tau)
   ```

2. 正贡献求和后 clamp `[0,1]` 容易快速饱和，无法区分一条强证据和大量同源回声。

3. 固定 actor cap `0.4` 缺少 trace 支持，可能系统性低估单 worker 深耕正确链的价值。

4. 冷启动未定义。所有 route energy 接近 0 时，应回退 planner priority、planner order 或 created sequence。

5. verifier/review 不能因为“不抢热”而被饿死，应有独立 lane 或保底服务额度。

建议改写为：

- 第一阶段只做 energy telemetry，不改变在线派发；
- route heat 不写回 evidence graph，只作为派生视图；
- 用 benchmark/replay 做 ablation；
- 证明有效后，在线第一版只作为 planner priority 相同或接近时的 tie-breaker；
- 不让 energy 替代 planner priority。

### 2.3 Advisor reactors：整体延后或缩成单触发器实验

原方案存在核心矛盾：

> Advisor 调用 `graph.propose_intent()`，但中央 Reason 仍是唯一裁决者。

`propose_intent()` 写入的是正式 intent。如果派发器消费它，Advisor 已经是实际决策者；如果派发器不消费，它就是不会执行的记录。

因此必须先选择一种模型：

- **AdvisorySuggestion 模型**：Advisor 只写非执行性建议，Reason 下一轮决定是否转化为 Intent。Reason 仍是唯一裁决者，但无法做到事件即执行。
- **Formal Intent 模型**：Advisor 直接写可 claim intent。OODA 更快，但必须承认 Reason 不再是唯一裁决者，需要完整仲裁和预算协议。

当前还缺：

- stable idempotency key；
- route/goal 语义去重；
- per-route quota；
- global fanout budget；
- cooldown 与 max-per-run 的跨 Advisor 协调；
- 与 operator directives、Reason intents、review/verifier intents 的优先级关系；
- run 结束、暂停、恢复、崩溃恢复时的生命周期。

建议原文不要把四个 Advisor 放入第一批。可改成：

> 先选择一个因果关系最确定、低重复、低 token 风险的触发器做实验，并提供确定性测试与 trace replay。四触发器体系等待调度仲裁统一后再设计。

### 2.4 注意力卫生：拆分采纳

#### token accounting：优先采纳

当前 per-agent budget 状态机没有真实 token 输入，这是明确缺口。建议优先做，但需要正式 outcome/accounting 接口，不能写成“一行接电”。

#### write-rate telemetry：采纳

先记录而不是拒绝：

- 每 worker 每分钟 fact/dead_end 数；
- candidate/verified/dead_end 比例；
- dedupe 命中率；
- route-less finding 比率；
- Reason summary 膨胀情况；
- provider error 与 worker burst 的关联。

#### 写入硬限速：暂缓

`60s/30 条` 可能误伤合法高产 worker，尤其是回合结束时批量抽取事实或扫描结果的情况。存储层静默拒绝也会伤害审计性。

如果未来确实需要拒绝，应至少满足：

- 返回结构化结果码，而不是复用 `-1`；
- UI/Reviewer 可见；
- SQLite/Postgres 后端语义一致；
- 不丢关键 dead-end 或 witness；
- 有真实 trace 支持阈值。

#### burst 指纹和 actor cap：作为实验参数

不能继续仅用旧 run-75377 证明“近义重复仍是当前主因”，因为该事故的主因可能已由 identity normalization 修复。需要新 trace。

actor cap 也应作为离线参数，不应直接硬编码为 0.4。

---

## 3. 对原文 §8 六个挑战问题的直接回答

### 3.1 “接电”判断是否成立？

部分成立。信息素数学、Board、投影和 UI 已有，但主决策路径没有真正接通；还缺 route lineage、原始 event 时间、durable subscriber、投影覆盖和复杂度控制。

### 3.2 能量公式是否合理？

目前不合理。dead-end 公式方向错误；actor cap 缺少证据；正贡献易饱和；冷启动和 review/verifier 饥饿问题未定义。

### 3.3 Advisor 的 `max_per_run + cooldown` 足够吗？

不够。它只限制单 Advisor 的频率，不能限制多 Advisor 共同扩散、Reason 重复提议、recovery/retry、operator directive 竞争和每个 intent 实际 token 消耗差异。还需要全局 fanout budget、route quota、稳定语义去重和统一仲裁。

### 3.4 写限速和 actor cap 会误伤吗？

有较高风险。写限速可能丢合法批量证据；actor cap 可能低估单 worker 深挖正确路线。两者都应先 telemetry/ablation，不应直接成为生产硬规则。

### 3.5 机械预判表会和 canonical mapping 重复维护吗？

会。应建立单一权威 direction rule 结构，统一 aliases、canonical id、profile、category aliases、关键词模式等。但关键词只能作为 fallback/diagnostic，不能与 alias canonicalization 拥有同等覆盖权。

### 3.6 四提案是否真的零新增 LLM、零触碰图、零隐式成本？

不能。

- misc triage 明确新增 LLM 调用；
- Advisor `propose_intent()` 会写 SharedGraph；
- formal Advisor intent 会改变决策主体；
- subscribe、projection、energy 计算都有读/轮询/CPU 成本；
- route heat 写进 Reason summary 会增加 prompt token；
- 若每 tick 全量扫描 events，复杂度会随 run 历史线性增长。

建议改写为：

> 不修改 provenance gate；不让派生注意力成为证据；尽可能不增加模型调用；新增计算和 token 成本必须可测量、可开关、可回滚。

---

## 4. 建议 DeepSeek 重写后的落地优先级

### 第一批：基线正确性

1. 修正 priority float 持久化语义。
2. 让 ReasonSwarm 实际 worker 并发严格不超过 `max_workers`。
3. 明确 SharedGraph append-only 语义。
4. 增加真正的 append-only/provenance architecture guard。

### 第二批：方向路由可观测性

1. Reason parser 保留 `raw_direction`。
2. 记录 canonicalization/fallback diagnostic。
3. 机械规则只对空或非法 direction 做高置信 fallback。
4. 增加 decision → profile → worker factory/runtime 的完整测试。
5. misc triage LLM 暂不作为第一版。

### 第三批：token budget 接线

1. 为 worker outcome 增加可靠 token delta。
2. recon、worker、retry、recovery 统一结算。
3. 防止恢复/重试重复计费。
4. 明确 per-agent 统计维度。
5. UI 展示预算、告警和 provider error 来源。

### 第四批：route 数据完整性与 telemetry

1. 补齐 route lineage。
2. 定义 dead-end、flag、PoC、route-less finding 的归属规则。
3. 保留原始 event 时间。
4. 增加 write-rate、重复率、summary 膨胀指标。
5. 建立可重放 benchmark fixture。

### 第五批：energy 离线实验

1. 修正公式。
2. 对权重、半衰期、actor cap 做 ablation。
3. 第一版只输出 route heat telemetry。
4. 证明有效后，只作为在线派发 tie-breaker。
5. 不覆盖 planner priority，不写回 evidence graph。

### 第六批：单 Advisor 实验

只选择一个低风险触发器，先补齐幂等键、global budget、route quota、生命周期和仲裁协议。不要第一批实现四个 Advisor。

---

## 5. 建议替换原文 §7 的验收标准

建议原文新增或替换为以下验收点：

- `max_active_workers <= max_workers` 有确定性测试。
- `0.5/0.9` priority 持久化后不丢精度。
- 非法 raw direction 可观测。
- 合法 direction 不被低置信关键词错误覆盖。
- direction 最终传到正确 profile/engine/runtime。
- token 对 recon、worker、retry、recovery 恰好结算一次。
- energy 冷启动保持 planner 原顺序。
- dead-end 惩罚随时间减弱。
- review/verifier 不因 route heat 饥饿。
- energy 计算不能每 0.5 秒全量扫描全部历史，必须有复杂度约束或缓存策略。
- Advisor 相同 trigger 重放不会重复生成 intent。
- Advisor 不能绕过 operator pause/stop/resume 生命周期。
- 所有 attention/pheromone 结果只能影响调度，不能绕过 provenance gate。
- 离线 eval 零假 flag。
- 相同 token/worker 预算下，solve-rate 不退化，并提供成本与等待时间对照。

---

## 6. 建议原文删除或改写的表述

建议删除或改写以下说法：

1. “`MemoryBoard.subscribe` 完整实现 cursor+commit_cursor 光标幂等”
   → 改为“subscribe 与 cursor API 都存在，但 durable consumer 语义未封装”。

2. “四个 Advisor 复用 subscribe，零额外读开销”
   → 改为“可复用 Board/Projector 的一部分基础设施，但需要补事件投影、cursor、幂等、预算与复杂度控制”。

3. “中央 Reason 仍是唯一裁决者，同时 Advisor 调 `propose_intent`”
   → 二选一：非执行性 suggestion 或正式 Advisor intent。

4. “misc triage 符合零新增 LLM 调用”
   → 改为“misc triage 是有成本可选项，第一版可先用机械 seed”。

5. “方案四 `charge_agent` 是一行改动”
   → 改为“需要 outcome/accounting 接口和 retry/recovery 去重结算”。

6. “append-only 由 `test_architecture.py` 同类测试守护”
   → 改为“当前尚缺真实 append-only/provenance architecture guard”。

7. “复合题 crypto intent → pi-crypto profile 无回归测试”
   → 改为“已有 decision 层测试，缺 runtime/factory 端到端接线测试”。

---

## 7. 评审验证状态

本评审基于当前项目代码与测试结果：

- 已核查相关代码路径：`dswarm/swarm/board.py`、`projection.py`、`reason_scheduler.py`、`shared_graph.py`、`review_flow.py`、`runtime.py`、`swarm.py`、`dswarm/solver/reason.py`、`worker_profiles.py`、`cli_solver.py`、`types.py` 及相关测试。
- 已运行完整测试：`uv run pytest -q`，结果为退出码 0，仅有现有依赖弃用警告和预期 skip。
- 本反馈文档不修改 `docs/08-oss-research-and-kernel-improvements.md`，只作为给 DeepSeek 修订原方案的审稿意见。

---

## 10. v2 修订方案复评（2026-08-14）

### 10.1 复评结论

v2 已吸收上一轮评审的大部分关键意见：落地顺序从“先上调度机制”改为“先修基线正确性”；energy 降为 telemetry 与离线消融；Advisor 缩为单触发器实验；注意力卫生拆出了 token accounting；总红线也不再声称“零成本”。这些修订显著降低了直接破坏现有调度和 provenance 不变式的风险。

但按当前仓库代码继续核验后，v2 仍不能作为一个整体直接批准。主要剩余问题不是方向错误，而是若干数据模型、容量语义、账本幂等和事件消费路径尚未闭合。总体 verdict 是：

> **v2 可以作为分阶段研究路线；第一、二、四批在修正本文列出的设计问题后可实施，第三批需重新设计 accounting 契约，第五批只批准离线实验，第六批当前 No-Go。**

分批结论：

| 批次 | 结论 | 实施前必须补齐 |
|---|---|---|
| 第一批：基线正确性 | **Conditional Go** | priority 先消除 Python 截断；并发按 ordinary/review 分 lane；append-only 选严格 event-row immutable |
| 第二批：方向可观测性 | **Go after model correction** | diagnostics 必须逐 Intent 保存，不应放成 ReasonResult 的单值字段 |
| 第三批：token 预算接线 | **Redesign before Go** | 唯一账本、稳定 usage 幂等键、instance/profile/provider 三层预算维度 |
| 第四批：route 数据完整性 | **Conditional Go** | lineage、event time、route-less 分类、durable replay 与独立 telemetry 载体 |
| 第五批：energy | **Offline Go / Online No-Go** | 去相关、稳定排序、exact-equal tie-break、统计显著性和 feature-off 等价性 |
| 第六批：单 Advisor | **No-Go** | 先证明 suggestion 的实际唤醒/消费路径和相对 baseline 的延迟收益 |

### 10.2 v2 已正确吸收的修改

以下修订方向可以保留：

1. **六批顺序基本正确。** priority、并发和事件不可变性确实应先于 energy/Advisor。
2. **energy 降级正确。** 先收 telemetry、做 benchmark replay，再讨论在线调度，避免未经证据的评分函数替换 planner 决策。
3. **dead-end 衰减方向正确。** `-penalty * exp(-age/τ)` 比“越旧惩罚越重”符合探索语义，但仍需定义多条 dead-end 的合并和 score 边界。
4. **review/verifier 独立 lane 正确。** 这与项目已有 `review_policy.max_concurrent` 和 reviewer 保留容量的设计方向一致。
5. **Advisor 二选一模型正确。** `AdvisorySuggestion` 与 `Formal Intent` 必须在类型和执行权限上分开，不能一边声称 Reason 唯一裁决、一边让 Advisor 直接派发。
6. **注意力卫生拆分正确。** token accounting 是可验证的基础能力；write-rate 应先测量，不应在缺乏真实分布前直接硬限速。
7. **红线表述更准确。** “不修改 provenance gate；不让派生注意力成为证据；成本可测量、可开关、可回滚”应继续保留。

### 10.3 仍需修正的代码事实与设计

#### 10.3.1 priority：根因是 Python 截断，不必先做高风险 schema migration

当前至少有两个精度丢失点：

- `SharedGraph.propose_intent()` 对 priority 执行 `int(raw_priority or 0)`；
- `SharedGraph.dispatchable_intents()` 再次执行 `int(item.get("priority") or 0)`。

虽然 schema 当前声明为 `priority INTEGER`，但 SQLite 的 INTEGER affinity 可以保存不能无损转成整数的 REAL 值。实测写入 `0.5/0.9` 后仍分别以 REAL 保存。因此第一步不应默认在 “REAL migration” 和 “×100 整数”之间二选一，而应：

1. 删除两个 `int()` 截断；
2. 在 Python/API 层统一使用 `float`；
3. 对新旧 SQLite DB 做持久化、排序和 replay 测试；
4. 只有跨后端 DDL 契约确实要求时再迁移列类型。

`×100` 方案不推荐作为默认方案：planner priority 不保证只有两位小数，固定缩放会引入精度上限和双尺度转换。另一个必须明确的问题是：当前 operator priority（例如 100/50/0/-10）和 planner priority（常见 0..1）混用，不能只修存储而不定义比较尺度。

#### 10.3.2 `max_workers`：必须保留 Reviewer 的独立保留容量

`ReasonSwarm` 当前通过 `asyncio.gather()` 并发启动本轮 fresh decisions，只有 `max_intents_per_reason` 限制，没有 `max_workers` semaphore，因此 v2 指出的缺口成立。

但 v2 的验收条件 `max_active_workers <= max_workers` 与现有设计冲突。项目已有明确语义：ordinary worker 受 `max_workers` 限制，review worker 受 `review_policy.max_concurrent` 的独立保留容量限制；测试也要求 ordinary slots 满时 Reviewer 仍可启动。

应改为三个约束：

```text
ordinary_active_workers <= max_workers
review_active_workers <= review_policy.max_concurrent
total_active_workers <= max_workers + review_policy.max_concurrent
```

实现时 ordinary 与 review 使用独立 semaphore，并明确 recon、explore、recovery、operator fallback 分别属于哪个 lane。异常、取消、重试和恢复路径都必须经过同一容量控制并可靠释放 permit。

#### 10.3.3 append-only：当前不是开放问题，必须选择严格事件行不可变

当前 `SharedGraph` 仍存在对 `events` 的原位更新：candidate 提升 verified 会更新 `verified/confidence`，fact summary 会更新 `payload`。这与当前 `AGENTS.md` 的明确不变式冲突：

> The evidence graph is append-only. Never make shared_graph overwrite in place.

因此 v2 §9 的 `(i)/(ii)` 不应继续保留为同等开放选项。本项目当前应选择：

- `events` 中的 `ts/actor/kind/payload/artifact_id/verified/confidence/dedupe_key` 均不可原位修改；
- verification promotion 写新事件，或写入可由 event log 重建的 projection/state table；
- summary 写 side table，或追加 `fact_summary_recorded` 一类新事件；
- `intents` 等物化 projection 可以更新，但必须明确其不是 canonical event log。

守护测试不能只是允许某几个字段 UPDATE；应静态禁止所有 `UPDATE events`，并增加行为测试：执行 verification、summary、review 后，原事件行字段或稳定哈希保持不变；从 event log replay 后可重建相同 projection 状态。provenance gate 及其测试保持原样，不为迁移让路。

#### 10.3.4 direction diagnostics：应逐 Intent 建模

`ReasonResult` 可以包含多个 intent，而每个 intent 都可能有不同的 raw direction。若只在 `ReasonResult` 增加一个 `raw_direction` 或 `direction_diagnostic`，数据会错配。

建议在每个 `Intent` 上保存结构化 resolution，例如：

```text
raw_direction
canonical_direction
direction_resolution = empty | explicit_auto | recognized_alias | invalid |
                       mechanical_fallback | category_fallback
```

原始值进入事件和 UI 前应做长度限制。机械 fallback 只处理空/非法 direction，不能覆盖已经合法 canonicalize 的模型输出。方向 registry 可以成为单一权威来源，但不建议把 profile、镜像、prompt、关键词和所有运行时策略塞进一个无类型巨型 dict；应使用同一 registry 下的 typed fields。

#### 10.3.5 token accounting：`solver_id` 不能同时承担所有预算语义

当前事实是：

- `SolveOutcome` 没有 token 字段；
- `CliSolver._tokens_spent()` 已能读取部分 usage；
- cost event 已区分 input/output token；
- ReasonSwarm worker outcome 仍以 `delta_tokens=0/tokens=0` 结算。

v2 提议“agent 维度使用每次 spawn 唯一的 `solver_id`，恢复/重试按新实例结算并天然去重”，这不能满足预算控制：每次 retry/recovery 创建新 ID 会重置所谓 per-agent cap，也无法限制同一 profile/provider/account 的累计消耗；“新实例”也不等于“不会重复结算”。

至少需要三个维度：

1. `worker_instance_id/solver_id`：单次生命周期归因；
2. `profile_id` 或 direction：调度预算和方向成本；
3. `provider/account_id`：配额、错误聚合和暂停派发。

每次 usage 还需要稳定幂等键，例如 `usage::<worker_instance_id>::<provider_call_id>` 或 ledger event ID，以防 outcome 重放、backend restart、recovery 和内部 provider retry 重复计费。应优先扩展现有 `CostController/COST_UPDATE` 作为唯一事实源，再由 MemoryBoard/UI 投影读取；不要形成两个独立累计器。

当前 `MemoryBoard.charge_agent()` 只累计并设置 warned，ReasonSwarm 的 `_budget_exhausted()` 检查的是 challenge global budget，尚不存在完整的“per-agent 软告警→硬上限→暂停派发”状态机。v2 必须按实际状态改写，不能把待建能力写成已具备能力。

#### 10.3.6 route projection 与 telemetry：区分 evidence、projection 和 metrics

当前 projector 主要投影 `fact_added`；Finding 没有稳定独立的 `route_hash`，投影也没有可靠保留原始 event timestamp。`subscribe_events(after_seq)` 可以轮询，但 cursor 只在调用方内存中，不是 durable consumer checkpoint。

建议：

- route lineage 优先通过 `intent_id -> intents.route_hash` 解析，而不是只信任 payload 中可缺失的 `route_hash`；
- 区分 explicit route、从 intent 继承的 route、以及 route-less 原因；
- 同时保存 event time 与 projection write time；
- benchmark replay 使用 virtual time，避免重放当天时间改变 pheromone/energy；
- 高频 write-rate、重复率、膨胀等原始 telemetry 写独立 append-only metrics artifact/table；
- UI 只接收低频聚合 delta；不要把高频 metrics 写回 evidence graph 或 Reason summary。

#### 10.3.7 energy：v2 更安全，但公式与排序语义仍未闭合

正贡献公式：

```text
1 - Π(1 - w_i * exp(-age_i/τ))
```

只有在每一项都被约束到 `[0,1]` 时才成立，需明确 weight domain 和 clamp。它能提供上界，但不会自动解决同一 actor、同源重复 finding 把 route 快速推近 1 的问题。计算前应做 identity dedupe，并按 actor/source correlation 分组；可先组内取 max 或折扣，再跨相对独立来源组合。

还需定义：

- 多条 dead-end 如何合并；
- 正负贡献的尺度和最终 score 是否 clamp；
- 同 route 同 priority 下的稳定排序键；
- feature off 时是否与 baseline decision-for-decision 一致。

“priority 接近时 tie-breaker”和“永不覆盖 planner priority”不能同时成立。若 `0.90` 与 `0.85` 因 energy 交换顺序，就已经覆盖了原 priority。第一版在线实验若未来获批，只允许 **priority 完全相等** 时使用 energy，排序建议为：

```text
lifecycle_lane -> planner_priority -> energy_if_priority_equal -> original_index
```

若后续要支持 near-equal，必须显式定义 epsilon/bucket，并承认这是有界重排，而不能继续表述为“永不覆盖 planner priority”。

#### 10.3.8 Advisor：当前缺少真实的低延迟消费路径

v2 将第一版缩为 flag-scout `AdvisorySuggestion`，但同时存在两个矛盾：

1. suggestion 不直接执行，因此不具备“事件即执行”的 OODA 收益；
2. 当前 ReasonSwarm 会等待本轮所有 worker 的 `asyncio.gather()` 完成，才进入下一 Reason cycle。

所以某个 worker 中途发现 flag 并写 suggestion 后，其他慢 worker 仍会阻塞下一轮 Reason；`shared_graph.subscribe_events()` 只解决“看到事件”，没有解决“谁被唤醒、何时消费、如何中断等待、如何受 pause/stop/budget 约束”。此外 multi-flag Reason prompt 已能看到已捕获 flag 并要求只规划剩余 flag，flag-scout 是否比现有下一轮 Reason 更快尚未证明。

第六批当前应判为 No-Go。若继续研究，先写消费协议并提供完整 trace：

```text
flag_found -> suggestion -> Reason consume -> focused dispatch
```

同时测量 `time(flag_found -> next focused dispatch)` 与 baseline，记录 suggestion 接受/拒绝原因，验证 restart/replay 幂等，以及 pause/stop 后绝不产生新 spawn。global budget、cooldown 和 durable cursor 不能只存在于进程内变量；幂等键应使用 source event seq/hash，不应直接拼接原始 flag。

### 10.4 对 v2 五个开放问题的明确答复

1. **append-only 选哪边？** 选择严格 event-row immutability。派生状态进入新事件或可重建 projection；不修改 `AGENTS.md` 来放宽该不变式。
2. **Advisor 选哪种模型？** 若继续实验，只允许 `AdvisorySuggestion`，但第六批当前 No-Go。先证明消费/唤醒路径；在此之前不生成 Formal Intent，不直接派发。
3. **priority 如何迁移？** 先删除 Python `int()` 截断并统一 float API；SQLite schema 暂不迁，验证旧 DB 后再决定。不要采用默认 ×100 缩放。
4. **route-less fact 进哪个 bucket？** 只有通过 intent lineage 可唯一解析时才继承 route；否则进入独立 `unattributed` telemetry bucket，不自动归入热门 route，第一版也不参与在线 energy。
5. **telemetry 放哪里？** 原始数据进入独立 append-only metrics artifact/table，以 event seq 和 virtual replay time 保证复现；低频 UI 摘要走 bus delta；不写回 evidence graph。

### 10.5 建议替换 v2 §8 的验收标准

#### 基线正确性

- [ ] priority 在 parser、event payload、intent projection、dispatch API、UI 和 replay 全链路保持浮点精度。
- [ ] `0.5/0.9` priority 持久化、重启、重放和排序后不丢精度。
- [ ] operator 与 planner priority 的尺度、覆盖规则和稳定排序有明确契约。
- [ ] `ordinary_active_workers <= max_workers`。
- [ ] `review_active_workers <= review_policy.max_concurrent`。
- [ ] `total_active_workers <= max_workers + review_policy.max_concurrent`。
- [ ] cancellation、异常、retry、recovery 后所有 semaphore permit 均被释放且不超发。
- [ ] 源码和 SQL guard 禁止所有 `UPDATE events`。
- [ ] verification、summary、review 后原 event row 字段/稳定哈希不变。
- [ ] 仅从 event log replay 可重建相同 verified/summary projection。

#### 方向路由

- [ ] 同一 ReasonResult 中多个 intent 各自保留正确的 raw/canonical/diagnostic。
- [ ] diagnostic 使用结构化枚举，非法 raw 值进入事件前受长度限制。
- [ ] 合法 direction 不被低置信机械规则覆盖。
- [ ] direction 经 decision → profile → worker factory → runtime 全链路一致。

#### token 与预算

- [ ] recon、ordinary worker、review、provider retry、worker recovery 的 usage 均进入同一 ledger。
- [ ] 相同 usage/outcome/event 重放不会重复计费。
- [ ] retry/recovery 不会通过生成新 `solver_id` 重置 profile/provider/account 预算。
- [ ] CostController、Board projection、API/UI 展示的 token/cost 总量一致。
- [ ] provider 未返回 usage 时状态显示为 `unknown/estimated`，不能静默记为真实 0。
- [ ] 软告警、硬上限、暂停派发和恢复条件有确定性状态机测试。

#### route 与 telemetry

- [ ] explicit route、intent-inherited route、unattributed route 可区分。
- [ ] route-less 原因使用结构化枚举，不被自动归入热门 route。
- [ ] event time 与 projection write time 分离，replay 使用 virtual time 后结果稳定。
- [ ] telemetry 原始数据不进入 evidence graph，不扩大 Reason prompt。
- [ ] durable consumer 在 crash/restart 后从 checkpoint 恢复，重放不重复产生派生记录。

#### energy 离线实验

- [ ] 正贡献项在组合前 clamp 到 `[0,1]`，weight、τ、dead-end 合并和最终 score 边界均有定义。
- [ ] correlated duplicate、同 actor/source 回声不会把 route energy 刷满。
- [ ] 冷启动及 feature off 与 planner baseline decision-for-decision 一致。
- [ ] 第一版只在 priority 完全相等时允许 energy tie-break；相同输入多次排序完全稳定。
- [ ] review/verifier 不受 route heat 排序影响，继续使用独立容量和生命周期 lane。
- [ ] energy 计算不按固定短周期全量扫描全部历史，复杂度和缓存失效策略有基准数据。
- [ ] ablation 使用相同 benchmark、模型/provider、token/worker 预算和离线网络条件。
- [ ] 报告多 seed、flag latency、worker starts、tokens、provider errors、route churn 和置信区间；样本不足时不能据此批准在线调度。

#### Advisor 实验

- [ ] 提供 `flag_found -> suggestion -> Reason consume -> dispatch` 完整 trace。
- [ ] 报告与 baseline 的 `flag_found -> next focused dispatch` 延迟对照。
- [ ] suggestion 接受/拒绝原因可追踪；被拒绝 suggestion 不生成正式 intent。
- [ ] source event 重放、进程重启和 cursor 恢复均不会重复建议/派发。
- [ ] pause、stop、budget exhausted 后 Advisor 绝不触发新 worker。

#### 总红线与评估口径

- [ ] 不修改或弱化 provenance gate；派生 attention/energy/Advisor 结果永远不是 flag provenance。
- [ ] capability eval 全程离线，零假 flag。
- [ ] “solve-rate 不退化”使用预先定义的非劣界、相同预算和多 seed/置信区间判断，不要求每个单次 run 都绝对不退化。
- [ ] 所有新机制有 feature flag，关闭后恢复 baseline 行为，并可测量额外 CPU、I/O、内存和 prompt token 成本。

### 10.6 建议 DeepSeek 对 `docs/08` 的下一轮修订动作

1. 将 §9 问题 1 直接定稿为严格 event-row immutable，不再保留放宽 append-only 的备选项。
2. 将 priority 迁移改为“先移除 Python 截断并验证 SQLite affinity”，把 schema migration 降为条件分支。
3. 将并发验收改成 ordinary/review 双 lane，保留现有 Reviewer reserved capacity。
4. 将 direction diagnostics 从 ReasonResult 级字段改为 Intent 级结构化字段。
5. 重写 token accounting：明确唯一 ledger、usage 幂等键、instance/profile/provider 三层 identity，以及 unknown usage 语义。
6. 将 energy 在线语义从“接近时 tie-break”改为“第一版仅 exact-equal priority”；补 actor/source 去相关和稳定排序。
7. 把第六批标记为设计阻塞，不再暗示 `subscribe_events` 已经提供即时 OODA；先补消费协议和 latency experiment。
8. 用本节清单替换 v2 §8 的 15 条简化验收标准。

### 10.7 验证状态

本次复评对照当前提交 `4590dfb` 的代码与测试状态完成。已执行完整 Python 测试：

```text
uv run pytest -q
exit code: 0
```

由于当前是 Windows 工作区，项目的 `init.sh` 会主动拒绝 WSL bash 跨文件系统执行，因此本次采用 `AGENTS.md` 允许的等价主检查 `uv run pytest -q`。本节只新增方案评审记录，不实施任何内核改动，也不改变 provenance gate、event spine 或 SharedGraph 行为。
---

## 11. v3 第三轮复评：测试基线冲突核验（2026-08-14）

### 11.1 结论

v3 对六批路线的技术修订基本正确，但 §8 将 `test_planner_forwards_base_url` 认定为“任何环境下确定性失败”，并把直接原因归为旧 Claude profile 被 pi-only 校验拒绝，这一判断不成立。

独立复现实验表明，5 个目标测试的真实状态是：

| 环境 | connectivity ×3 | planner base_url | tsc |
|---|---:|---:|---:|
| 当前操作环境（兼容变量 `DEEPSEEK_API_KEY` 已设置） | 通过 | 通过 | 通过 |
| 显式清空 `DSWARM_DEEPSEEK_API_KEY` 与 `DEEPSEEK_API_KEY` | 失败 ×3 | 失败 | 仓库本地 TS 5.9.3 通过 |
| 注入非真实占位 key，保持 LLMClient monkeypatch | 通过 ×3 | 通过 | 不适用 |

因此：

1. `docs/09` §10.7 记录的 `uv run pytest -q` exit 0 是一次真实、可复现的执行结果，不应称为无法复现；但该记录当时没有说明兼容环境变量 `DEEPSEEK_API_KEY` 已设置，环境条件记录不够完整。
2. 4 个 LLM 测试确实存在环境漂移，但共同直接原因是凭据预检，而不是返回内容语义或 pi-only profile 校验。
3. planner 测试中的 Claude fixture 是迁移残留，应改为合法 pi profile；但它不是当前 `DID NOT RAISE` 的充分或直接原因。
4. TypeScript 测试确实会受 PATH 中任意 `tsc` 版本影响；仓库本地 compiler 通过不代表 PATH 中 TS6 一定通过。

v3 的方案审批结论不受此处影响，但其 §8 必须修正；不能再以“5 个测试在当前代码下确定性失败”为实施基线。

### 11.2 LLM 测试的真实调用链

三个 connectivity 测试虽然 monkeypatch 了 `dswarm.core.llm.LLMClient`，但 `probe_reason_llm_endpoint()` 在构造 client 前会先执行 endpoint/credential resolution。没有 key 时，它直接返回 `missing_api_key`：

```text
resolve_reason_llm_endpoint
  -> has_api_key == false
  -> probe_reason_llm_endpoint 提前返回
  -> monkeypatched LLMClient 从未构造
```

所以：

- `test_llm_test_uses_request_body_base_url` 看不到被记录的 base URL/model；
- `test_llm_test_empty_content_still_ok` 根本没有进入 mocked chat；
- `test_llm_test_chat_raises_is_not_ok` 得到的是 missing-key detail，而不是 mocked `401 unauthorized`。

这些测试的目标是验证 request-body wiring、空 content 成功语义和异常映射，不是在测试凭据缺失。它们应在测试内部显式设置一个非真实占位 key，以保证被测路径不受开发机环境和 `.env` 影响；真实 key 不应参与这些单元测试。

### 11.3 planner 测试：旧 profile 与直接失败原因必须区分

`test_planner_forwards_base_url` 当前确实使用了迁移前夹具：

```text
engine=claude
transport=claude_code
```

在 pi-only 项目中这属于陈旧数据，应该迁移为合法的 pi profile，以保证测试输入符合生产契约。

但实测结果是：

- key 缺失时：`resolve_reason_llm_endpoint()` 返回 `has_api_key=false`，driver 不构造 `LLMClient`，所以 `_StopHere` 不会抛出；
- 注入占位 key 后：同一份旧 Claude fixture 下测试通过，`LLMClient.__aenter__()` 被调用并抛出 `_StopHere`。

这说明 `DID NOT RAISE` 的直接原因是 planner credential preflight 短路。旧 profile 是测试质量问题，但不能被写成“任何环境下确定性失败”的因果解释。

最小确定性修复应同时做两件事：

1. 测试内部设置非真实占位 key；
2. 将 worker fixture 迁移为合法 pi profile。

只改 profile 而不固定凭据，测试在无 key 环境仍会失败；只加 key 而保留 Claude profile，则测试虽然能过，但继续携带 pi-only 迁移残留。

### 11.4 TypeScript 测试：两个独立问题

当前 `find_tsc()` 的选择顺序是：

```text
TS_BIN -> PATH tsc -> repo-local apps/web/ui/node_modules/.bin/tsc
```

这使测试可能调用与项目无关的全局/其他工作区 compiler。当前仓库本地 TypeScript 版本是 5.9.3，现有 tsconfig 在该版本下通过；外部环境若把 TS6 放到 PATH，`baseUrl` 弃用可能触发 TS5101。

这里有两个独立修复点：

1. **测试工具确定性**：默认优先 repo-local pinned compiler；只有显式 `TS_BIN` 才覆盖。PATH 应作为最后 fallback，而不是优先于项目依赖。
2. **TS6 前向兼容**：删除 `baseUrl` 时，现有 `paths` 目标也要从 `types/...` 改为 `./types/...`，否则 TypeScript 5.9 会报 TS5090。已用临时配置验证以下组合可通过本地 `tsc --noEmit`：

```json
{
  "paths": {
    "@earendil-works/pi-coding-agent": ["./types/pi-coding-agent.d.ts"],
    "typebox": ["./types/typebox.d.ts"]
  }
}
```

所以不能只机械删除 `baseUrl`，也不建议仅用 `ignoreDeprecations` 掩盖问题。

### 11.5 对 v3 §8 的建议替换文本

建议将 v3 §8 的测试表述改为：

> 当前提交在带有 `DEEPSEEK_API_KEY` 且使用仓库本地 TypeScript 5.9.3 的环境中，`uv run pytest -q` 已验证 exit 0。独立无凭据环境暴露出 4 个 LLM 单元测试依赖 ambient credential；PATH 中外部 TS6 还会使扩展类型检查产生版本漂移。这些是测试确定性问题，而不是当前生产路径的 5 个稳定回归。实施任何内核方案前，应先让相关测试显式注入占位凭据、迁移 planner 的 pi-only fixture、优先选择 repo-local tsc，并完成 TS6-compatible tsconfig 调整。

建议删除以下不准确表述：

```text
任何环境下确定性失败
claude profile 被拒绝 -> roster 空 -> LLMClient 未构造
至少第 4 行是确定性失败
```

### 11.6 建议的最小修复范围

若进入代码修复，建议保持为一个独立、先于内核六批方案的测试基线提交：

1. `tests/test_connectivity_probes.py`
   - 三个 mocked LLM 测试显式设置占位 key。
2. `tests/test_llm_base_url_wiring.py`
   - planner 测试显式设置占位 key；
   - Claude worker fixture 迁移到合法 pi profile。
3. `docker/worker-pi/scripts/check_pi_extensions.py`
   - compiler 查找顺序改为 `TS_BIN -> repo-local -> PATH`。
4. `docker/worker-pi/pi-config/extensions/tsconfig.json`
   - 删除 `baseUrl`；
   - `paths` 目标改成 `./types/...`。

验收矩阵至少包括：

- 两个 DeepSeek key 均为空时，4 个 LLM 测试通过；
- shell 中存在真实/非真实 key 时结果相同；
- repo-local TypeScript 通过；
- PATH 中有其他 `tsc` 时仍优先使用 repo-local compiler；
- 显式 `TS_BIN` 仍可覆盖，便于 CI 做 TS6 前向兼容测试；
- 完整 `uv run pytest -q` 绿色。

---

## 12. v4 实现方案审查（2026-08-14）

审查对象：`docs/10-v4-kernel-improvement-implementation.md`。本轮不是对研究方向再次投票，
而是逐项核对当前工作区代码，判断 v4 是否已经达到“可以按文档直接实施”的接口闭合程度。

### 12.1 总体结论

**v4 可以作为后续升级的路线图，但不能按当前文本将 M1–M9 整体直接实施。** M0 已独立验证；
M1、M2、M4、M6 在修正文档列出的接口缺口后可以逐项实施；M3 与 M5 仍涉及事件源和唯一账本
的结构性问题，必须先完成设计修订；M7 只批准离线实验，不批准把 tie-break 接入生产派发；
M8 只批准 fixture/trace 实验，不构成 Advisor 上线解锁；M9 不能作为一个模块批准，必须拆成
独立 RFC/任务，其中两项在当前代码中已经基本实现。

| 模块 | Verdict | 实施条件 |
|---|---|---|
| M0 测试基线 | **Verified Complete** | 作为独立提交固化，继续保持空凭据全量绿色 |
| M1 priority | **Conditional Go** | 统一 float API、统一 SQL/Python 排序键、保持 FIFO、覆盖全部截断点 |
| M2 双 lane | **Conditional Go** | initial recon/verifier/0 容量/stop-cancel/单一 gate 所有权全部闭合 |
| M3 严格事件不可变 | **Design Review Required / High Risk** | 先补 canonical promotion/summary events、可重建投影、DB trigger 与全读取链路 |
| M4 direction diagnostics | **Conditional Go** | 修 resolution 枚举；registry 不接管动态 account/image；贯穿持久化与 UI 链路 |
| M5 token accounting | **Redesign before Go** | 在真实 provider/CLI charge 发生点生成 usage_id，并扩展唯一 CostController 账本 |
| M6 route lineage + telemetry | **Conditional Go** | 定稿 pheromone 时间语义、lineage conflict、durable cursor/retention 与增量聚合 |
| M7 energy | **Offline Conditional Go / Online No-Go** | 修公式与回放口径；在线接线必须在离线证据后单独审批 |
| M8 Advisor | **Experiment-only / Production No-Go** | 仅离线 fixture/trace；不得改生产 Reason prompt 或派发链路 |
| M9 OSS 遗产 | **Reject as bundled module** | 至少拆成 6 个独立任务/RFC；先剔除已实现项 |

因此，v4 顶部的实施顺序：

```text
M0 → M1 → M2 → M3 → ... → M9 可并行
```

当前不能作为批准后的执行 DAG。特别是 M3、M5、M9 仍是阻塞项，而 M7/M8 的“实验代码”与
“生产接线”也必须分成不同审批门。

### 12.2 M0 独立验证

本轮在以下变量都显式为空的环境中执行完整测试：

```powershell
$env:DSWARM_DEEPSEEK_API_KEY=''
$env:DEEPSEEK_API_KEY=''
uv run pytest -q
```

结果为 `exit code 0`，全量测试通过。四文件修复与 §11 建议一致：

- `tests/test_connectivity_probes.py`：mocked LLM 测试显式注入非真实占位 key；
- `tests/test_llm_base_url_wiring.py`：固定凭据前置并迁移合法 pi profile；
- `docker/worker-pi/scripts/check_pi_extensions.py`：`TS_BIN → repo-local → PATH`；
- `docker/worker-pi/pi-config/extensions/tsconfig.json`：删除 `baseUrl`，`paths` 改显式相对路径。

结论：M0 可以作为独立、先于内核改造的基线提交。不要把它与 M1 或更大迁移混在同一提交。

### 12.3 M1 priority：Conditional Go

v4 对两处主要 `int()` 截断的判断成立，但当前设计内部仍有排序契约矛盾：

1. 文档新增 `priority_scale`，却要求 SQL 继续只按 `priority DESC`。这样 operator/planner 的
   分层尺度不会真正参与调度。若保留该列，所有调度查询必须使用同一 scale-aware key，例如：

   ```sql
   ORDER BY
     CASE priority_scale WHEN 'operator' THEN 0 ELSE 1 END,
     priority DESC,
     created_seq ASC
   ```

2. `priority_sort_key()` 草案的最后一项是 `-created_seq`，会让新 Intent 优先，与当前
   `created_seq ASC` 的 FIFO 相反。升序排序时应保持：

   ```python
   (scale_rank, -priority, created_seq)
   ```

3. 不能只删除 `shared_graph.py::propose_intent` 和 `dispatchable_intents` 的两处截断；
   `swarm.py` 的调度读取仍有 `int(r.get("priority"))`，摘要格式化则应明确只是展示层。
4. operator Intent 的每个创建入口都必须显式写 `priority_scale='operator'`，不得以后通过
   `priority >= 50` 反推来源。
5. `priority_scale DEFAULT 'planner'` 可兼容旧库，但需要旧库 reopen + mixed-scale 排序测试。

修正以上事项后，M1 可作为第一个内核功能单独实施。

### 12.4 M2 双 lane：Conditional Go

双 semaphore 方向与 Reviewer reserved capacity 不变式一致，但“所有 worker 路径走 gate”尚未由
当前接口保证：

1. `ReasonSwarm.run()` 的 initial recon 在 `_one()` 外直接执行，只改 `_one()` 会继续绕过 gate。
2. verifier 当前通过 `worker_class="verifier"` 表达；`lane_for(mode)` 只识别 `mode="review"`，
   会把 verifier 静默放入 ordinary lane。必须定稿 verifier 与 review 共用 reserved lane，或建立
   独立 verifier lane，不能依赖偶然的 mode 文本。
3. `review_policy.get("max_concurrent") or 1` 会把合法配置 `0` 改成 `1`。新 gate 必须保留
   “0 表示禁用”的语义，并确定等待者收到何种拒绝结果。
4. semaphore 等待必须同时响应 task cancellation、`stop_event` 和 pause 状态；停止后不能留下
   永久等待容量的 worker coroutine。
5. classic `Swarm` 已有 `ReviewCapacityMixin`、`_active_review_tasks` 和 `_maybe_start_review()`。
   新 gate 必须成为唯一容量所有者，不能与旧 mixin 双重计数或双重拒绝。
6. 需要明确总量上限是：

   ```text
   ordinary_active <= max_workers
   review_active <= review_max_concurrent
   total_active <= max_workers + review_max_concurrent
   ```

   此时 `max_workers` 不再能被文档描述为全局总 worker 上限。
7. `acquire` 成功后才允许 `release`；active 计数与 snapshot 必须受同一并发契约保护。
8. 测试必须证明 classic Swarm 与 ReasonSwarm 注入的是同一个 gate 实例，而不是各自构造一把门。

修正后可在 M1 完成并全量绿色后，作为第二个独立功能实施。

### 12.5 M3 严格事件不可变：暂不批准实施

当前生产代码确有两处修改 canonical `events` 行：verification promotion 与 fact summary。
v4 提出的 `fact_verifications` / `fact_summaries` 旁表可以成为投影，但**仅增加旁表仍不能满足
“只从 immutable events 重建”**。

#### verification promotion 必须有 canonical event

当前 candidate 与后续 verified fact 发生 dedupe collision 时，旧实现通过 `UPDATE events SET verified=1`
提升原事件。如果只 UPSERT `fact_verifications` 而不追加新事件，删除投影后仅回放 `events` 无法知道
该 candidate 后来被谁、凭什么提升。必须先定义类似：

```text
fact_verified / fact_promoted
payload = {fact_seq, verifier, witness/source, verified, confidence}
```

的不可变事件，再由它重建 `fact_verifications`。

#### summary 也必须选边

EventBus 的 `NODE_SUMMARIZED` 不等于 SharedGraph canonical event。若 summary 要在 SharedGraph replay
后保留，则需追加 `fact_summarized` canonical event；若它只是可丢失缓存，则文档必须明确不把它列入
“events 可重建的事实状态”。不能一边只写旁表，一边声称从 canonical events 完整恢复 summary。

另外必须补齐：

- SQLite `BEFORE UPDATE/DELETE ON events -> RAISE` trigger，作为运行时硬保护；
- 全仓静态扫描，而不是只扫描 `shared_graph.py`，因为 blackboard skill 也直接访问数据库；
- projection rebuild API：可清空投影并从 canonical events 完整重建；
- `by_seq` 必须引用真实 canonical event seq；
- 全读取链路清单：`snapshot`、`verified_evidence`、reason summary、fact retention、BoardProjector、
  BTW、replay、API/UI；当前 `_summary_for_fact_seqs()` 等路径仍直接读取 event 行字段；
- 老数据库迁移边界：历史上已经被原位更新的值无法恢复其原始状态，只能将迁移时状态作为 genesis。

M3 接近项目 substrate，必须先把上述事件协议写成独立设计并再次评审，不能边写测试边临时决定语义。

### 12.6 M4 direction diagnostics：Conditional Go

逐 Intent 保存 raw/canonical/resolution 的方向正确，但 registry 权威边界需要缩小：

1. `canonical == raw.lower()` 应命名为 `explicit_canonical`，不能叫 `explicit_auto`。
2. `auto/any/unknown/unclear`、空值和非法值应分别记录为结构化 resolution，至少区分：
   `explicit_auto`、`empty`、`invalid`、`alias`、`explicit_canonical`、`mechanical_fallback`。
3. registry 只应权威管理 canonical id、aliases、fallback keywords 和默认 profile id；
   `credential_account` 与 image/runtime 是可由运行配置覆盖的动态事实，必须从实际 profile resolver 获取。
4. `raw_direction` 在 parse 边界就应限长、去控制字符，而不是等进 UI 才清理。
5. 字段必须贯穿 `Intent → DispatchDecision → intent_proposed event → intents projection →
   dispatchable_intents → UI/telemetry`，仅修改 dataclass 不足以形成诊断链。
6. mechanical fallback 只能处理 empty/auto/invalid，不能覆盖合法 canonical 或 alias。
7. 新 dataclass 字段放末尾并提供默认值，保护现有 positional fixture。

完成这些修订后，M4 可以实施。

### 12.7 M5 token accounting：必须重设计

当前项目已经存在真实结算链：

```text
CliSolver._charge_external_result()
  -> CostController.add_external_usd(...)
  -> global/challenge/solver ledger
  -> COST_UPDATE
```

因此在 `ReasonSwarm._one()` 结束时再调用 `record_worker_usage()`，会有把同一 provider usage
结算两次的高风险。`SolveOutcome.tokens` 可以是结果摘要，但不能成为第二次 charge 的依据。

必须改为：

```text
每次真实 CliResult/provider 调用完成
  -> 在该调用产生处生成 stable usage_id
  -> CostController.record_usage_once(...)
  -> 写入同一 durable COST_UPDATE/usage event stream
  -> global/challenge/solver/profile/account 投影
  -> ProfileBudgetGate 只读取投影，阻止未来 spawn
```

具体修订：

- usage_id 要标识一次可计费调用，而不是 `run_id + solver_id`；一个 solver 内可能有 initial、
  continuation、retry、respond 和 provider recovery 多次调用；
- 不新增 `sessions/<run>/usage.jsonl` 第二账本，复用 SessionStore 已持久化的事件流；
- unknown/estimated usage 是状态，不是一次 0-token charge；
- profile/account identity 在 spawn/charge 时已经可得，应随原始 charge 写入；
- gate 状态必须可由 durable ledger 重建，不能只靠 `_paused_profiles`；
- operator resume 必须定义 override、提高 cap 或 reset window，否则账本仍超限会立即再次暂停；
- 预算告警使用专用事件，不复用 provider 故障的 `PROVIDER_BATCH_ALERT`；
- 达到 cap 不强杀当前 worker，但阻止该 profile 的后续 spawn；stop/finalize 不得被预算门阻塞。

M5 在文档改成上述唯一账本架构前是 No-Go。

### 12.8 M6 route lineage + telemetry：Conditional Go

三级 lineage 与独立 metrics 载体方向可行，但需补齐以下契约：

1. inherited route 不能只查 `intent_products`；旧记录或异常路径可能仅在 fact payload 中保留
   `intent_id`。API 应有明确 fallback 顺序并记录 lineage reason。
2. explicit `payload.route_hash` 与 intent-inherited route 冲突时不能静默 explicit wins；应返回
   `explicit_conflict` 诊断并同时保留两值供审计。
3. `Finding.created_at` 当前直接参与 pheromone decay。如果 replay 时仍用 projection write time，
   即使新增 `event_ts`，同一事件的 pheromone 仍会随重放时刻变化。应明确拆分：

   ```text
   event_ts              canonical event time
   projected_at          materialization time
   pheromone_origin_ts   decay 使用的时间，通常等于 event_ts
   ```

4. `MetricsSink(run_root)` 必须对应现有 SessionStore/workspace 的真实目录契约，并定义 retention、
   最大尺寸/轮转、并发 append lock、partial-line 容错、durable cursor/checkpoint。
5. `aggregate_delta()` 不应每 30 秒重新扫描整个 JSONL，应维护可重放的增量 counter。
6. virtual replay 应消费已记录 event timestamp；不需要也不应为此改造整个生产 SharedGraph 的
   所有 `time.time()` 调用。
7. metrics 永远不能写回 evidence、扩大 Reason prompt 或影响 provenance。

修订后 M6 可在 M3 事件语义稳定后实施；其中纯 route-lineage API 可更早独立做，但不要和 metrics
持久化打成不可审查的大提交。

### 12.9 M7 energy：只批准离线实验

M7 不能按当前伪代码直接接入生产排序，离线部分也有四处必须修正的问题：

1. `@dataclass(frozen=True)` 中的 `weights: dict = {...}` 仍是共享可变默认值；应使用
   `field(default_factory=...)`，或暴露只读 Mapping。
2. dead-end 公式为负值：

   ```text
   -dead_penalty * exp(-age / dead_tau)
   ```

   同 route 多条“取 max”会选中**绝对值最小、通常最旧**的惩罚，从而忽略刚发生的强 dead-end。
   若只保留一条，应取最负值（`min`）；若组合多条，需定义 capped 合并并证明不会无限累加。
3. `energy_rank` 没有写清 tuple 的升降序。若 Python 升序排序，正 energy 直接放入 key 会让低
   energy 先出。建议明确为：

   ```python
   (lane_rank, -planner_priority, -energy_for_equal_priority, original_index)
   ```

   且只在**同 lane、priority 精确相等的组内**比较 energy。不同 priority 不应通过填 0 的方式
   进入同一个含糊排序表达式。
4. `actor_grouping=max` 只能抑制同 actor 回声，不能抑制多个 worker 转述同一 artifact/source 的
   correlated duplicate。分组身份至少要考虑 canonical fact identity、artifact/source lineage 和
   actor group，而不是只按 actor。

另外：

- `flag_found route -> 1.0` 不宜作为第一版在线信号。单 flag 任务已经结束；multi-flag 任务中，
  已出旗 route 可能反而应该降温，避免持续重派同一路径。先把它作为离线标签/终局指标，不进入
  在线 energy 公式。
- `route_energy(graph, ...)` 若直接读取 SQLite/SharedGraph，就不再是“纯函数、无 IO”。建议输入
  M6 产出的不可变 `RouteObservation[]`。
- 静态 trace 重排不能证明真实 counterfactual flag latency：调度顺序变化会改变并发、prompt 上下文、
  provider 状态和 worker 输出。replay 报告应标注为“调度重排估计”，上线证据仍需同 benchmark、
  同预算、多 seed 的离线真实运行。
- feature off 必须完全绕过 energy 计算与缓存，保证 decision-for-decision 和额外成本都等于 baseline。

Verdict：可以实现纯函数、fixture、replay 与 ablation 报告；`DSWARM_ENERGY_TIEBREAK` 生产接线
不属于本模块批准范围，必须等实验达标后单独提交在线 RFC。

### 12.10 M8 Advisor：仅实验，不构成解锁

当前 `subscribe_events()` 是 0.5 秒轮询生成器；ReasonSwarm 仍在每轮末尾等待：

```python
await asyncio.gather(*[_one(d) for d in fresh], return_exceptions=True)
```

所以 v4 的 suggestion 路径“不唤醒、不派发”，仍要等最慢 worker 完成后才进入下一轮 Reason。
它最多测试 suggestion 是否改善**下一轮规划内容**，不能改善当前生产链的真实 OODA 延迟。
“若存在唤醒机制的理论最小延迟”也不能作为 Advisor 上线证据。

建议把 M8 收缩为完全离线实验：

- 不在生产 SharedGraph schema 中增加 `suggestions` 表；实验数据写独立 fixture/sidecar DB；
- 不修改生产 `_run_reason()` 或 Reason summary；`## Open suggestions` 仅在实验 runner 中构造；
- suggestion lifecycle 若要重放，应记录 append-only 的 created/consumed/rejected 实验事件，而不是
  只更新 mutable status；
- baseline 与 suggestion 路径报告完整 trace、接受/拒绝理由和下一轮 intent 差异；
- 明确结论口径：测得的是“next-cycle planning quality/estimated lower bound”，不是已实现快路径；
- pause/stop/budget 的生产保证要等未来真正存在 watcher/wakeup/dispatch consumer 后再验收。

Verdict：批准离线 evidence collection；继续维持 production No-Go。只有未来设计出 watcher 所有权、
wakeup、取消 gather/等待策略、正式 Intent 转换门、budget/cooldown/cursor 后，才进入下一轮评审。

### 12.11 M9 OSS 遗产：拒绝捆绑实施

M9 同时包含安全门、网络范围治理、目标侧清理和三个运行时补丁，不符合 `AGENTS.md` 的
“One feature at a time”，也不能声称“可与 M4+ 并行”。应至少拆成以下独立任务：

1. Pentest Verified-PoC RFC；
2. scope policy/parser RFC；
3. runtime network observation/enforcement 方案；
4. typed cleanup registry RFC；
5. custom-endpoint runtime smoke probe；
6. 上游补丁差异核验/回合测试。

逐项问题如下。

#### Verified-PoC 门

`Reproduction{command, indicator}` 由 worker 自己提供，若 verifier 只检查 indicator 是否出现在
`_provenance_corpus`，命令可以直接 `echo <indicator>`，形成自证。还需定义：独立 verifier 身份、
目标绑定、PoC artifact hash、允许的 reproduction 结构、真实 tool-result witness、exit/status、
anti-echo/anti-laundering 规则，以及 finding severity 的标准 schema。该功能靠近 provenance substrate，
必须独立 RFC；不能以“复用 witness gate”一句带过，也不得修改 flag gate。

#### scope 事后审计

“扫描 provenance 文本中的 host 引用”不能证明真实网络行为：它会把文档、错误消息和历史记录误判为
越界，也会漏掉 DNS 解析、重定向、代理、编码 IP 和工具内部连接。并且 `canonicalize_lane()` 是危险
资源并发 lane 的 host/port 归一化，不是 scope whitelist parser。文本扫描最多只能产生 advisory
warning，不能据此改变 verified evidence。真正的 scope 保证需要结构化 scope 语法、DNS/IP/CIDR
解析和执行层网络 observation/enforcement；事后报告与运行时阻断要分开设计。

#### cleanup registry

让不可信 worker 写 `CLEANUP=<cmd>`，再由 coordinator 在 finalize 中执行任意命令，是高危 host RCE。
不应存在 raw command cleanup。安全版本必须使用 typed action allowlist，例如关闭本 run 所拥有的
listener/container/session、删除明确登记且位于允许根目录内的 artifact；并要求 ownership、作用域、
幂等键、超时、逆序、同 sandbox/container executor、无 host shell、stop/cancel 语义和可读报告。
失败可以不阻断 finalize，但不能吞掉审计记录。

#### “上游三补丁”不是三个都待实现

- UID/GID 探测已存在于 `container_exec.py::_query_worker_uid_gid()` / `_worker_uid_gid()`，并有
  `test_container_exec.py` 覆盖 image probe、override、recursive chown 与 symlink 安全；应先做差异核验，
  不要重复实现。
- web control plane 位于容器时，`resolve_worker_backend(..., in_web_container=True)` 已强制返回
  `container`，`runtime_env.py` 与 `test_runtime_env.py` 也明确禁止静默 host fallback；该项基本完成。
- custom endpoint 当前的 account probe 故意采用 model-agnostic direct endpoint；profile/provider health
  已用 `probe_endpoint(..., validate_model=True)`。`EndpointDriver._hello_argv()` 反而明确返回空列表，
  所以“EndpointDriver 已有 hello 机制”与代码不符。若仍需要验证 pi provider materialization/schema，
  应新增独立、显式花费额度的 `runtime_smoke` 深度，不要替换廉价 account/profile auth probe，也不要
  在每次设置页轮询时启动真实 CLI 回合。

### 12.12 修正后的依赖 DAG

建议采用以下审批和实施顺序：

```text
M0 独立提交并全量验证
  -> M1（修订后）独立实现 -> 专项 + 全量
  -> M2（修订后）独立实现 -> 专项 + 全量

M3 先写事件协议 RFC -> 再评审 -> 再实现
M4 可在 M1/M2 后独立实现
M5 先重写唯一账本设计 -> 再评审 -> 再实现
M6 lineage 子项可先做；event-time/replay/metrics 持久化等待 M3 语义稳定
M7 离线实验依赖 M1 + M6；在线 tie-break 另立 RFC
M8 离线 fixture 可独立研究；任何生产 watcher 依赖 M2 + M3 + M5 + M6
M9 拆包后逐项排期，Verified-PoC/scope/cleanup 均单独安全评审
```

### 12.13 推荐首个实施批次

当前最安全且符合“一次一个功能”的执行方式：

1. 先提交已经验证绿色的 M0 四文件修复；
2. 修改 `docs/10` 的 M1 排序契约后，只实施 M1；
3. 运行 priority 专项测试与 `uv run pytest -q`；
4. M1 评审通过后，再修改并实施 M2；
5. 在 M3 新事件协议获批前，不修改 canonical events 行语义；
6. 在 M5 唯一账本设计获批前，不新增任何 worker 结束后二次 token charge。

### 12.14 最终审批结论

对“是否可按 `docs/10-v4-kernel-improvement-implementation.md` 直接升级改造”的回答是：

- **可以按模块推进，但不能按当前 v4 全文直接连续实施。**
- **立即可做：**提交 M0；修订后实施 M1，再修订后实施 M2。
- **条件可做：**M4、M6 的独立子项。
- **设计阻塞：**M3、M5。
- **仅实验：**M7 offline、M8 fixture/trace。
- **拒绝捆绑：**M9；拆包后重新评审，且先删除已实现的重复项。

只有在 `docs/10` 吸收本节列出的修订后，它才适合作为后续实施计划的直接依据。
