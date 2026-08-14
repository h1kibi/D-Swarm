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
