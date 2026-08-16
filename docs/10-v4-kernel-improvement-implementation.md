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

**Status (2026-08-16): M6a-1 / M6a-2 / M6b-1 / M6b-2 implemented and test-backed; M6 complete.**
首轮复评审定 **Contract v1 reviewed — Revise before Go**（M6a 三阻断：多 intent
lineage、orphan 禁止继承、短路解析无法冲突检测；M6b 四缺口：路径未选边、无 durable
checkpoint、记录模型不足、埋点语义未定义）。Contract v2 已按评审意见逐条修订；实施顺序 =
M6a-1 → M6a-2 → M6b-1 → M6b-2，每步独立提交、全量绿。已核实的关键代码事实：
`intent_products` 主键为 `(intent_id, fact_seq)`（一个 fact 可关联多个 intent）；
`add_evidence` 对不存在的 intent 写 `orphan_intent_id` 且**丢弃边**（注释
"drop the edge"）；`PostgresBoard` 无 M6 字段（`created_at` 由 `NOW()` 生成）；UI reducer
会把非空 actor 加进 worker 列表（events.ts:890-895）；真实 workspace 为
`sessions/<safe_run_id>/workspace`。

## M6a — lineage（两阶段 resolver + 双 Board 持久化）

### M6a-1 RouteObservation 模型与 resolver（纯解析，不动 Board）

```python
@dataclass(frozen=True)
class IntentRouteRef:
    intent_id: str
    route_hash: str

@dataclass(frozen=True)
class RouteObservation:
    fact_seq: int
    event_ts: float
    explicit_route_hash: str = ""
    inherited_routes: tuple[IntentRouteRef, ...] = ()
    effective_route_hash: str = ""
    lineage: str = "unattributed"      # explicit|inherited|unattributed|
                                       #   explicit_conflict|inherited_conflict
    reason: str = ""
    attempted_orphan_intent_id: str = ""
    attempted_orphan_route_hash: str = ""   # 仅供审计
    eligible_for_energy: bool = False
```

**两阶段算法**（`shared_graph.py` 增
`route_observations(fact_seqs) -> dict[int, RouteObservation]` 与单条
`route_lineage_for_fact` 包装）：

1. **独立采集**（互不短路）：
   - explicit：`fact_added.payload.route_hash`（比较前使用现有 route normalization）；
   - inherited（canonical 边）：`intent_products → intents.route_hash`，稳定排序
     `ORDER BY intents.created_seq, intents.intent_id`（**禁止依赖无 ORDER BY 的返回顺序**）；
   - inherited（旧记录兜底）：仅当 canonical product edge 完全缺失时，
     `payload.intent_id → intents.route_hash`（reason=`payload_intent_inherit`）；
   - orphan 审计：`payload.orphan_intent_id` → **只记录 attempted_orphan_*，绝不产生
     inherited、绝不进入 energy 输入、绝不覆盖 explicit**
     （reason=`orphan_intent_reference`）。
2. **统一裁决**（比较前对 route_hash 归一化，防大小写/格式假冲突）：

| 情况 | lineage | effective_route_hash | eligible_for_energy |
|---|---|---|---|
| 无 explicit，继承得唯一 route（含多 intent 同 route） | inherited | 该 route | true |
| 多 intent 继承不同 route | inherited_conflict | `""` | **false**（不得随意挑一个给 M7） |
| explicit 与全部 inherited 一致 | explicit | explicit | true |
| explicit 与任一 inherited 不同 | explicit_conflict | explicit（保留，另存 inherited 全量） | false |
| 无可信 route | unattributed | `""` | false |

M6a-1 的实现必须保留全部 `IntentRouteRef`；orphan 引用即使后来出现同名 intent，也只能作为
`attempted_orphan_*` 审计信息，不能复活已被内核主动丢弃的 lineage 边。

### M6a-2 Finding 时间三拆 + 双 Board 持久化

```python
# Finding 增（board.py）
route_hash: str = ""
route_lineage: str = ""
event_ts: float | None = None
projected_at: float | None = None
pheromone_origin_ts: float | None = None
fact_origin_ts: float | None = None   # promotion 时保留原事实时间供审计
```

- `Finding.pheromone()` 用 `pheromone_origin_ts or created_at`（旧投影兼容）；
- **promotion 时间语义选边**：`fact_verified` replacement Finding 的
  `event_ts = pheromone_origin_ts = fact_verified.ts`（验证提升**刷新** pheromone 年龄），
  原事实时间存 `fact_origin_ts = fact_added.ts` 供审计；base 投影用 `fact_added.ts`；
- lineage 数据源统一走 M3 `fact_effective` 折叠，不另建事实 JOIN 模型；
- **双 Board 完整持久化**：`MemoryBoard.write_finding/replace_by_source` 与
  `PostgresBoard`（schema 迁移、`_finding_columns`、`_finding_from_row`、`write_finding`、
  `replace_by_source`）全部保留新增字段；`projection.py` base/promotion 两条投影路径填
  event_ts 与 lineage；新增 MemoryBoard/PostgresBoard 一致性测试。

实现记录（2026-08-16）：上述 Finding 六字段、旧投影衰减回退、MemoryBoard replacement、
Postgres schema/row/write/replace 契约以及 base/promotion/cold-replay 投影测试均已落地；
`fact_verified` 以 promotion event timestamp 刷新 pheromone，`fact_origin_ts` 保留 genesis
事实时间。M6b sidecar telemetry、UI 与 ReplayClock harness 仍未开始。

## M6b — telemetry（sidecar，非 route 事实源）

### M6b-1 MetricsSink 独立持久化（先不接生产埋点）

**路径定稿**：`run_root = RunManager.workspace_dir(run_id)` →
`sessions/<safe_run_id>/workspace/metrics/route-telemetry.jsonl`
（与证据图并列但不是 evidence；避开 M5 `usage` 账本语义）。

**轮转 + retention**：单文件写锁；current 达到 5MB 时按
flush → close → 高编号向低编号 `os.replace` → 新建 current 的顺序轮转；
`retention_generations = 3`（`.1..3`，超代删除）。尾部 partial line 容错读取，不能让指标损坏
反向阻断 graph、worker 或 projector。

**durable checkpoint**：`metrics/route-telemetry.checkpoint.json`：

```json
{"schema_version": 1, "last_record_id": "...", "last_record_seq": 123,
 "counters": {}, "partial_lines_ignored": 0}
```

checkpoint 原子写（tmp + flush + fsync + `os.replace`）；重启时加载 checkpoint，只扫描保留
文件中的增量记录，按 `record_id` 去重，恢复内存 counter。`aggregate_delta()` 只消费内存
增量 counter，**不得周期性重扫 JSONL**。

**记录 schema**：

```json
{"schema_version": 1, "record_id": "fact_write:123:fact:45:base", "record_seq": 42,
 "kind": "fact_appended", "run_id": "run-0001", "challenge_id": "chal",
 "event_ts": 1755300000.0, "observed_at": 1755300000.2, "actor": "cli-pi-2",
 "fact_seq": 45, "route_hash": "route-abc", "route_lineage": "inherited",
 "lineage_reason": "intent_product", "intent_ids": ["I-a", "I-b"], "verified": true}
```

**事实源边界**：M7 的 route replay 读取由 immutable graph 构建的
`RouteObservation[]`；MetricsSink 只是性能/运行统计 sidecar，**不是 route 事实源**
（"metrics 不是 evidence"）。

实施记录（2026-08-16）：`dswarm/swarm/route_telemetry.py` 已落地独立
`RouteMetricRecord`/`MetricsSink`；路径、schema、sink-owned `record_seq`、进程内单写锁、
5MB 轮转、3 代 retention、原子 checkpoint、重启增量 reconcile、`record_id` 去重与尾部
partial-line 截断修复均有确定性测试。`aggregate_delta()` 只消费内存增量并在成功写入
checkpoint 后清空；本阶段没有接入 graph/projector/worker/UI/Reason/gate，也没有生产埋点。

### M6b-2 埋点、聚合与 UI

| kind | 触发点（best-effort，绝不 raise 进 graph/worker/projector） |
|---|---|
| `fact_appended` | `add_evidence()` 成功 append 且 `seq > 0` 之后 |
| `dedupe_hit` | dedupe 使 `add_evidence()` 返回 `-1` 的确定分支 |
| `summary_recorded` | `record_fact_summary()` 首次成功返回 `True` 之后 |
| `fact_projected` | `BoardProjector.replace_by_source()` 结果非 `ALREADY_APPLIED` 之后 |
| `fact_promoted` | `fact_verified` replacement 成功投影之后 |

**BLACKBOARD_DELTA 边界**：`metrics_summary` delta 用**空 actor**（防 UI 把 `metrics`
当 worker）；不创建 fact/intent/graph 节点、不写黑板 timeline；**Reason prompt 构造逐字节
不变**。准确表述：零 SharedGraph canonical event，允许产生非证据性 SessionStore/UI
telemetry event。新增 replay 测试，确保历史 metrics summary 不污染 worker 列表。

**ReplayClock 契约**：

```python
class ReplayClock:
    def set(self, ts: float) -> None
    def now(self) -> float
```

离线 harness：从 immutable graph 构造 `RouteObservation[]` → 按 `(event_ts, fact_seq)`
稳定排序 → 逐条 `ReplayClock.set` → `MemoryBoard` 用 `ReplayClock.now` →
`projected_at` 用注入 clock → pheromone 恒用 `pheromone_origin_ts=event_ts` → 两次 replay
产物完全一致。**仅 Board/Projector 注入可选 clock**；生产默认真实 clock；不改造
SharedGraph 其余 `time.time()`；不要求 Postgres 虚拟 DB clock。

实现记录（2026-08-16）：五类生产埋点已接入 SharedGraph 与 BoardProjector；真实 Web
运行使用 `workspace/metrics/route-telemetry.jsonl`，standby reopen 同样恢复 sidecar，任何
目录、append 或 aggregate 故障均被隔离，不能阻塞 canonical graph 或 projection。每次实际
dedupe collision 使用独立 `record_id`，避免同一事实的多次命中被错误折叠。
`metrics_summary` 仅通过 EventBus/SessionStore 作为非证据事件发出，actor 为空；Web reducer
将其隔离在 `blackboard.routeMetrics`，不创建 worker、timeline、solver、fact、intent 或 graph
节点。`route_replay.py` 已提供只读 immutable graph、忽略 telemetry sidecar 的
`ReplayClock` 确定性 harness。红线回归覆盖 metrics 开/关时 canonical events、
`to_reason_summary()` 与 Reason prompt 完全一致，且 provenance gate 未改动。

验证记录（2026-08-16）：`uv run pytest -q` exit 0；Web UI `npm test` 为 18 files /
136 tests 全绿；`npx tsc --noEmit` exit 0；`npm run build` 完成 production build。

### M6 红线

- metrics 零 SharedGraph canonical event；
- metrics 零 Reason prompt 影响；
- metrics 零 provenance / flag gate 影响；
- M7 只接收 `eligible_for_energy=True` 的 RouteObservation，不能从 sidecar metrics 反推 route。

## 验收测试（24 项）

M6a：1 多 intent 同 route→inherited 保留全部 ID；2 多 intent 异 route→inherited_conflict；
3 explicit 与多 inherited 冲突保留全部值；4 查询/边插入顺序变化不改变 observation；
5 orphan 永不产生 inherited；6 orphan route 永不进 energy 输入；
7 MemoryBoard replace 保留全部 M6 字段；8 PostgresBoard roundtrip 保留全部字段；
9 fact_added 用 fact event ts；10 fact_verified replacement 用 promotion ts（刷新 pheromone）；
11 旧 Finding 缺字段时回退 `created_at` 衰减。

M6b：12 真实 workspace 路径测试；13 5MB 确定性轮转；14 retention 上限；
15 checkpoint 后重启不重复累计；16 checkpoint 落后时 reconcile；
17 partial line 忽略并计数；18 metrics 目录不可写时 graph append 仍成功；
19/20 metrics 开/关时 Reason prompt 逐字节一致 / gate 结果一致；
21 metrics 不增加 SharedGraph event；22 metrics summary 只增加 SessionStore telemetry event；
23 UI 不把 metrics actor 当 worker；24 两次 virtual replay 输出一致。

## 实施顺序

```
M6a-1（RouteObservation + resolver，纯解析）→ 定向测试 → 提交
M6a-2（Finding 字段 + 双 Board + 投影）→ 全量绿 → 提交
M6b-1（MetricsSink 路径/轮转/checkpoint/schema，不接生产埋点）→ 定向测试 → 提交
M6b-2（埋点 + 增量聚合 + UI reducer + ReplayClock harness + 红线回归）→ 全量绿 → 提交
```

M7（energy）输入 = M6a 的 `RouteObservation[]`（`eligible_for_energy` 已内置）；M6b 的重放
确定性支撑其 ablation harness。

---

## M7 energy 离线实验（09 §10.3.7 / §10.5 energy 1-8）

**Status (2026-08-16): Implemented — Contract v9.2 approved (Conditional Go closed), three
commits landed, full suite green。**
- M7-0：`dswarm/swarm/energy.py`（类型/枚举/validator/固定点序列化）+ `energy_capture.py`
  （有界只读捕获：progress handler、fact_effective + promotion_ts JOIN、applied dead-end、
  三计数）+ `energy_sidecar.py`（segment 轮转/partial tail 截断/两维 manifest/resume epoch
  ack guard/恢复折叠/complete 派生谓词）+ `reason_scheduler.py` 接线（`energy_trace_enabled`
  /`energy_trace_sink` 构造参数，fresh 后 cycle_started→capture→cycle_trace 两阶段，feature
  off 零副作用）；测试 23-39/45-51/53-68/74-116/119-127。
- M7-1：`route_energies`/`reorder_decisions` 三序列与 exact-equal 分组（`energy.py` 内）；
  测试 1-22/69-73。
- M7-2：`dswarm/swarm/energy_report.py`（离线 replay/只读折叠/两段归因指标/paired run-level
  bootstrap/coverage 与 N/A 纪律报告）；测试 40-44/52/117-118。
- M7 补足质检：M7 capture 与 M6 `resolve_route_observation()` 共用同一 lineage 裁决；
  sidecar/report 共用 canonical trace parser 与 segment 选择规则，finalized manifest 必须与实际
  segment fold 对账；malformed fact/promotion/applied conclusion 令 snapshot fail-closed；
  cold-start、`flag_captured` 与 bootstrap 阈值边界已补齐。
- 独立 benchmark harness：`dswarm/swarm/energy_benchmark.py` +
  `scripts/energy_benchmark.py`。operator-local factory 必须返回 `EnergyBenchmarkSuite`，且 case 的
  `run_id` 必须与其构造的 `ReasonSwarm.run_id` 一致：

  ```bash
  uv run python scripts/energy_benchmark.py package.module:build_suite
  uv run python scripts/energy_benchmark.py package.module:build_suite --output report.json
  ```

  输出 kind 固定为 `m7_offline_scheduling_reorder_estimate`；harness 不读取线上 energy 开关，
  不接 Web/UI，不改变 Reason 生产派发顺序。
**实施边界保持：不接在线 DSWARM_ENERGY_TIEBREAK（另立 RFC）、不改生产派发顺序、不改
provenance gate、不改 SharedGraph canonical 写语义；telemetry 样本失败不阻断派发；仅
dataset/process resume 完整性 witness 无法持久化时在新实例派发前 fail-fast。**

### 已核实的代码基线（v8 依据）

- `conclude_intent`（shared_graph.py:2966-3020）无条件追加 `EV_INTENT_CONCLUDED`，owner fence
  （:2991 `AND (worker=? OR worker IS NULL)`）更新 `intents.result_seq`——迟到结论进日志不生效；
- `journal_mode=DELETE` + `busy_timeout=5000`（shared_graph.py:780-781）；
- Reason 真实派发序（reason_scheduler.py:832-837, 941-954）：`capped = decisions[:limit]` →
  fresh 过滤（**保持模型序**）→ `_register_decision`/emit → `asyncio.gather`——**无
  lane/scale/priority 重排**；
- dispatchable queue 排序（shared_graph.py:4107-4110）：review 后置、operator 前置、
  priority DESC、created_seq ASC——与 Reason fresh 派发序不同；
- `fact_effective` VIEW 有 `fact_ts`/`promotion_seq`，**无 `promotion_ts`**（fact_events.py:77-118）；
- `normalize_priority`（priority.py:24-41）：None/bool/非法字符串/NaN/Inf → 0.0；label 映射；
  合法数值保持精度；
- `_emit` 固定 `actor="reason"`（reason_scheduler.py:125-145）；UI reducer 只隔离
  `kind == "metrics_summary"`（events.ts:691-693）；
- `lane_for`：worker_class ∈ {review, verifier} → "review"，否则 "ordinary"（lane_gate.py:47-54）；
- `DispatchDecision`（agents.py:33-53）无 `priority_scale` 字段（图上注册时计算）；M7 冻结为
  "planner"。

### M7-1 完整公式契约（v8：时间模型与三序列定稿）

```python
@dataclass(frozen=True)
class EnergyConfig:
    weights: Mapping[str, float]       # __post_init__：复制 + MappingProxyType + key 白名单
                                       # （verified_witness/verified/candidate）+ finite + 缺 key 报错
    tau: float = 1800.0                # > 0
    dead_penalty: float = 0.5          # [0,1]
    dead_tau: float = 7200.0           # > 0

@dataclass(frozen=True)
class RouteEnergy:
    route_hash: str
    positive: float                    # [0,1]
    penalty: float                     # [0,1] 正值
    energy: float                      # clamp(positive - penalty, 0, 1)
    flag_captured: bool                # 报告标签，不进公式
    raw_fact_count: int                # 排除后普查（universe 语义见下）
    correlation_group_count: int       # 实际进入概率合并的组数
    eligible: bool                     # := positive > 0（能否把 energy 抬到 0 以上）

def route_energies(
    observations: Sequence[EnergyObservationSnapshot],
    dead_ends: Sequence[DeadEndObservationSnapshot],
    config: EnergyConfig,
    *,
    as_of_ts: float,                              # finite epoch；仅用于衰减，不参与成员判定
    captured_routes: frozenset[str] = frozenset(),
) -> dict[str, RouteEnergy]:
    # 成员判定在 capture 层按 seq 截止完成（v8 时间模型）；本层只做公式与数值校验
    # 排除（公式层）：not eligible_for_energy / retired；全部数值 finite 校验
    # tier：verified 且 bool(witness.strip()) → verified_witness；verified → verified；否则 candidate
    # confidence = clamp_finite(obs.confidence, 0, 1)
    # raw_score = weights[tier] * confidence
    # decayed = clamp(raw_score, 0, 1) * exp(-age / tau)，age = max(0.0, as_of_ts - energy_origin_ts)
    # correlation 组内：max(decayed)；route 内：1 - Π(1 - group_score)
    # penalty：dead_penalty_i = dead_penalty * exp(-age / dead_tau)
    #   route_penalty = max(dead_penalty_i for same route)   ← 正值取 max（永不求和）
    # energy = clamp(positive - route_penalty, 0.0, 1.0)
    # 参与规则：challenged 不进正贡献；revalidated 恢复 base verdict、不刷新 energy_origin_ts；
    #   promotion 刷新 energy_origin_ts（promotion_ts）
    # universe = {有贡献观测的 route} ∪ {有 applied dead-end 的 route} ∪ captured_routes；
    #   dead-end-only / captured-only route → positive=0, energy=0, eligible=False
    # standalone dead_end v1 audit-only（不产生 penalty）

def reorder_decisions(
    decisions: Sequence[EnergyDecision],
    *,
    enabled: bool,
    energy_supplier: Callable[[], Mapping[str, RouteEnergy]],
) -> list[EnergyDecision]:
    # 三条序列（v8 定稿）：
    #   production_order        = 输入序（= Reason fresh 真实派发序，模型序）
    #   planner_baseline_order  = sort((lane_rank, scale_rank, -normalized_priority, original_index))
    #   energy_order            = sort((lane_rank, scale_rank, -normalized_priority,
    #                                   -energy_within_exact_group, original_index))
    # enabled=False → 返回 production_order（逐元素 == 输入，与现网派发逐决策等价）、
    #   supplier 调用次数严格 0
    # exact_equal_group = (worker_lane, priority_scale, normalized_priority)；
    #   normalized_priority = normalize_priority(decision.priority)（priority.py 复用，构造时冻结）；
    #   equal 判定 = IEEE float ==（normalized 值）
    # energy 只影响 exact-equal 组内顺序；不得跨 lane/scale/priority 重排
    # M7 只接受 snapshot 输入，不读 SQLite、不 import SharedGraph（静态断言）
```

### 统一类型模型（v8：全展开，无 `...`、无"字段同 v6"）

```python
@dataclass(frozen=True)
class EnergyObservationSnapshot:
    fact_seq: int
    fact_origin_ts: float                # = VIEW fact_ts（fact_added 事件 ts）
    energy_origin_ts: float              # = promotion_ts（promotion_seq → events.seq）否则 fact_ts
    route_hash: str
    lineage: str
    lineage_reason: str
    inherited_intent_ids: tuple[str, ...]
    state: str                           # 枚举见下
    retired: bool
    verified: bool
    base_verified: bool
    confidence: float                    # VIEW confidence（challenged → 0.4）
    witness: str
    artifact_id: str
    source: str
    actor: str
    correlation_kind: str                # "artifact" | "fallback"
    correlation_basis_hash: str
    eligible_for_energy: bool            # 结构性：route_hash 非空 ∧ confidence finite ∧ lineage 已裁决
    exclusion_reason: str                # "" | missing_route_hash | non_finite_confidence | lineage_unresolved

@dataclass(frozen=True)
class DeadEndObservationSnapshot:
    intent_id: str
    route_hash: str
    result_seq: int                      # = intents.result_seq（applied conclusion，canonical）
    concluded_ts: float                  # = applied conclusion 事件 ts（events.seq = result_seq）
    result: str
    genuine_giveup: bool
    eligible_for_energy: bool            # applied ∧ genuine_giveup ∧ route_hash 非空
    exclusion_reason: str                # "" | missing_route_hash | not_applied | not_genuine_giveup
    conclusion_event_count: int          # 审计：该 intent 全部 conclude 事件数
    ignored_stale_conclusion_count: int  # 审计：owner-fence 失败被忽略的迟到结论数

@dataclass(frozen=True)
class GraphCycleSnapshot:
    graph_after_seq: int                 # = 同事务 MAX(seq)；因果截止唯一权威
    observations: tuple[EnergyObservationSnapshot, ...]
    dead_ends: tuple[DeadEndObservationSnapshot, ...]
    complete: bool                       # 三层不变式见 sidecar 节
    exclusion_reason: str
    observed_fact_count: int             # 阶段 1 DB 读到的 fact 原始行数（P1-3）
    captured_fact_count: int             # 阶段 2 成功构造的 observation 数（P1-3）
    stored_fact_count: int               # 实际保存的 len(observations)；超限 stub = 0（P1-3）

@dataclass(frozen=True)
class EnergyDecision:
    decision_id: str                     # blake2b(run_id|trace_id|original_index|intent_id|decision_source)
    trace_id: str                        # m7-cycle::{run_id}::{instance_uuid}::{generation}
    reason_cycle_id: str                 # UI 展示，不参与去重
    intent_id: str
    route_hash: str
    worker_lane: str                     # "ordinary" | "review"（lane_gate.lane_for）
    priority: float                      # 原始值（DispatchDecision.priority）
    normalized_priority: float           # normalize_priority(priority)，构造时冻结
    priority_scale: str                  # "planner"（M7 v1 只捕获 Reason fresh）
    original_index: int                  # fresh 零基索引
    decision_source: str                 # "reason"

@dataclass(frozen=True)
class CycleTrace:
    schema_version: int
    trace_id: str
    reason_cycle_id: str
    decision_ts: float                   # epoch（time.time()）
    expected_decision_count: int
    decisions: tuple[EnergyDecision, ...]
    snapshot: GraphCycleSnapshot         # 内嵌，不手工复制
    complete: bool
    exclusion_reason: str
    serialized_bytes: int
    serialized_bytes_attempted: int | None

# CycleTraceSummary：v8 删除——M7 v1 sidecar-only，不发 EventBus 摘要（见表述与 UI 边界）
```

**枚举与 validator（逐项定义，无实现者猜测）**：

```text
state                      ∈ {candidate, verified, challenged, revalidated, rejected, merged, superseded}
correlation_kind           ∈ {artifact, fallback}
observation.exclusion_reason ∈ {"", missing_route_hash, non_finite_confidence, lineage_unresolved}
dead_end.exclusion_reason  ∈ {"", missing_route_hash, not_applied, not_genuine_giveup}
worker_lane                ∈ {ordinary, review}；priority_scale ∈ {planner}（v1）
decision_source            ∈ {reason}（v1）
validator：全字段类型/枚举/finite/非负校验；decision_id 重算比对；违例 → complete=False 并拒收
```

**dead-end canonical 来源（阻断 3，已获批）**：只从 `intents.result_seq → events.seq` 读取
applied conclusion（owner fence 已过滤迟到结论）；`MAX(intent_concluded.seq)` **禁用**；迟到
结论仅计入 `ignored_stale_conclusion_count` 审计，**不得进 penalty**。

### SQLite 捕获：内部有界执行（阻断 4/5 选边定稿 + v8 时间模型）

**选边：阶段 1 直接读 M3 `fact_effective` VIEW**（不再"只取 raw rows"），不在 M7-0 重构
M3 投影系统：

```text
专用只读连接（非主 _conn）：
  PRAGMA query_only=ON
  PRAGMA busy_timeout=250           # 短超时，防阻塞 blackboard writer
  set_progress_handler(deadline_monotonic)   # 超时经 progress handler 中止 SQLite VM
阶段 1（同一短事务，graph_after_seq = MAX(seq) 先读）：
  BEGIN
  1. SELECT MAX(seq) AS graph_after_seq
  2. SELECT ... FROM fact_effective
  3. JOIN events(seq = fact_effective.promotion_seq) → promotion_ts（显式 JOIN，v8 时间模型）
  4. SELECT intent_products + intents（lineage，created_seq <= graph_after_seq）
  5. SELECT applied conclusions：intents.result_seq IS NOT NULL
     AND result_seq <= graph_after_seq → JOIN events(seq = result_seq) 取 concluded_ts
  COMMIT
阶段 2（事务外）：lineage 裁决 → correlation hash → dataclass → validator → 序列化/大小检查
finally：rollback（若在事务）+ close
```

- **因果成员资格唯一权威 = seq**：graph_after_seq = 同事务 MAX(seq)；阶段 1 全部读取在同一
  事务内完成，rollback-journal（DELETE）下写者被阻塞，所见行 seq ≤ graph_after_seq 恒成立；
  **event_ts 不参与成员判定**（v7 测试 24 的 `event_ts > cursor` 量纲错误已修正）；
- **时间戳只用于衰减**：fact_origin_ts = fact_ts；energy_origin_ts = promotion_ts（若
  promotion_seq 非空）否则 fact_ts；concluded_ts = applied conclusion 事件 ts；ts 晚于
  as_of_ts → age = max(0, ...) = 0（时钟偏差饱和，不做第二套成员权威）；
- **asyncio 超时 ≠ SQLite 查询已停止**：真正的时限由 `capture_energy_cycle_snapshot()`
  内部 deadline 实现；scheduler 可用 `asyncio.to_thread`，但不得只靠 `wait_for` 假装取消；
- 任何失败（busy/超时/异常）→ `complete=False, exclusion_reason="snapshot_unavailable"`，
  继续 dispatch，绝不 raise；不改 WAL/substrate；
- 语义声明：snapshot 描述 capture 时刻的图状态（可能晚于 Reason 读图时刻，如实记录
  graph_after_seq）；energy 归因以 capture 状态为准。

### sidecar 崩溃恢复协议与 manifest 状态机（P0-1/2/4 定稿）

```text
// energy-cycle-traces.%06d.jsonl 行类型（每行一个 JSON，flush + fsync）
{"kind":"cycle_started","trace_id":"...","schema_version":1,"reason_cycle_id":"...","decision_ts":...}
{"kind":"cycle_trace","trace_id":"...", <CycleTrace 全部字段，或超限/失败 exclusion stub>}
{"kind":"resume_epoch","resume_epoch_id":"UUID4","resume_ts":...,"prior_lifecycle":"...",
 "prior_data_quality":"...","schema_version":1}
```

**durable attempt 协议（阻断 5）**：fresh 形成后顺序——1. 生成 trace_id；2. durable append
`cycle_started`（flush + fsync）；3. capture snapshot；4. 构造 trace 或 exclusion stub；
5. durable append `cycle_trace`（flush + fsync）；6. 之后才 `_register_decision()` 与 dispatch。

**record append 失败契约（P0-2 定稿，cycle_started 与 cycle_trace 统一）**：
- 任何 sidecar append/fsync 失败：捕获异常，绝不向 scheduler 外传播；**不阻断 dispatch**
  （生产优先，telemetry 只损失实验样本）；
- 进程内 recorder_dirty=True，且**内存 data_quality 即时下调**为 max(current, incomplete)
  （与 durable 写成功与否无关）；exclusion_reasons += ["cycle_started_append_failed" |
  "cycle_trace_append_failed"]；随后尝试 durable 降级 manifest（temp→flush→fsync→replace）；
- cycle_trace 失败的特殊后果：已持久化的 cycle_started 在恢复扫描时成为 orphan started →
  dataset 至少 incomplete（双保险：本进程降级 + 恢复孤儿判定）；
- 本进程 finalize：只要 recorder_dirty=True，**禁止**写 finalized + clean（只能 finalized +
  当前内存质量，即 ≥ incomplete）；
- 降级 manifest 前再次崩溃 → 由 unclean reopen（下）接管；
- 无论降级是否成功，继续原有 `_register_decision()`/dispatch 事件/gather 顺序。

**resume guard（v9.2 P0 定稿，不受 energy_trace_enabled 控制）**：见术语节——guard 只针对
**dataset/process resume**（为已结束/释放/丢失原 ReasonSwarm 实例的 run 新建 ReasonSwarm 并
继续产生 Reason cycle）；同一实例内的 live HITL pause→resume 不触发 guard。guard 在任何
Reason cycle/dispatch 前执行，**resume_epoch 始终先写（单一路径，既是正常协议也是兜底）**：

```text
0. 若原 lifecycle 已是 in_progress → 先按 unclean reopen 规则降级
   （data_quality=max(current, incomplete)、exclusion_reasons += ["unclean_reopen"]）
1. 生成 resume_epoch_id = UUID4
2. durable append {"kind":"resume_epoch","resume_epoch_id":...,"resume_ts":...,
   "prior_lifecycle":...,"prior_data_quality":...}（flush + fsync）
   ← 唯一完整性 witness；resume_ts 仅审计/展示，不参与任何因果判定
3. append 失败 → **fail-fast 拒绝本次 resume**（清晰报错，无 dispatch；操作者可重试）
4. append 成功 → 尝试 manifest 翻转 lifecycle_status=in_progress：
   - if energy_trace_enabled is False:
       data_quality = max(data_quality, incomplete)
       exclusion_reasons += ["resume_without_energy_trace"]
   - if prior_data_quality != clean（enabled 但历史数据已降级）:
       保持原 data_quality 与原 exclusion_reasons，**不**额外写 resume_without_energy_trace
5. 翻转失败**也继续**（resume_epoch 已是 durable witness；翻转只是增强，不是前提）
6. 之后才允许 Reason cycle / dispatch
```

- 边界说明：resume guard 是**数据集完整性门**，不是 telemetry 样本管线——样本级故障
  （capture/cycle_started/cycle_trace/manifest 更新失败）不阻断 dispatch/stop/hint；唯一
  fail-fast 点是 witness（resume_epoch）写不进去（否则恢复边界会静默漏记），且发生在新
  实例产生任何派发之前；
- 崩溃序分析：resume_epoch 前崩溃 → 无 dispatch，旧 finalized+clean 数据集仍真实；
  resume_epoch 后、finalize 前崩溃 → last_resume_epoch_id 未被 ack（见 complete 谓词）→
  非 complete；翻转成功后再崩溃 → unclean reopen 降级（双保险）。**所有路径均无
  "complete 却缺 cycle"**；
- finalize：durable 写 manifest 时把当前全局折叠的 last_resume_epoch_id 写入
  `finalized_resume_epoch_id`（acknowledgment）；**因果确认用 epoch id，禁用墙钟比较**。

**unclean reopen 规则（P0-1 定稿）**：reopen 时读到的 manifest 若 lifecycle_status ==
in_progress（无论原因），在任何 Reason cycle/capture/`_register_decision`/dispatch 前：

```text
data_quality = max(data_quality, "incomplete")       # sticky
exclusion_reasons += ["unclean_reopen"]              # set 语义去重
durable_write_manifest(lifecycle_status="in_progress", data_quality, ...)
```

（resume guard 步骤 0 先执行同一降级；本规则亦可被 recorder 独立初始化路径调用，幂等：
max() 单调 + 原因 set 去重。）

- 只有 reopened 前已是 finalized 的 manifest 在合法 resume 时回到 in_progress 而**不**自动降级
  （quality 保持原值）；但若该 resume 后再崩溃，下次 reopen 按本条降级；
- 残余窗口（fresh 形成 → cycle_started 落盘前崩溃）：无 durable 记录，但 manifest 停在
  in_progress → reopen 触发 unclean_reopen 降级 → **该 run 永久非 complete**；不存在
  "宣称 complete 却缺 cycle" 的路径（v8 的推论由本规则落实）。

**manifest 状态机（两维，阻断 6）**：

```json
{
  "schema_version": 1,
  "run_id": "...",
  "lifecycle_status": "in_progress | finalized",
  "data_quality": "clean | incomplete | corrupt",
  "exclusion_reasons": [],
  "created_ts": 0.0, "finalized_ts": 0.0,
  "finalized_resume_epoch_id": "",
  "cycles_started": 12, "cycles_written": 11, "cycles_failed": 1, "cycles_excluded": 2,
  "segment_count": 4,
  "total_trace_bytes": 1234567,
  "max_trace_bytes": 2097152, "max_run_trace_bytes": 268435456, "max_segment_bytes": 16777216,
  "first_trace_id": "...", "last_trace_id": "..."
}
```

- 初始（无 manifest 且无 segment）→ {in_progress, clean}；finalize：durable 写 finalized +
  `finalized_resume_epoch_id` = 当前全局折叠的 last_resume_epoch_id（acknowledgment）；
  finalized_ts/created_ts 仅审计展示，不参与任何判定；data_quality 单调只降不升；
- **complete 是派生谓词，非存储态**（v9.2：因果确认用 epoch id，禁用墙钟比较）：
  lifecycle_status=finalized ∧ data_quality=clean ∧
  `finalized_resume_epoch_id == last_resume_epoch_id`（每次 resume 都必须被某次成功 finalize
  acknowledge；无 resume 时两侧均为 ""）∧ 无 orphan started ∧ 无 corrupt 分类
  （duplicate/trace-without-started）∧ 全部 trace.complete ∧ 无 malformed ∧
  total ≤ MAX_RUN_TRACE_BYTES ∧ 计数恒等式成立；
- **last_resume_epoch_id 是扫描派生状态，不持久化**（终审 P2 定稿）：manifest 只保存 finalize
  时确认的 `finalized_resume_epoch_id`；任何 manifest 加载/重建都必须先按物理 append 顺序
  折叠全部 segment 得到运行时 last_resume_epoch_id，再计算 complete——segment 是唯一
  recovery truth，不存在可能过期的第二份权威副本；
- manifest 缺失或损坏但 segment 已存在 → 重扫 segment 重建计数（并照常派生
  last_resume_epoch_id），data_quality 至少 incomplete，**不得重建为 clean**。

**manifest durability 协议（P0-4 定稿）**：

```text
1. 写同目录 temp 文件；2. flush(temp)；3. os.fsync(temp_fd)；4. os.replace(temp, manifest)；
5. POSIX 上尽力 fsync(parent_directory)；Windows 上目录 fsync 不保证支持（如实声明平台保证）；
6. 任一步失败：保留上一个 manifest；recorder_dirty=True；本进程不得产生 finalized+clean；
   segment 保持 recovery truth；遗留 temp 文件尽力删除（清理失败不得覆盖主异常）。
```

**跨 segment 配对（P1-1 定稿）**：恢复/校验/计数一律做**全 run 全局折叠**——按 segment 编号
升序扫描全部有效行，构建 started_by_trace_id / trace_by_trace_id / resume_epoch 序后，才计算
orphan/duplicate/failed/计数恒等式。分类：started 无 trace → orphan（failed，quality ≥
incomplete）；duplicate started → corrupt；duplicate trace → corrupt；**trace 无 started →
corrupt**；cycle_started 与 cycle_trace 跨 segment 是合法状态。

**resume_epoch 行协议（v9.2 定稿）**：
- 唯一身份 = resume_epoch_id（UUID4）；duplicate 处理选边：**内容完全一致 → 幂等折叠（丢弃
  重复行）**；同 id 内容不一致 → corrupt；malformed → corrupt（同中间 malformed 规则）；
- 参与 MAX_SEGMENT_BYTES append 前预判（是 sidecar 行）；**不计入 total_trace_bytes**
  （该字段只表 trace/stub 数据量）；
- last_resume_epoch_id 按**物理 append 顺序**（segment 编号升序 + 行序）确定，**禁止按
  resume_ts 排序**；跨 segment 是合法状态；
- last_resume_epoch_id 由全量折叠派生（不持久化，终审 P2）；complete 要求
  manifest.finalized_resume_epoch_id == last_resume_epoch_id。

**三层 complete 不变式（互不推导）**：
- snapshot.complete = 捕获事务成功 ∧ 全量读取 ∧ 未超 capture deadline ∧ snapshot validator
  通过（**大小只在 trace 层判定**：canonical bytes ≤ MAX_TRACE_BYTES，v9.1 修订）；
- trace.complete = snapshot.complete ∧ len(decisions) == expected_decision_count ∧
  trace validator 通过 ∧ canonical bytes ≤ MAX_TRACE_BYTES；
- dataset complete = 上节派生谓词。

**字节计量：canonical record 与固定点算法（P0-3 定稿）**：

```text
canonical record = 包含 kind="cycle_trace" 的完整 JSONL object（envelope 计入计量）；
计量单位 = UTF-8 字节，不含尾换行（非 ASCII/中文按 UTF-8 字节，非字符数）。

完整 trace：
  1. serialized_bytes_attempted = None
  2. serialized_bytes 初值 0
  3. canonical encode
  4. serialized_bytes = encoded byte length
  5. 重复 3-4 直到数值稳定（固定点）
  6. 稳定长度 <= MAX_TRACE_BYTES → 写完整 record；> → 构造 stub

迭代上限（防御，v9.1）：MAX_FIXED_POINT_ITERATIONS = 8；超限 → complete=False +
exclusion_reason="size_fixed_point_failed"、data_quality 至少 incomplete、继续 dispatch
（防编码器/schema 后续变化导致无限循环）。

超限 stub：
  serialized_bytes_attempted = 完整 record 的稳定长度
  stub 的 serialized_bytes 对 stub record 独立再做固定点编码
  stub 最终实际写入行长度必须 == 其 serialized_bytes（断言）
```

MAX_RUN_TRACE_BYTES = 256MiB，计量对象 = cycle_trace/stub 实际行内容（不含换行与 manifest；
cycle_started 不计）。MAX_TRACE_BYTES = 2MiB。

**fact 计数三拆（P1-3 定稿）**：
- observed_fact_count = 阶段 1 DB 查询读到的 fact 原始行数；
- captured_fact_count = 阶段 2 成功构造（lineage/correlation/validator 通过）的 observation 数；
- stored_fact_count = 本 record 实际保存的 len(observations)（完整 trace = captured；
  超限 stub = 0，且 captured > 0）。

**超限 stub 内容**：保留 decisions 全量 + trace 元数据 + observed/captured_fact_count
（stored_fact_count=0）；observations=()、dead_ends=()（体积来源丢弃）；complete=False +
exclusion_reason。

**segment 规则**：MAX_SEGMENT_BYTES = 16MiB；append 前预判
current_size + len(line) + 1 > MAX_SEGMENT_BYTES → 换新段（cycle_started 与 cycle_trace 同规则
参与预判）；编号 %06d 从 000000；reopen 取最高编号段，先尾部截断再续写；segment_count =
磁盘存在的段文件数（含空段）。MAX_TRACE_BYTES(2MiB) < MAX_SEGMENT_BYTES(16MiB) → 合法 trace
恒装入单段（契约断言）。

**partial tail（定稿）**：仅最后一段最后一行可 partial；reopen 截断至最后一个 '\n' + fsync
后允许 append；非最后段的 partial/malformed → corrupt；中间 malformed → 停止读取该行及后续
全部段 → corrupt。

**计数恒等式**（以全局折叠结果为准）：cycles_started = durable cycle_started 记录数；
cycles_written = cycle_trace 行数（complete + stub）；cycles_failed = orphan started；
cycles_excluded = trace.complete=False 的 stub 行；恒等式 cycles_started = cycles_written +
cycles_failed、cycles_written = complete + excluded、excluded ≤ written。
snapshot_unavailable/超限/validator 失败 = excluded stub（有 durable trace 行，非 failed）。

### scheduler 接入时序与字段所有权（高优 1 定稿，v8 两阶段协议）

```text
1. _run_reason() → _decisions_from_reason() → capped/fresh 过滤完成（模型序）
2. 若 M7 telemetry enabled 且 fresh 非空：
   a. 生成 trace_id（m7-cycle::{run_id}::{instance_uuid}::{generation}）
   b. durable append cycle_started（flush + fsync）
   c. capture graph snapshot（to_thread + 内部 deadline）
   d. 按 fresh 构造 EnergyDecision（normalized_priority 冻结）→ 构造 trace 或 exclusion stub
   e. durable append cycle_trace（flush + fsync）
3. 原有 _register_decision() + dispatch 事件
4. 原有 gather，顺序完全不改变（fresh 模型序）
```

字段规则（无"实现者自行猜测"空间）：`expected_decision_count = len(fresh)`；
`original_index` = fresh 零基索引；`worker_lane = lane_gate.lane_for(mode, worker_class)`；
`normalized_priority = normalize_priority(decision.priority)`（priority.py 复用，不重实现）；
`priority_scale = "planner"`（DispatchDecision 无该字段，图上注册时才计算——M7 冻结）；
`decision_source = "reason"`；`route_hash` = 归一化 decision route。M7 v1 只捕获 fresh
Reason 候选集。

### 三序列与报告归因（阻断 4 定稿）

- `production_order` = fresh 索引序（**真实派发序**，reason_scheduler.py:832-837/941-954
  无重排，模型序）；
- `planner_baseline_order` = sort(fresh, key=(lane_rank, scale_rank, −normalized_priority,
  original_index))——对齐 dispatchable queue 语义（shared_graph.py:4107-4110）；
- `energy_order` = sort(fresh, key=(lane_rank, scale_rank, −normalized_priority,
  −energy_within_exact_group, original_index))；
- 报告分两段：production→planner_baseline（**上下文，不归因 energy**）与
  planner_baseline→energy（**energy 归因**）；displacement/reorder-count/churn/rank-corr/top-k
  均按两段分别计算；zero-change run 指 planner_baseline→energy 段无重排；
- enabled=False → production_order；**M7 v1 不把 production→planner 差异计入 energy 指标**。

### M7 telemetry 开关（P1-2 定稿，v9.1 收窄零副作用）

- 显式构造参数，不在 scheduler 内部散读环境变量：
  `ReasonSwarm(..., energy_trace_enabled: bool = False, energy_trace_sink: EnergyTraceSink | None = None)`；
- **零副作用仅对 fresh run 承诺**（v9.1）：enabled=False 且磁盘不存在该 run 的既有 M7
  dataset（无 manifest、无 segment）→ 严格零副作用：不创建 metrics 目录、不创建 SQLite 专用
  连接、不生成 trace_id、不做 canonical JSON、不调用 fsync；dispatch 与现网逐决策等价
  （测试 103）；
- enabled=True 且 sink=None → 构造报错（无落点不开采集）；
- **resume 术语（v9.2 定稿）**：guard 只针对 **dataset/process resume** = 为已结束/释放/
  丢失原 ReasonSwarm 实例的 run 新建 ReasonSwarm 并继续产生 Reason cycle（guard 在该新实例
  任何 Reason cycle/dispatch 前执行）；**live operator resume** = 同一 ReasonSwarm 实例内
  HITL pause→resume，只恢复派发，**不执行 guard、不写 resume_epoch、不改 manifest
  lifecycle/quality**（guard 只在 recorder/dataset 初始化路径运行一次，pause 不重新初始化）；
- **dataset resume 时 guard 不受开关控制**（v9.1）：disabled dataset resume → resume_epoch
  先写 + 翻转 in_progress + quality ≥ incomplete + 原因 resume_without_energy_trace → 该数据
  集此后永非 complete（测试 104 自 finalized+clean 起步验证）；witness 写失败 → fail-fast；
- enabled dataset resume：正常 reopen 语义（finalized+clean → in_progress+clean 不自动降级，
  resume_epoch 正常记录并由下次 finalize acknowledge）。
- M7 离线 v1 仅由 benchmark harness 显式开启；Web/CLI 暴露另行接线。

### 表述与 UI 边界（高优 2/3 定稿）

- 全文将 "causal ablation" 统一改为 **"offline scheduling reorder estimate（离线调度重排
  估计）"**——静态重排不证明 flag latency/token/worker starts/race/solve-rate；
- 可输出（按 planner_baseline→energy 段归因；production→planner 段单独作上下文）：
  submission-order displacement、exact-equal group reorder count、route churn、
  rank correlation、top-k route composition、zero-change run ratio、coverage/incomplete/
  corrupt 比例；`flag_latency/tokens_saved/worker_starts_saved/solve_rate_delta/race_outcome/
  counterfactual_cost` 一律 N/A；paired bootstrap 只对这些离线重排指标做 CI；
- **EventBus 选边（v8 定稿）：M7 v1 sidecar-only**——不发 EventBus 摘要、不调用 `_emit`
  （其固定 actor="reason"，reason_scheduler.py:125-145，无法复用发空 actor）、删除
  CycleTraceSummary 类型、不改 UI reducer（events.ts:691-693 的 metrics_summary 隔离不动）；
  UI 展示另行 RFC。

### 完整测试矩阵（127 项自包含清单，v9.2 终审；每项标注归属 M7-0/M7-1/M7-2）

每项可仅凭本契约独立编码（1-55 语义覆盖 v3-v5/v6 全部条目；8/16/18/22/24/25/26/38/42/51/
58-62/64 按 v8 修订；67-86 为 v8 新增；87-105 为 v9 新增；106-118 为 v9.1 新增；119-126 为
v9.2 新增；127 为终审新增）。**测试所有权（终审修正，无重叠）**：M7-0 =
23-39/45-51/53-68/74-116/119-127；M7-1 = 1-22/69-73；M7-2 = 40-44/52/117-118；每阶段只要求
当时已存在且归属本阶段的测试绿（M7-0 不得要求提前实现 M7-1/M7-2 测试目标）。

**1-22 M7-1 公式、配置与排序**
1. EnergyConfig 拷贝输入映射 + MappingProxyType（构造后改原始映射不影响已构造实例）。
2. 权重 key 白名单：未知 key → 构造报错。
3. 权重 key 完整性：缺 verified_witness/verified/candidate 任一 → 构造报错。
4. 权重非有限值（NaN/inf）→ 构造报错。
5. tau>0 / dead_penalty∈[0,1] / dead_tau>0 域校验。
6. confidence 域外 → clamp_finite 到 [0,1]。
7. age = max(0, as_of_ts − energy_origin_ts)；未来 origin 不产生负 age。
8. as_of_ts 非 finite 报错；**event_ts 不参与成员判定**（成员只按 seq，v8 时间模型）。
9. eligible_for_energy=False 排除；route_hash 缺失/空排除。
10. tier 判定顺序：verified_witness（witness.strip() 非空）> verified > candidate。
11. raw_score = weight × confidence；decayed = clamp(raw,0,1) × exp(−age/tau)。
12. correlation 组内 max（同 correlation_basis_hash 多观测）；跨组 1 − Π(1 − group_score)。
13. dead penalty = dead_penalty × exp(−age/dead_tau)；同 route 多 dead-end **正值 max 合并**
    （永不求和、永不超最强 penalty）。
14. energy = clamp(positive − penalty, 0, 1)，恒 ≥ 0。
15. flag_captured 仅报告标签，不进公式。
16. retired 排除 / challenged 不进正贡献 / revalidated 恢复 base verdict 不刷新 origin /
    promotion（promotion_ts）刷新 origin——四断一测；standalone dead_end（v1）audit-only
    不产生 penalty。
17. exact_equal_group = (worker_lane, priority_scale, normalized_priority)；equal 判定用
    IEEE float ==（normalized 值）；跨组/跨 lane/跨 priority 永不重排。
18. planner_baseline key = (lane_rank, scale_rank, −normalized_priority, original_index)。
19. energy key = (lane_rank, scale_rank, −normalized_priority, −energy_within_exact_group,
    original_index)。
20. 无观测 route → energy 0 → 组内退回 planner_baseline 序；冷启动全零 → energy_order ==
    planner_baseline_order（稳定排序，original_index 保持）。
21. lane_rank ordinary=0 < review=1、scale_rank operator=0 < planner=1 全局保序。
22. enabled=False → 返回 production_order（逐元素 == 输入，与现网派发逐决策等价）+
    supplier 调用 0 次；模块不 import sqlite3/SharedGraph（静态断言）。

**23-45 snapshot/sidecar/统计（v3-v5 语义）**
23. 原子并发：writer 并发写时，捕获结果与 graph_after_seq 同快照一致（单事务）。
24. **因果截止（v8 修订）**：graph_after_seq = 同事务 MAX(seq)；seq > graph_after_seq 的
    transition/fact/结论不得进入快照；event_ts 不参与成员判定。
25. 超限 stub（MAX_TRACE_BYTES=2MiB，v8 修订）：保留 decisions 全量 + 元数据 +
    observed/captured_fact_count（stored_fact_count=0）；observations=()、dead_ends=()；
    complete=False + exclusion_reason + serialized_bytes_attempted；超限数据不保留内存。
26. **partial tail（v8 修订）**：仅最后一段最后一行可 partial；reopen 截断至最后一个 '\n'
    + fsync 再续写；非最后段 partial → corrupt。
27. validator：必填字段缺失/非法枚举 → 拒收。
28. 显式序列化：CycleTrace → JSON 走显式编码函数，不依赖默认隐式契约。
29. epoch ts：decision_ts 用 time.time() epoch，非 ISO 字符串。
30. feature-off：capture 调用 0 次（不建连接）。
31. 双模型已删：observations 单一权威，无 facts/routes 双集合。
32. read transaction 不阻断 dispatch：capture 期间 stop/hint 响应、blackboard writer
    不超 busy_timeout。
33. capture 异常 → complete=False + snapshot_unavailable，继续 dispatch，绝不 raise。
34. 两阶段：事务内无 hash/dataclass/归一化/序列化（静态断言）。
35. M3 折叠语义：读 fact_effective VIEW；retired 后 state、challenged、revalidated 与 M3 一致。
36. trace_id 跨恢复唯一：同 run 两实例 → 两条记录、decision_id 不冲突。
37. sidecar run 内不删段（无 unlink）。
38. 中间 malformed 完整行 → 停止读取后续 segment，data_quality=corrupt（v8 字段名）。
39. 字节上限计量对象 = canonical CycleTrace JSON（不含尾换行）。
40. paired run-level delta bootstrap：run 内 cycles 整体重采样，不独立抽。
41. bootstrap 参数：seed=20260816、2000 次、95% percentile CI。
42. run 资格分档（v8 修订）：不完整/corrupt 排除主 CI；完整但 planner_baseline→energy 无重排
    → 零变化 run 纳入；单 cycle 超限 → run incomplete 仅进 coverage；run<5 → N/A、
    5-19 → exploratory、≥20 → CI。
43. N/A 纪律：flag_latency/tokens_saved/worker_starts_saved/solve_rate_delta/race/counterfactual
    全部 N/A。
44. coverage/incomplete/corrupt 比例统计输出。
45. sidecar 去重键 = trace_id（非 reason_cycle_id）。

**46-55 v6 新增（原文语义，47 修订）**
46. resume cycle ID 碰撞：两实例同 run，trace_id/decision_id 不冲突、双记录保留。
47. dead-end 截止（v7 修订保留）：canonical = intents.result_seq → events（≤ graph_after_seq）；
    同 intent 多次 conclude 只取 applied 一次，其余计 ignored_stale_conclusion_count。
48. read transaction 不阻断 dispatch。
49. sidecar run 内不删 segment。
50. 中间 malformed 行 → dataset corrupt。
51. 完整 trace 字节上限（2MiB）+ stub 语义（保留 decisions/元数据/计数，丢
    observations/dead_ends）。
52. paired run-level delta bootstrap。
53. 双模型已删（observations 单一权威）。
54. 两阶段捕获（事务内无 hash/序列化——静态断言）。
55. 日期已更新为 2026-08-16。

**56-66 v7 新增（部分按 v8 修订）**
56. dead-end 读 intents.result_seq 而非 MAX(seq)：迟到结论（owner-fence 失败）仅计审计数，
    不进 penalty。
57. progress-handler 超时中止 SQLite VM：事务不滞留、连接 finally close、
    busy_timeout=250、query_only=ON。
58. manifest 两维字段（v8 修订）：lifecycle_status + data_quality + cycles_started/written/
    failed/excluded + segment_count + total_trace_bytes + 三个上限常量 + first/last_trace_id。
59. dataset complete 派生谓词（v8 修订）：finalized ∧ clean ∧ 无孤儿 cycle_started ∧
    全 trace.complete ∧ 无 malformed ∧ ≤256MiB ∧ 计数恒等式。
60. resume 不提升（v8 修订）：quality 单调只降不升；reopen 前 durable 写 in_progress。
61. trace 成功 + manifest 更新失败 → 重扫 segment 重建（trace_id 去重）。
62. scheduler 顺序含两阶段协议（v8 修订）：fresh 形成 → cycle_started → capture →
    cycle_trace/stub → 才 _register_decision/dispatch；字段来源：expected_decision_count=
    len(fresh)、original_index 零基、worker_lane=lane_for、normalized_priority=
    normalize_priority(priority)、priority_scale="planner"、decision_source="reason"。
63. 表述统一：全文无 "causal ablation"，只用 "offline scheduling reorder estimate"。
64. CycleTraceSummary 已删除（v8 修订）：sidecar-only——静态断言无该类型、无 _emit 调用、
    无 UI reducer 改动。
65. CycleTrace 内嵌 snapshot：无信息丢失、无手工复制字段。
66. M7-1 公式契约自包含：测试 1-22 仅凭契约可独立编码。

**67-86 v8 新增**
67. 因果成员只按 seq：含"未来 ts"的已提交事件仍按 seq 入快照（量纲修正回归）。
68. promotion ts JOIN：energy_origin_ts = events(seq=promotion_seq).ts；无 promotion →
    fact_ts。
69. normalized_priority 冻结：None/bool/非法字符串/NaN/Inf → 0.0；label 映射；合法数值保持
    精度（复用 priority.normalize_priority——静态断言 import）。
70. exact-equal 用 IEEE float ==（normalized 值）；energy.py 无第二套归一化。
71. 三序列独立计算：production_order=输入序；planner_baseline_order 按 (lane_rank,
    scale_rank, −normalized_priority, original_index)；energy_order 加 −energy。
72. 归因分段：报告分别计算 production→planner_baseline 与 planner_baseline→energy；只有
    后者进 energy 指标。
73. enabled=False → production_order（与现网派发逐决策等价）+ supplier 0 次调用。
74. 两阶段协议顺序：fresh 形成后先 durable cycle_started（flush+fsync）→ capture →
    cycle_trace/stub → 才 _register_decision/dispatch（顺序断言）。
75. 恢复扫描：cycle_started 无 cycle_trace → data_quality 永久 incomplete（sticky）。
76. manifest 状态机：in_progress→finalized；quality 单调 clean<incomplete<corrupt；reopen
    前 durable 写 in_progress。
77. dataset complete 派生谓词（finalized ∧ clean ∧ 无孤儿 ∧ 全 complete ∧ 无 malformed ∧
    ≤256MiB ∧ 恒等式）。
78. 三层 complete 互不推导（snapshot/trace/dataset 各自独立用例）。
79. 字节语义：attempted = 完整 canonical JSON 字节；serialized = 实际写入行字节；stub 保留
    decisions+元数据+计数、丢 observations/dead_ends。
80. segment 轮转：16MiB append 前预判；编号 000000；reopen 取最高段续写；segment_count
    含空段。
81. 单行容量断言：MAX_TRACE_BYTES(2MiB) < MAX_SEGMENT_BYTES(16MiB) → 合法 trace 恒装入
    单段。
82. partial tail：仅最后段最后行可 partial；reopen 截断至最后一个 '\n' + fsync；非最后段
    partial → corrupt。
83. 计数恒等式：started = written + failed；written = complete + excluded；excluded ≤
    written；duplicate trace_id → corrupt。
84. snapshot_unavailable = excluded stub（非 failed）；cycle_started append 失败 → v9 双层
    处理：recorder_dirty + durable 降级（finalize 禁止 finalized+clean；崩溃由 unclean
    reopen 接管）。
85. EventBus 选边：v1 sidecar-only（无 _emit 调用、无 CycleTraceSummary、无 UI reducer
    改动——静态断言）。
86. RouteEnergy universe：raw_fact_count = 排除后普查（challenged 计入、retired 除外）；
    correlation_group_count = 实际合并组数；dead-end-only / captured-only route 输出
    positive=0、energy=0、eligible=False；eligible := positive>0；witness 用
    bool(witness.strip())。

**87-105 v9 新增（P0/P1 全覆盖）**
87. unclean reopen：in_progress + clean → reopen 后 data_quality=incomplete（sticky）+ 原因
    unclean_reopen；在任何 cycle/capture/dispatch 前完成。
88. 合法 resume：finalized + clean → 回 in_progress 不自动降级（quality 保持 clean）。
89. unclean reopen 后正常 finalize → 仍非 complete（quality incomplete sticky，只能写
    finalized + incomplete）。
90. cycle_started 零字节前 append 失败、进程正常 finalize → recorder_dirty → 禁止
    finalized + clean。
91. append 失败后立即崩溃 → 下次 reopen 走 unclean_reopen → 永久 incomplete。
92. append 写出 partial tail 后失败 → reopen 截断 + unclean 降级（两条路径均非 complete）。
93. manifest 降级写入本身失败 → recorder_dirty 保持 → finalize 不得写 finalized+clean。
94. 固定点收敛：serialized_bytes 999→1000、9999→10000 位数边界迭代稳定，且 == 实际行字节。
95. canonical record 含 kind envelope：2MiB 判定/segment 预判/run-total 用同一计量对象。
96. 临界值：MAX−1/MAX/MAX+1 三态；stub serialized_bytes == 真实 UTF-8 行字节（断言）。
97. 非 ASCII/中文字段按 UTF-8 字节计量（len(s.encode("utf-8"))，非字符数）。
98. manifest 写协议：temp → flush → fsync → os.replace；POSIX 尽力 fsync parent；失败保留旧
    manifest + recorder_dirty；Windows 目录 fsync 不保证（如实声明）。
99. manifest 缺失/损坏但 segment 存在 → 重扫重建计数，quality ≥ incomplete，不得重建为 clean。
100. 跨 segment 配对：全 run 按段号升序全局折叠后才算计数；cycle_started 与 cycle_trace
    跨段是合法状态。
101. 分类：duplicate started → corrupt；duplicate trace → corrupt；trace-without-started →
    corrupt；orphan started → incomplete。
102. cycle_started 参与 16MiB 容量预判；total_trace_bytes 只累计 cycle_trace/stub。
103. energy_trace_enabled=False → 零副作用（不建目录/连接/trace_id/canonical JSON/fsync）+
    dispatch 逐决策等价。
104. disabled resume 自 finalized+clean 起步（v9.1 重写）：resume guard 不受开关控制 →
    翻转 in_progress + quality ≥ incomplete + reason resume_without_energy_trace →
    永非 complete（原"自保护停在 in_progress"表述作废——那需要 resume 前就是 in_progress）。
105. fact 计数三拆：超限 stub captured_fact_count>0 ∧ stored_fact_count==0 ∧
    len(observations)==0；完整 trace stored==captured。

**106-118 v9.1 新增**
106. cycle_trace 零字节前 append 失败 → dispatch 仍发生 + recorder_dirty + 内存质量 ≥
    incomplete + 原因 cycle_trace_append_failed（异常不外传）。
107. cycle_trace 写出 partial tail 后 fsync 失败 → dispatch 仍发生；reopen 截断 +
    orphan started → sticky incomplete。
108. cycle_trace 失败后正常 finalize → 禁止 finalized+clean。
109. cycle_trace 失败后崩溃重开 → orphan started → sticky incomplete。
110. resume_epoch 先写单路径（v9.2 修订）：append 失败 → fail-fast（清晰报错、无 dispatch、
    可重试）；append 成功 + manifest 翻转失败 → 仍继续（witness 已 durable）。
111. 翻转失败仍继续（v9.2 修订）：resume_epoch 已落盘 → 恢复折叠 last_resume_epoch_id 未被
    ack → 非 complete（epoch id 判定，禁用墙钟）。
112. guard 前崩溃序：resume 启动后、guard 完成前崩溃 → 无 dispatch 发生，旧
    finalized+clean 数据集仍真实（不产生未采集派发）。
113. enabled 合法 resume 不自动降级（finalized+clean → in_progress+clean，88 语义强化）。
114. snapshot.complete 不含大小上限：大小只在 trace 层判定（canonical bytes ≤
    MAX_TRACE_BYTES；契约断言）。
115. 固定点迭代上限 8：超限 → complete=False + size_fixed_point_failed + 质量 ≥
    incomplete + 继续 dispatch。
116. 测试所有权：M7-0 测试收集不包含 M7-1/M7-2 条目（模块边界断言；M7-0 独立绿）。
117. M7-2 报告确定性：同一数据集两次 replay → 指标逐位一致（seed=20260816 固定）。
118. M7-2 两段归因：production→planner 段差异不计入 energy 指标（数值断言）。

**119-126 v9.2 新增**
119. 时钟回退回归：旧 finalized_ts(2000) > 新 resume_ts(1000) 但 finalized_resume_epoch_id
    未 ack 新 epoch → 数据集非 complete（P0 回归，墙钟不参与判定）。
120. resume_epoch 唯一身份 UUID4；duplicate 内容一致 → 幂等折叠；同 id 内容不一致 → corrupt。
121. malformed resume_epoch → corrupt（同中间 malformed 规则）。
122. resume_epoch 参与 16MiB append 前预判；不计入 total_trace_bytes。
123. last_resume_epoch_id 按物理 append 顺序（segment 号 + 行序）确定，禁止按 resume_ts 排序。
124. manifest 重建恢复 last_resume_epoch_id；complete 要求
    finalized_resume_epoch_id == last（无 resume 时两侧均 ""）。
125. live HITL pause→resume（同实例）：不写 resume_epoch、不改 quality/lifecycle。
126. 多次 resume：E1 ack 后再 resume E2 → complete 只在 ack E2 后成立（顺序回归）。
127. enabled resume + prior incomplete（终审 P1-2 回归）：quality 保持 incomplete、保留
    历史原因、**不**新增 resume_without_energy_trace。

### 实施顺序（v9.1 测试所有权重分）

```
M7-0（类型模型 + trace_id + 有界只读捕获（读 VIEW + promotion_ts JOIN）+ dead-end applied
      结论 + 两阶段 sidecar + resume guard（epoch ack）+ manifest 两维状态机 + scheduler
      顺序 + 测试 23-39/45-51/53-68/74-116/119-127）→ 全量绿（当时存在的全部测试）
M7-1（EnergyConfig/route_energies/reorder_decisions 三序列 + 测试 1-22/69-73）→ 定向测试
M7-2（replay/paired bootstrap/N/A 纪律报告 + 测试 40-44/52/117-118）→ 全量绿
每阶段完成后跑当时存在的全量测试；M7-0 不要求提前实现 M7-1/M7-2 的测试目标。
```

在线 `DSWARM_ENERGY_TIEBREAK` 接线 = 独立在线 RFC（M7-2 报告达标后另审）。

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
