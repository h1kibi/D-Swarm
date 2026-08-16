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

## M4 direction diagnostics（09 §10.3.4 / §10.5 direction routing 1-4）

**Status (2026-08-15): implemented and verified.** The deterministic registry,
parser diagnostics, scheduler fallback, event payload, SQLite projection, operator
telemetry, and compatibility tests are in place. The M4 implementation does not
change the provenance gate or the M3 immutable-event contract.

**Contract**

1. `DirectionRegistry` owns only stable direction vocabulary: canonical IDs,
   aliases, fallback keywords, and default profile IDs. It deliberately does not
   own image tags, credential accounts, endpoints, runtime selection, or other
   deployment facts; those remain in the existing profile/runtime resolvers.
2. `raw_direction` is sanitized at the parser boundary: control characters are
   removed, surrounding whitespace is trimmed, and the value is capped at 40
   characters before it reaches an event or UI.
3. `canonicalize()` uses these resolutions:
   - empty input → `empty`;
   - `auto`, `any`, `unknown`, or `unclear` → `explicit_auto`;
   - a canonical ID → `explicit_canonical`;
   - a registered alias (for example `reverse`) → `recognized_alias`;
   - any other non-empty value → `invalid`.
   A lower-case canonical direction is never classified as `explicit_auto`.
4. `_decisions_from_reason` applies mechanical fallback only to `empty`,
   `explicit_auto`, or `invalid`. A keyword match produces
   `mechanical_fallback`; without a match, the challenge category produces
   `category_fallback`. A valid canonical or alias result is never replaced by a
   keyword suggestion. Every fallback is retained in operator-visible telemetry
   as a `direction_override` delta.
5. Diagnostics travel through the complete chain:
   `Intent → DispatchDecision → intent_proposed payload → intents projection →
   dispatchable_intents()`. The event payload includes `direction`,
   `canonical_direction`, `raw_direction`, and `direction_resolution`.
6. New dataclass fields are appended with defaults so existing positional fixtures
   remain compatible. Programmatic legacy intents are canonicalized at the
   scheduler boundary, so `direction="reverse"` resolves to `rev` with
   `recognized_alias`.

**Implementation**

- `dswarm/solver/direction_rules.py`: typed static registry, canonicalization,
  sanitization, and deterministic keyword suggestions.
- `dswarm/solver/worker_profiles.py`: compatibility wrappers delegated to the
  registry.
- `dswarm/solver/reason.py`: parser-boundary raw/canonical diagnostics on each
  `Intent`.
- `dswarm/swarm/agents.py`: appended diagnostics on `DispatchDecision`.
- `dswarm/swarm/reason_scheduler.py`: canonicalization, permitted fallback,
  registration payload, operator delta, and scheduler telemetry.
- `dswarm/swarm/shared_graph.py`: intent schema/projection and dispatch exposure.

**Tests**: `tests/test_direction_diagnostics.py` covers registry resolution,
input sanitation, parser preservation, valid-direction precedence, fallback,
operator telemetry, event/projection parity, and legacy alias compatibility.
The M4 acceptance mapping is 09 §10.5 direction-routing items 1-4.

### M4.1 operator primary direction (CTF) — 操作员主方向选择

> **Status (2026-08-15): implemented and verified.** The finalized contract is implemented in the
> kernel, Web backend, and UI in that order. CTF-only direction handling is isolated from pentest/mock
> paths and does not touch the provenance gate. Deterministic backend, scheduler, recovery, and UI
> tests cover the contract.

**优先级链（定稿）**

```text
模型合法 canonical / alias  （复合题 per-intent 分流）
  > 操作员合法方向            （run 级主方向：initial recon + 模型方向空/非法时的兜底）
  > 机械关键词 fallback
  > category fallback
  > pi-worker
```

**五项已确认语义**

1. **值域**：操作员方向接受 canonical + 已注册 alias（`rev`/`reverse`/`ai_sec`/`ai-security`…），
   API 边界统一 canonicalize 后写入 `Challenge.direction`；非法值（`banana`、`../pwn`、
   控制字符、>40 字符）→ 置空 + 结构化 warning（`event=invalid_operator_direction,
   run_id, raw_direction, normalized_direction=""`），**不进入** Challenge/Intent/worker
   profile/黑板路由字段。
2. **`direction_source` 新字段**：`model|operator|keyword|category|default`，与
   `direction_resolution`（模型解析/回退状态，沿用已实现的
   empty/explicit_auto/explicit_canonical/recognized_alias/invalid/mechanical_fallback/
   category_fallback）**分离**——resolution 解释"模型说了什么、为何回退"，source 解释
   "最终派发为什么走这个方向"。示例：
   ```json
   {"raw_direction": "", "canonical_direction": "", "direction_resolution": "empty",
    "resolved_direction": "pwn", "direction_source": "operator"}
   ```
3. **命名**：`recognized_alias` 保持，不改 `alias`（与已实现契约/测试/事件兼容）。
4. **Category 与 Direction 两个值域分离**：Category 保持 Challenge Literal
   （web/pwn/**reverse**/crypto/forensics/misc）；Direction 用
   auto/web/pwn/rev/crypto/misc/forensics/aisec。**并修复存量 bug**：`LaunchForm.tsx` 的
   Category 下拉当前含 `"rev"`，而 `Challenge.category` Literal 不接受 `"rev"`，
   `drivers.py:352` 无归一化——从 UI 启动 rev 题会 Pydantic 校验失败。修复 = UI 改用
   `"reverse"` + 后端对存量 `"rev"`（历史 session 文件）做归一化兜底。
5. **恢复会话（/resolve）字段存在性语义**：请求**无** `direction` 字段 → 保留历史方向；
   `{"direction": ""}` → 显式恢复 auto（清除历史方向）；显式值 → 覆盖。**不得**用
   `ch.get("direction") or old`（会把显式 `""` 误判为未传）。

**initial recon 专属规则**：recon 是直接构造的 `DispatchDecision`，不走 Reason——方向 =
操作员合法方向 > category；**不经过关键词 fallback**（操作员已明确主方向）。recon 的
diagnostics：`direction_resolution` 保持解析语义，`direction_source = operator|category`。

**direction_override delta**（最终方向 ≠ 模型声明时写入黑板，操作员可见）：

```json
{"intent_id": "...", "raw_direction": "...", "model_canonical_direction": "",
 "operator_direction": "pwn", "resolved_direction": "pwn",
 "direction_source": "operator", "reason": "model_direction_empty"}
```

触发条件：模型方向为空/`auto`/非法而操作员方向生效；空/非法而关键词生效；category 兜底；
initial recon 用操作员方向覆盖 category。模型给合法方向 → 只记正常 diagnostics，不写 override。

**边界**：CTF 读 `challenge.direction`；pentest 不读不写不派发；mock 流不因新字段改变事件
序列；`gate.py` 与 anti-laundering 完全不动——方向是路由信息，永远不是 flag 证据。

**实施点**

- `dswarm/models/solve_graph.py`：`Challenge.direction: str = ""`（字段末尾，默认空）。
- `dswarm/swarm/reason_scheduler.py`：`_decisions_from_reason` 套用优先级链（模型合法 →
  操作员 → 关键词 → category → pi-worker）；initial recon 用操作员方向；`direction_source`
  贯穿 `DispatchDecision → propose_intent payload → dispatchable_intents`。
- `apps/web/drivers.py` / `apps/web/routes/runs.py`：边界 canonicalize + 非法置空 warning；
  `/resolve` 按存在性语义处理 direction。
- `apps/web/ui/components/LaunchForm.tsx`：新增 Direction 下拉（auto + 七方向，默认 auto；
  auto → 空串）+ Category 值域修复（`reverse`）。
- 测试：`test_direction_diagnostics.py` 扩展（操作员兜底、模型合法胜出、recon 来源、
  override delta、非法输入置空）；web 边界测试（alias canonicalize、存在性语义）；UI vitest。

---

## M5 token accounting 重设计（09 §10.3.5 / §10.5 token 1-6）

**状态更新（2026-08-15）**：§12.7 判定本模块 **Redesign before Go**；唯一账本契约
（[docs/14](14-m5-unique-ledger-rfc.md)）的 v1/v2 评审见
[docs/15](15-m5-unique-ledger-rfc-review.md) 与
[docs/16](16-m5-unique-ledger-rfc-v2-review.md)，v3 第三轮评审见
[docs/17](17-m5-unique-ledger-rfc-v3-review.md)，v4 第四轮评审见
[docs/18](18-m5-unique-ledger-rfc-v4-review.md)，v4.1 第五轮最终评审见
[docs/19](19-m5-unique-ledger-rfc-v4-1-review.md)。**RFC v4.1 已批准实施**（docs/14 当前版，
自包含实施规范）：完整恢复 per-worker token/bridge/互斥/预算/reconciliation，拆分
`call_outcome`/`usage_status`，统一 `UsageJournal`，定稿 checked durability、postflight fail-stop、
完整 canonical identity、per-worker exec-env token 接线与 `SpawnGuard`，并以测试矩阵 1–30
锁定。Phase 1–6 已按顺序完成，30 项验收矩阵已逐项核对并有确定性测试或全量回归证据。
不得新增二次计费路径或修改 provenance gate、anti-laundering、shared evidence graph 事实语义。**

**Phase 1 实施进展（2026-08-15）**：已完成 `SessionStore.append_checked`、`EventBus.emit_checked` 双算法、paired critical sink，以及 `RunManager.create()` / `_fresh_bus()` 重接线。确定性测试覆盖 checked failure 不可见、retry 新 seq/同 usage_id、真实 `flush+fsync`、non-critical failure 隔离、无 double-append 与 fresh-bus 保持；相关回归及全量 `uv run pytest -q` 均绿色。**Phase 1 验收完成。**

**Phase 2 实施进展（2026-08-15）**：已新增 run-scoped `UsageJournal`、不可变 `UsageCall` / `UsageRecord`、统一 `AccountingUnavailable` preflight 契约，并完成 started/finished 两阶段 `flush+fsync`、同路径并发写锁、started-only → `interrupted/unknown/None` 崩溃折叠与 canonical usage_id 幂等 reconcile。质检进一步锁定未知账户必须为 `None`、provider/fallback producer 与 usage status 互斥，以及损坏 started 行统一 fail-stop 为 `UsageJournalCorrupt`。测试矩阵 11–15、Phase 1/SessionStore/Web 相关回归及全量 `uv run pytest -q` 均以退出码 0 通过。**Phase 2 验收完成，允许进入 Phase 3。**

**Phase 3 实施进展（2026-08-15）**：已完成 `WorkerClaims` 与 ModelGateway per-worker token API（`issue_worker`、claims 查询、按 token/worker/run 分层撤销）及 1024 hard cap；同一 run 的 worker/review/recon/BTW token 不再互撤，入口 claims 为不可变快照，达到上限时拒绝新签发且不驱逐存量 token。普通 Swarm worker、review/recon worker、容器 BTW 均已注入各自 `DSWARM_TASK_TOKEN`，显式 endpoint profile 不签 gateway token；构造失败、worker runtime finally、run teardown 均有撤销保护。新增确定性测试覆盖 claims/env 对齐、兄弟 worker 隔离、scope、rollback、finally、显式 endpoint 和 run 清理；旧生产路径不再调用 legacy `issue()` / `revoke()` / `token_for_run()`。Phase 3 定向回归及全量 `uv run pytest -q` 均以退出码 0 通过。**Phase 3 验收完成，允许进入 Phase 4；本阶段未接入 usage producer、USAGE_RECORDED、ledger_state、SpawnGuard 或预算门。**

**Phase 4 实施进展（2026-08-15）**：已完成 producer wiring 与 invocation identity 接线：internal producer 通过 run-scoped `UsageWriter` 覆盖 Reason/Titler/Summarizer/BTW 等内部 LLM 调用，非 gateway 的 Pi/CLI 路径通过 fallback writer 记录 `invocation_aggregate`；`CliResult` 携带稳定 `invocation_id`，retry 不重复生成。ModelGateway、`LLMClient`、Titler、Summarizer、BTW 与 `Swarm` 的注入路径已完成确定性覆盖，未改变现有 `COST_UPDATE` 消费协议。Phase 4 专项回归 `tests/test_phase4_wiring.py` 与相关模型/事件/LLM/Web 测试均通过，**Phase 4 验收完成。**

**Phase 5 实施进展（2026-08-15）**：已完成 usage ledger 到预算执行点的接线：`USAGE_RECORDED` 经 `UsageLedger` 幂等折叠后进入 profile/account 双 `ProfileBudgetGate`，`_make_cli_worker()` 在真正创建 worker 前执行预算检查，预算拒绝不会增加 `_spawned_total`。`BUDGET_ACTION` 可重放恢复 blocker，`BUDGET_ALERT` 对 warn/cap 边沿去重；新增 `GET /api/runs/{run_id}/budget` 快照接口。Run 创建/重启恢复会在 run 注册后恢复预算配置，journal-only usage 会在 spawn 前 reconcile；journal reconcile 或 canonical append 失败会将 ledger/SpawnGuard 置为 `failed`，阻止后续 provider call 与 spawn，但 `stop`/`finalize` 仍可执行。新增 `tests/test_phase5_ledger.py` 17 项确定性测试，Phase 5 专项测试 `17 passed`，相关 Phase 4/EventBus/ModelGateway/LLM/Web 回归 100 项通过；随后全量 `uv run pytest -q --maxfail=1 --disable-warnings` 通过。**Phase 5 验收完成，允许进入 Phase 6。**

**Phase 6 实施进展（2026-08-16）：已完成预算可观测性与账本恢复闭环。** 新增 `POST /api/runs/{run_id}/budget/rebuild`，由 `RunManager.rebuild_ledger()` 统一执行 rebuilding → replay/reconcile → ready/failed 状态转换；重建失败会保留 `stop`/`finalize` 可用，并通过 503 与 UI 告警反馈。前端新增 `BudgetStatusPanel` 与 `budgetStatus` 纯函数投影，在 Inspector 中展示 global tokens/calls、unknown/estimated usage、profile/account 独立预算、blocked 状态和 ledger state；账本 failed 时可直接触发恢复。新增 404/503/recovery API 边界测试与前端投影测试。验证证据：`tests/test_phase5_ledger.py` 18 项通过、前端 Vitest 18 files/135 tests 通过、Next production build 编译与类型检查通过；全量 `uv run pytest -q --maxfail=1 --disable-warnings` 通过。**Phase 6 验收完成，M5 最终验收闭环。**



### M5 v4.1 最终验收矩阵（2026-08-16）

以下矩阵将 RFC §8 的 30 项要求映射到工作区中的确定性测试；“PASS”表示该契约已有
直接测试或由对应的端到端回归覆盖。M5 不改变 provenance gate、anti-laundering、
shared evidence graph 的事实语义。

| # | 结果 | 验收证据 |
|---:|:---:|---|
| 1 | PASS | `tests/test_phase5_ledger.py::test_usage_ledger_rebuilds_five_projections_idempotently`；`test_budget_gate_replays_duplicate_usage_and_actions_without_double_charge` |
| 2 | PASS | `tests/test_usage_journal.py::test_fallback_cannot_claim_provider_call_identity`；`test_usage_status_must_match_producer_contract`；gateway/internal/fallback wiring 回归 |
| 3 | PASS | `tests/test_llm.py::test_streaming_usage_writer_does_not_double_record_legacy_cost`；`tests/test_event_bus.py::test_sink_exception_does_not_block_fanout`；前端事件 reducer 回归 |
| 4 | PASS | `tests/test_modelgateway.py::test_per_worker_tokens_in_same_run_do_not_revoke_each_other`；`test_token_revoke_apis_are_scoped`；Swarm worker finally/teardown 测试 |
| 5 | PASS | `tests/test_modelgateway.py::test_gateway_journal_records_claims_and_canonical_event`；`tests/test_swarm.py::test_container_workers_receive_independent_claimed_tokens_and_release_only_self` |
| 6 | PASS | `tests/test_event_bus.py::test_emit_checked_failure_is_not_visible_and_retry_uses_new_seq` |
| 7 | PASS | 同上：retry 使用新 Event/new seq，`usage_id` 仍由 ledger 幂等折叠 |
| 8 | PASS | `tests/test_web_server.py::test_run_manager_checked_sink_survives_fresh_bus_without_double_append` |
| 9 | PASS | `tests/test_event_bus.py::test_append_checked_flushes_fsync_and_propagates_failure`；`tests/test_usage_journal.py::test_started_is_fsynced_before_mock_upstream_receives_request` |
| 10 | PASS | `tests/test_event_bus.py::test_paired_session_store_sink_avoids_double_append` |
| 11 | PASS | `tests/test_usage_journal.py::test_started_is_fsynced_before_mock_upstream_receives_request`；gateway 顺序测试 |
| 12 | PASS | `tests/test_usage_journal.py::test_started_only_reconciles_to_interrupted_unknown_without_zero_cost` |
| 13 | PASS | `tests/test_usage_journal.py::test_started_prewrite_failure_is_accounting_unavailable_before_upstream`；gateway fail-closed 测试 |
| 14 | PASS | `tests/test_usage_journal.py::test_concurrent_journal_instances_serialize_same_file_writes` |
| 15 | PASS | `tests/test_usage_journal.py::test_reconcile_is_idempotent_by_usage_id`；ledger reconcile 回归 |
| 16 | PASS | `tests/test_modelgateway.py::test_gateway_provider_error_gets_terminal_unknown`；`tests/test_llm.py::test_llm_usage_writer_records_provider_error_before_reraising` |
| 17 | PASS | `tests/test_phase5_ledger.py::test_ledger_reconcile_failure_blocks_spawn_but_allows_stop_finalize`；budget rebuild 503/ready 测试 |
| 18 | PASS | `tests/test_phase4_wiring.py::test_cli_result_exposes_invocation_id`；`test_run_cli_assigns_invocation_id`；fallback retry wiring |
| 19 | PASS | `tests/test_phase5_ledger.py::test_spawn_guard_blocks_failed_and_waits_for_rebuild`；Swarm budget rejection/stop/finalize 回归 |
| 20 | PASS | `tests/test_modelgateway.py::test_token_hard_cap_rejects_without_evicting_active_tokens` |
| 21 | PASS | `tests/test_modelgateway.py::test_gateway_call_keeps_entry_claims_after_token_revoke` |
| 22 | PASS | `tests/test_llm.py::test_llm_usage_writer_records_durable_success_and_canonical_event`；`test_llm_usage_writer_records_provider_error_before_reraising`；`test_llm_usage_writer_records_timeout_terminal`；Titler/Summarizer 注入测试 |
| 23 | PASS | `tests/test_web_server.py::test_btw_container_uses_independent_gateway_token_and_revokes_it` |
| 24 | PASS | `tests/test_phase5_ledger.py::test_usage_ledger_rebuilds_five_projections_idempotently`；replay/rebuild 回归 |
| 25 | PASS | `tests/test_phase5_ledger.py::test_profile_budget_alerts_are_edge_triggered`；`test_budget_action_is_durable_semantics` |
| 26 | PASS | `tests/test_phase5_ledger.py::test_budget_resume_without_explicit_action_does_not_clear_blocker`；无参数 resume 不解除预算 blocker |
| 27 | PASS | `tests/test_phase5_ledger.py::test_profile_budget_gate_has_independent_profile_and_account_blockers`；billing 维度 spawn 测试 |
| 28 | PASS | `tests/test_phase5_ledger.py::test_run_manager_budget_snapshot_endpoint` |
| 29 | PASS | `tests/test_phase5_ledger.py::test_run_manager_budget_rebuild_endpoint_restores_ready_state`；404/503 边界测试 |
| 30 | PASS | `uv run pytest -q --disable-warnings`、前端 Vitest、Next production build、`git diff --check`；provenance/gate 回归保持绿色 |

**最终状态：M5 Phase 1–6 施工完成，RFC v4.1 的 30 项验收矩阵 PASS。**


以下为被 RFC 取代的旧稿（保留作对照）：

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
