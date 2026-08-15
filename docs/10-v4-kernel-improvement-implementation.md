# D-Swarm 内核改进 v4 实现方案（实现级设计）

> 状态：**v4（2026-08-14）**。本文档是 [docs/08](08-oss-research-and-kernel-improvements.md)
> （v3 研究+决策）与 [docs/09](09-kernel-improvement-review-feedback.md)（两轮评审）的**实现层
> 配套**：把维护者认可有潜力的方案落成模块级、接口级设计。每条实现都标注对应的评审验收
> 条目（09 §10.5），并遵守两轮评审定下的修正（exact-equal tie-break、双 lane、严格事件行
> 不可变、逐 Intent diagnostics、三层账本身份、Advisor 先实验后实施）。
>
> **前置 M0（一切实现之前）**：修绿当前 5 个失败测试——`test_planner_forwards_base_url`
> 夹具迁移 pi profile（确定性失败，环境无关）；3 个 `test_llm_test_*` 显式
> `monkeypatch.setenv("DSWARM_DEEPSEEK_API_KEY", ...)`（消除环境漂移）；
> `pi-config/extensions/tsconfig.json` 加 `"ignoreDeprecations": "6.0"`。
>
> **M0 状态（2026-08-14 更新）：已完成，但按第三轮复评（docs/09 §11）修正后的方案执行**
> ——真实根因是 ambient credential 依赖与 `find_tsc` 的 PATH 优先顺序，修复限定 4 个文件
> （另含 `check_pi_extensions.py`），tsconfig 采用"删 `baseUrl` + `paths` 显式相对路径"
> 而非 `ignoreDeprecations`。两个 DeepSeek key 均为空时全量 `uv run pytest -q` 已通过
> （exit 0）。
>
> 实施顺序：M0 → M1 → M2 → M3（第一批，互有小依赖：M2 需要 M0 后测试干净）→ M4（第二批）
> → M5（第三批）→ M6（第四批）→ M7（第五批离线）→ M8（第六批实验）→ M9（OSS 遗产，
> 独立小项，可与 M4+ 并行）。

---

## M1 priority 精度与尺度契约（09 §10.3.1 / §10.5 基线 1-3；§12.3 修订）

**状态（2026-08-14）**：已按本节修订契约实施；M1 专项测试、受影响的 Reason/Swarm 测试和空凭据全量测试均通过。

**现状（已核实）**：`propose_intent`（shared_graph.py:2506-2513）与
`dispatchable_intents`（:3931）两处 `int()` 截断；`swarm.py::_open_intents`
仍有一次调度读取截断；摘要块还有展示层截断。operator 指令使用
`100/50/0/-10` 标签映射，planner 使用连续浮点，两套尺度当前混排在同一列。

**修订后的设计**

1. 新模块 `dswarm/swarm/priority.py`（纯函数，无 IO）：
   `normalize_priority(value) -> float` 负责标签、有限数值与非法值归一化；
   `priority_sort_key(scale, priority, created_seq)` 固定返回
   `(scale_rank, -priority, created_seq)`，其中 operator scale 恒在 planner scale 前，
   同优先级严格按 `created_seq ASC` 保持 FIFO。
2. `intents` 增加
   `priority_scale TEXT NOT NULL DEFAULT 'planner'`。显式合法 scale 优先；operator actor、
   operator directive/source 或标签式 priority 自动归为 `operator`；其余数值 priority
   归为 `planner`。旧库通过幂等 `ALTER TABLE` 获得默认 planner scale。
3. 不迁移 `priority` 列类型。删除写入、读取和 Swarm 调度投影中的 `int()` 截断，
   Python/API 统一返回有限 `float`；展示使用 `{priority:g}`，不得改变数值。
4. 所有会影响 Intent 调度或呈现顺序的查询使用同一尺度契约；在既有
   ordinary-before-review lane 约束内，排序固定为：
   `CASE priority_scale WHEN 'operator' THEN 0 ELSE 1 END, priority DESC, created_seq ASC`。
   不使用 `-created_seq`，不保留仅按 `priority DESC` 的混排路径。
5. SQLite INTEGER affinity 是否保存 REAL 由持久化探针测试保护；只有该探针在目标后端失败时
   才另立 schema migration，不采用 `×100` 作为默认迁移方案。

**测试**（`tests/test_priority.py` + `tests/test_shared_graph.py`）

- 纯函数：标签/数值/非法值归一化；operator/planner 分层；同 priority FIFO。
- `0.5/0.9` 持久化，关闭并重开 DB 后仍为 REAL，dispatch 返回原值且顺序稳定。
- operator scale 即使数值较低也排在 planner scale 前；ordinary/review 既有 lane 顺序不变。
- 旧库 INTEGER priority 自动补 planner scale，读取兼容。
- Reason summary 显示小数 priority，不再截断为 0。
- 验收映射：09 §10.5「priority 全链路浮点精度」「operator/planner 双尺度契约」。

---

## M2 双 lane 并发（09 §10.3.2 / §10.5 基线 4-7）

**状态（2026-08-14）**：已按 docs/09 §12.4 的 Conditional Go 修正条件实施。M2 专项、
受影响的 Reason/Swarm 回归及空凭据全量 `uv run pytest -q` 均已通过。

**实施前现状（已核实）**：`ReasonSwarm.run` 对 fresh decisions 直接 `gather`，上限仅
`max_intents_per_reason`；`max_workers` 只在 provider 告警里当统计数；review worker 走
ReasonSwarm 派发时没有容量门，classic Swarm 又由 `_active_review_tasks` 单独计数，存在多套
容量语义。既有测试 `test_review_worker_uses_reserved_capacity_when_ordinary_slots_full` 保护
"ordinary 满员时 reviewer 仍可启动"语义，不可破坏。

**实际实现**

1. 新增 `dswarm/swarm/lane_gate.py::WorkerLaneGate`，以两把 `asyncio.Semaphore` 作为 run-local
   唯一容量所有者：ordinary = `max_workers`，review = `review_max_concurrent`。接口定稿为：
   ```python
   WorkerLaneGate(max_workers: int, review_max_concurrent: int)
   lane_for(*, mode: str = "", worker_class: str = "") -> Literal["ordinary", "review"]
   await acquire(*, mode="", worker_class="", stop_event=None, pause_event=None) -> WorkerLane
   release(lane: WorkerLane) -> None
   snapshot() -> {"ordinary_active": int, "review_active": int}
   ```
   `worker_class in {"review", "verifier"}` 与 review/verify mode 共用 reserved review lane；
   其余 recon/explore/bootstrap/respond/fallback/recovery 归 ordinary lane。
2. `Swarm` 构造唯一 `_worker_lane_gate` 并注入 `ReasonSwarm`，不再各自维护 semaphore；
   `review_policy.max_concurrent=0` 原样保留并通过 `WorkerLaneDisabled` 明确拒绝，不使用 `or 1`。
3. `ReasonSwarm._run_worker()` 在 `worker_factory` 之前 acquire、在 `finally` release，因此 initial
   recon、fresh Reason intents、fallback、recovery 和 operator-directive decision 全部走同一 gate。
   `DispatchDecision` 保留 `Intent.worker_class`，避免 verifier 被静默归入 ordinary lane。
4. classic Swarm 的 direct review (`_maybe_start_review`) 与 operator spawn (`_apply_worker_cmds`)
   也在 worker 构造前 acquire。构造异常、worker 异常、正常结束、kill/cancel，以及 task 在第一次
   调度前被取消，均通过幂等释放闭包 + done callback 归还 permit。
5. `ReviewCapacityMixin` 只读取 gate snapshot；`_active_review_tasks` 仅保留任务生命周期注册用途，
   不再与 gate 双重计数。profile/account capacity 仍作为独立资格约束保留。
6. semaphore 等待同时响应 task cancellation、`stop_event` 和 pause gate。取消与 permit 同一事件循环
   turn 竞争时，会在清理任务 settled 后检查并归还“迟到成功”的 permit，防止隐性泄漏。

**并发契约**

```text
ordinary_active <= max_workers
review_active <= review_max_concurrent
total_active <= max_workers + review_max_concurrent
```

这里 `max_workers` 是 ordinary lane 上限，不再被描述为包含 reserved reviewer/verifier 的全局总上限。

**测试**

- `max_workers=2` + 4 个 ordinary decisions：峰值并发严格为 2。
- verifier/review 共用独立 lane；ordinary 满员时 reviewer 仍可启动。
- `review_max_concurrent=0` 明确禁用，不被提升为 1。
- pause 不提前启动等待 worker；stop 与 task cancellation 可中断等待。
- 普通取消、取消/permit 同 turn 竞态、worker/factory 异常和首次调度前取消均不泄漏。
- direct review、operator spawn、ReasonSwarm 多轮结束后 `snapshot()` 归零；同一批连续 operator spawn 各自绑定正确 worker 与 permit。
- classic Swarm 与 ReasonSwarm 使用同一 gate 实例。
- 验收映射：09 §10.5「三约束」「permit 释放不超发」与 §12.4 八项修正条件。

---

## M3 严格事件行不可变（09 §10.3.3 / §10.5 基线 8-10）

**状态（2026-08-14）**：已按 RFC v3 和批准的实现计划落地。`events` 现在由
`BEFORE UPDATE/DELETE` 触发器保护为 immutable event rows；`fact_verified` 与
`fact_summarized` 是 promotion/summary 的 canonical 状态事件；`fact_effective` 是
可从事件日志重建的统一 typed projection，包含 sticky retired、challenge+fact 双键绑定、
promotion provenance 和 summary 字段。旧的 `fact_reviews`、`fact_states`、`fact_merges`
表仅为兼容旧数据库/旧工具保留，M3 不再写入或读取它们。

已完成的实现面：

1. `dswarm/swarm/fact_events.py`：v2 schema contract、26 列 `fact_effective` VIEW、
   promotion/summary 唯一性、transition JSON/目标 guard、events immutable triggers、
   migration preflight、user_version 检查和 backup API。
2. `dswarm/swarm/shared_graph.py`：fresh DB 直接安装 contract；旧 DB 只能通过显式
   `migrate_to_v2(backup_path=...)` 安装；生命周期写路径只追加事件；所有有效事实读取
   走 `effective_fact(s)`。
3. `dswarm/swarm/projection.py`、`board.py`、`postgres_board.py`：base/promotion
   projection key、`replace_by_source` 幂等/替换协议、partial sync、失败不越 cursor、
   replay 不重复写入或发 delta；无旧 Finding 时不伪造 `supersedes_source_seq`。
4. `skills/dswarm-blackboard/blackboard.py`、worker 环境：challenge scope 显式传递，
   `read_facts`/`read_review` 只读 `fact_effective`；未迁移数据库不静默回退到 raw
   `events.verified`。`review_flow` 的候选计数 fallback 同样只读有效投影。

验证覆盖：M3 专项、SharedGraph、Board projector、blackboard skill、lifecycle wiring、
Reason/summary 回归和空凭据全量测试均纳入最终验证；Postgres 当前为 contract-level 测试，
尚未在本机执行真实 Postgres 集成测试。实现基线和 28 项测试矩阵仍见
[docs/11-m3-event-immutability-rfc.md](11-m3-event-immutability-rfc.md)。

发布注意：当前工作区代码已经是 v2 contract（`user_version=2`）的 phase-2 实现；向
GitHub 发布时应将数据库备份/显式迁移作为升级说明，不能宣称早于 phase-1 的旧二进制
能够自动拒绝新数据库。

---

## M4 direction diagnostics（09 §10.3.4 / §10.5 方向路由 1-4）

**现状（已核实）**：`parse_reason_reply` 直接 `canonical_direction(raw)`，非法值进
scheduler 前已成空串；`_decisions_from_reason` 拿不到原始词；方向规则散在
`worker_profiles.py`（映射表/镜像 tag/account id）与未来关键词表之间。

**设计**

1. 新模块 `dswarm/solver/direction_rules.py`——**单一权威 registry**（typed fields）：
   ```python
   @dataclass(frozen=True)
   class DirectionSpec:
       canonical: str            # web|pwn|rev|crypto|misc|forensics|aisec
       profile: str              # pi-web ...
       image_tag: str
       account_id: str
       aliases: tuple[str, ...]  # ("reverse",) for rev
       keywords: tuple[str, ...] # ("rsa","factor","discrete log",...) for crypto
   class DirectionRegistry:
       def canonicalize(self, raw: str) -> tuple[str, str]   # (canonical, resolution)
       def suggest(self, goal: str, brief: str) -> tuple[str, str] | None  # 关键词高置信
       def profile_for(self, canonical: str) -> str
       def image_for(self, canonical: str) -> str
   ```
   `worker_profiles.py` 的 `DIRECTION_PROFILE`/`canonical_direction`/`direction_image` 改为
   委托 registry（导出函数保留，调用方零改动）。
2. `Intent` 增字段（09 §10.3.4 逐 Intent 建模）：
   ```python
   raw_direction: str = ""          # 进事件/UI 前限长 40
   direction_resolution: str = ""   # empty|explicit_auto|recognized_alias|invalid|
                                    #   mechanical_fallback|category_fallback
   ```
   `parse_reason_reply` 填充：raw 空→`empty`；canonical==raw.lower()→`explicit_auto`；
   别名命中→`recognized_alias`；canonical 空且 raw 非空→`invalid`。
3. `_decisions_from_reason`：canonical 空或 `invalid` 时调 `registry.suggest(goal, brief)`
   → 命中标 `mechanical_fallback`；仍空 → category → `category_fallback`。**合法模型输出
   永不覆盖**（只记 telemetry）。
4. `propose_intent` payload 携带 `raw_direction/direction_resolution`；
   `dispatchable_intents` 返回；fallback 覆盖发生时 `_emit_bb("direction_override", ...)`
   发操作员可见 delta。

**测试**：混合多 intent（crypto 合法 / "reversing" 非法 / 空）各自 diagnostics 正确；
"reversing"→invalid+raw 保留+mechanical_fallback 生效；"extract RSA key from binary"
（crypto 关键词命中但模型已给合法 rev）→ 模型值保留；decision→profile→engine→runtime
全链路（扩展既有 `test_reason_decisions_route_direction_to_profile`）。

**验收映射**：09 §10.5 方向路由全部 4 条。

---

## M5 token accounting 重设计（09 §10.3.5 / §10.5 token 1-6）

**现状（已核实）**：`SolveOutcome` 无 token 字段；`CliSolver._tokens_spent()` 存在但只用于
生命周期事件；`ReasonSwarm._one` 结算 `tokens=0`；`MemoryBoard.charge_agent` 仅累计+warned；
`_budget_exhausted` 只查 challenge global。"per-agent 软警告→硬上限→暂停派发"状态机**不存在**。

**设计**

1. **数据模型**（types.py）：
   ```python
   # SolveOutcome 增
   tokens: int = 0
   tokens_unknown: bool = False
   ```
   `CliSolver.run()` finally 捕获本实例 delta（结束时 `_tokens_spent()` − 进场快照）；
   CliResult 无 usage 报告时 `tokens_unknown=True`。
2. **唯一账本**（core/cost.py 扩展）：
   ```python
   async def record_worker_usage(*, usage_id: str, instance_id: str,
                                 profile_id: str, account_id: str,
                                 input_tokens: int, output_tokens: int,
                                 unknown: bool) -> bool
       # usage_id 幂等：内存 UNIQUE 集 + 持久化 ledger（sessions/<run>/usage.jsonl，
       #   与 SessionStore 同 sink 风格）；重复调用返回 False 且不计费
   def profile_usage(profile_id) -> Ledger
   def account_usage(account_id) -> Ledger
   ```
   - `usage_id = f"usage::{run_id}::{solver_id}"`：solver_id 每次 spawn 唯一 → 单实例
     生命周期归属；重放/重启重复提交同 id 被幂等吸收。
   - retry/recovery 新 solver_id → 新 usage_id（不重复计费），但 **profile/account 维度
     累计跨实例**（这是 v2 设计被否决点的修复）。
3. **三层预算** `dswarm/swarm/budget.py`（已有文件，改写）：
   ```python
   class ProfileBudgetGate:
       def charge(self, usage) -> ChargeVerdict   # ok | warn | cap_exceeded
       # warn_at 80% → 发 PROVIDER_BATCH_ALERT 同风格 delta
       # cap → self._paused_profiles.add(profile_id)（复用既有暂停派发机制）
       # operator resume 清除；状态机测试确定性驱动
   ```
   `_one` 结算改为 `record_worker_usage`；`MemoryBoard.charge_agent` 改为**投影读取**
   CostController（不再自己累计）；`COST_UPDATE` payload 增 `unknown/estimated` 标记。
4. UI：worker 设置页预算显示挂 CostController 投影。

**测试**：同 usage_id 重放计一次；新实例同 profile 累计增长；unknown 展示为
`unknown/estimated` 且不记 0；软警告→硬上限→暂停→恢复全状态机；recon/worker/retry/
recovery 各结算恰好一次；CostController/Board/UI 投影一致。

**验收映射**：09 §10.5 token 与预算全部 6 条。

---

## M6 route lineage + telemetry（09 §10.3.6 / §10.5 route 1-4）

**现状（已核实）**：`BoardProjector.project_event` 只投影 `fact_added`；`Finding` 无
`route_hash`、`created_at` 是投影时刻；`MemoryBoard(now=...)` 已支持虚拟时钟。

**设计**

1. **lineage**（shared_graph.py 增只读 API）：
   ```python
   def route_lineage_for_fact(self, fact_seq: int) -> dict:
       # {"route_hash": str, "lineage": "explicit|inherited|unattributed",
       #  "reason": "payload_route_hash|intent_inherit:<intent_id>|no_intent|...",
       #  "intent_id": str}
   ```
   优先级：payload.route_hash（explicit）→ intent_products→intents.route_hash
   （inherited）→ 无（unattributed）。route-less 原因结构化枚举。
2. **Finding 扩展**（board.py）：`route_hash/route_lineage/event_ts/projected_at`；
   `BoardProjector` 投影时填 `event_ts=events.ts`（原始事件时间）与 `projected_at=now`。
3. **独立 telemetry 载体** `dswarm/swarm/metrics.py`：
   ```python
   class MetricsSink:
       def __init__(self, run_root: Path) -> None      # metrics/usage.jsonl append-only
       def record_fact_write(self, actor, verified, route_lineage) -> None
       def record_dedupe_hit(self, actor) -> None
       def record_summary_size(self, n_chars) -> None
       def aggregate_delta(self) -> dict               # 每 30s 一次低频聚合
   ```
   UI 只收 `metrics_summary` 低频 delta；**不写 evidence graph、不进 Reason prompt**。
4. **virtual time replay**：`ReplayClock` 注入 Board/Projector；benchmark replay 固定
   virtual 时间，两次重放结果一致。

**测试**：三类 lineage 区分与原因枚举；投影保留 event_ts；virtual time 重放两次一致；
metrics 不产生任何 graph 事件；durable consumer 从 checkpoint 恢复不重复派生。

**验收映射**：09 §10.5 route 与 telemetry 全部 4 条。

---

## M7 energy 离线实验（09 §10.3.7 / §10.5 energy 1-8）

**现状（已核实）**：信息素数学在 `board.py`；无 route 级聚合；无 replay harness；
`MemoryBoard` 已有虚拟时钟注入点。

**设计**

1. `dswarm/swarm/energy.py`（纯函数，无 IO、无 LLM）：
   ```python
   @dataclass(frozen=True)
   class EnergyConfig:
       weights: dict[str, float] = {"verified_witness": 1.0, "verified": 0.6, "candidate": 0.3}
       tau: float = 1800.0
       dead_penalty: float = 0.5
       dead_tau: float = 7200.0
       actor_grouping: str = "max"     # 组内聚合 max|sum
       exclude_housekeeping: bool = True   # verifier/review route 不参与
   def route_energy(graph, now, cfg) -> dict[str, float]
       # 1) identity dedupe（复用 _normalize_fact_identity）
       # 2) 每项 clamp [0,1]
       # 3) actor 分组：组内 max；跨组概率合并 1 - Π(1 - x)
       # 4) dead-end：-dead_penalty * exp(-age/dead_tau)，同 route 多条取 max
       # 5) flag_found route → 1.0
   def energy_rank(decisions, energies, original_order) -> list
       # key = (lane, planner_priority, energy if priority exactly equal else 0.0,
       #        original_index)   ← exact-equal tie-break 唯一在线语义
   ```
2. `scripts/energy_ablation.py`：读 `shared_graph.db` 或 JSONL，`ReplayClock` 虚拟时间，
   在每个 decision 时刻计算 energy vs baseline 顺序，输出 CSV + 报告（flag latency、
   worker starts、tokens、route churn、多 seed、置信区间）。`DSWARM_ENERGY_TIEBREAK=1`
   作为**离线验证通过后**的在线开关（仅 exact-equal，默认 0 = feature off 与 baseline
   decision-for-decision 一致）。
3. 复杂度：energy 增量缓存（按 fact_seq 失效），禁止每 tick 全量扫；基准测试记录。

**测试**：纯函数单测（clamp/去相关/冷启动恒等=baseline 原序/稳定排序/dead-end 随时间减弱）；
harness 对固定 fixture 两次运行输出逐字节一致。

**验收映射**：09 §10.5 energy 全部 8 条。

---

## M8 Advisor 解锁实验（09 §10.3.8；第六批维持 No-Go，本模块只做证据收集）

**现状（已核实）**：`shared_graph.subscribe_events` 存在但仅轮询；ReasonSwarm 的 `gather`
屏障意味着"flag 落地 → 下一轮 Reason"至少等本轮最慢 worker。评审要求先给消费协议 +
延迟 trace，再谈实施。

**设计（不生产化）**

1. `dswarm/swarm/suggestions.py`——suggestion 侧表（**非 intents**）：
   ```sql
   CREATE TABLE suggestions (
     suggestion_id TEXT PRIMARY KEY,
     source_event_seq INTEGER NOT NULL UNIQUE,   -- 幂等：同一源事件唯一
     kind TEXT NOT NULL,                          -- flag_scout|...
     payload TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'open',         -- open|consumed|rejected
     reason TEXT, created_seq INTEGER)
   ```
2. `scripts/advisor_latency_exp.py`：同一 fixture 跑两条路径——
   - baseline：现网行为，记录 `time(flag_found → next focused dispatch)`；
   - suggestion：flag 事件时写 open suggestion（不唤醒、不派发），Reason 下一轮摘要增
     `## Open suggestions` block，Reason 决定 convert（→propose_intent）或 reject（记
     reason）；测量延迟差，并计算"若存在唤醒机制"的理论最小延迟。
   输出完整 trace + 接受/拒绝原因，作为第六批解锁与否的证据。
3. 生命周期：convert 前检查 pause/stop/budget；`pause/stop` 后绝不产生新 spawn；
   durable cursor 用 `source_event_seq`，进程重启后从 checkpoint 续。

**测试**：同 source_event_seq 重放不重复；rejected 不生成 intent；pause/stop 零 spawn；
cursor 恢复幂等。

**验收映射**：09 §10.5 Advisor 全部 5 条（以实验产物形式）。

---

## M9 OSS 遗产四项（08 §5.8；独立小项，可与 M4+ 并行）

| 项 | 实现要点 | 测试 |
|---|---|---|
| **Verified-PoC 门**（pentest） | `cli_solver._handle_poc_save` 扩展：POC 记录 `Reproduction{command, indicator}`；review_flow 对高严重度 finding 生成 verifier intent；verifier 重跑后 indicator 必须出现在 `_provenance_corpus` 才把 finding 置 verified（复用 witness gate 模式，不新增可信度语义） | test_pentest_mode.py：无复现证据的 finding 不得 verified；indicator 命中即 verified |
| **scope 事后审计** | `dswarm/swarm/scope_audit.py`：解析 `Challenge.scope` 白名单（复用 canonicalize_lane 的 host 归一化），扫描 provenance corpus 检 out-of-scope 引用 → `EV_REVIEW_FINDING(kind="scope_violation")` + 报告排除 + HITL 提示 | 白名单命中/越界/无 scope 三态；violation 不进 verified 集合 |
| **cleanup registry** | blackboard skill 增 `CLEANUP=<cmd>` 标记 → `cleanup_actions` 表（intent 关联）；wind-down（`_finalize_coordinator_run`）逆序执行 + 报告清单 | 逆序执行；失败不阻断 finalize；清单可读 |
| **上游合并补丁** | ① custom-endpoint 健康检查跑真实 CLI 回合（0.2.4 语义，EndpointDriver 已有 hello 机制，补齐 schema 错误预检）；② worker 镜像 UID/GID 探测后 chown（0.2.5）；③ 容器内强制 container backend、拒绝静默回退 host | 各自独立确定性测试；全量回归 |

---

## 总纲：每模块完成后

1. 专项测试绿 → `uv run pytest -q` 全量绿（M0 后基线为 0 失败）。
2. 验收对照：逐条勾选 09 §10.5 对应条目，未覆盖项显式说明原因。
3. 不变式回归：provenance gate 测试、事件不可变 guard（M3 起）、append-only replay
   全部保持绿；任何模块不得修改 `gate.py` 的判定逻辑。
4. feature flag 纪律：M7 的 tie-break、M8 的 suggestion 注入均有开关，关闭 = 现网行为。
