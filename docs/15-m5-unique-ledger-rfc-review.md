# M5 唯一账本 RFC v1 评审反馈

> 评审对象：[docs/14-m5-unique-ledger-rfc.md](14-m5-unique-ledger-rfc.md)  
> 上位方案：[docs/10-v4-kernel-improvement-implementation.md](10-v4-kernel-improvement-implementation.md) M5  
> 评审日期：2026-08-15  
> 裁决：**Design Review Required / 暂不批准实施**

## 0. 总结论

RFC v1 已修正旧稿中最危险的几项方向性问题：不在 worker 结束时二次结算、把 unknown 当状态、预算只阻止未来 spawn、预算动作可重建、预算告警与 provider 故障分离。这些原则应保留。

但 RFC 的两个基础前提与当前代码不一致：

1. `CliSolver._stream_cost(CliResult)` 是一次 **CLI invocation 的汇总结算点**，不是一次 provider billable call 的唯一入口；一个 `pi --mode json` agentic loop 可以向上游发起多次 `/chat/completions`。
2. 仓库已经存在 `sessions/<run_id>-gateway-usage.jsonl`，并被 `eval_nyu/runner.py` 明确视为 Pi worker 真实上游 usage 的 authoritative ledger。RFC 不能在“不建第二账本”的同时忽略这本现有账本。

此外，内部 Reason/协调器 LLM 仍走 `LLMClient._record_cost() -> CostController.record()`，没有进入 RFC 的 `kind="usage"` 重建协议；`usage_id` 在进程重启后会碰撞；ModelGateway 只有 run 级身份，无法准确投影 profile/account；复用 `COST_UPDATE` 会破坏 Web/TUI/BTW 的现有累计摘要契约；EventBus 吞掉 sink 异常，无法支撑 RFC 当前承诺的强 durable/rebuild 语义。

因此 M5 仍应保持 **No-Go**。应先出 RFC v2，定稿 canonical usage 粒度、producer、身份、持久化边界与预算 scope，再开始施工。

## 1. 本轮核验范围与基线

本轮直接核对了：

- `dswarm/solver/cli_solver.py::_stream_cost`
- `dswarm/solver/cli_driver.py::CliResult`、`PiDriver.parse`
- `dswarm/solver/modelgateway.py`
- `dswarm/core/cost.py::CostController`
- `dswarm/core/llm.py::_record_cost`
- `dswarm/core/event_bus.py::EventBus.emit`
- `dswarm/core/session_store.py::SessionStore`
- `dswarm/swarm/worker_runtime_mixin.py`
- `apps/web/run_manager.py`
- Web/TUI/BTW 的 `COST_UPDATE` 消费者
- `eval_nyu/runner.py::_gateway_usage_summary`
- 相关测试：`tests/test_modelgateway.py`、`tests/test_cli_executor.py`、`tests/test_cost.py`

基线验证：2026-08-15 在当前工作区运行 `uv run pytest -q`，退出码为 0（4 个依赖弃用 warning）。本评审只修改文档，不实施 M5。

## 2. 已认可、应原样保留的设计原则

### 2.1 禁止 worker 结束后二次计费

RFC 拒绝在 `ReasonSwarm._one()` 根据 `SolveOutcome` 再做一次 charge，这一点正确。结果对象可以承载摘要，但不能成为同一上游 usage 的第二个结算来源。

### 2.2 unknown 不是 0 token

`unknown_calls`/`token_status` 必须独立表达；缺 usage 的调用不能伪装为 `input_tokens=0, output_tokens=0`。USD 已知、token 未知时，应允许“美元已知 + token unknown”的组合状态。

### 2.3 预算 cap 不强杀运行中 worker

达到 profile/account cap 后只阻止后续 spawn；运行中的 single-shot worker允许完成。`stop`、生命周期收尾、finalize、事件持久化不得经过预算门。

### 2.4 预算解除必须是显式 durable action

普通 HITL `resume` 不应静默移除 budget blocker。`raise_cap`、明确 override 等动作必须写入可重建事件，并保留 actor、scope、旧值/新值或增量。

### 2.5 专用预算事件

`BUDGET_ALERT`/`BUDGET_ACTION` 与 `PROVIDER_BATCH_ALERT` 分离是正确边界。预算耗尽是策略状态，provider 批量错误是运行故障，两者不可复用同一告警协议。

## 3. 阻断级问题

### 3.1 阻断 A：`_stream_cost()` 不是“一次可计费调用”的真实唯一入口

RFC §1/§3 将以下关系视为等价：

```text
一次 CliResult == 一次可计费 provider call
```

代码不支持这个等价关系：

- `CliResult` 的类型注释是 “One CLI run's outcome”（`cli_driver.py:173-185`）。
- `PiDriver` 启动的是完整 `pi --mode json` agentic loop（`cli_driver.py:426-482`），该 loop 可以经历多个 assistant/tool turn。
- `PiDriver.parse()` 扫完整 stdout 后只返回一个 `CliResult`，并且对多个 usage event 采取“保留最后一个值”，不是逐调用产出 usage（`cli_driver.py:603-653`）。
- ModelGateway 每次收到 `/chat/completions` POST 都独立转发并在响应完成时调用 `_record_usage()`（`modelgateway.py:108-123, 213-251, 256-305`）。`tests/test_modelgateway.py::test_usage_recorded_per_run` 已验证同一 token、同一 run 的两个 POST 会进入 usage ledger。

所以 `_stream_cost()` 最多是 CLI invocation 级聚合/兼容入口，不能在未证明 Pi usage 语义前被定义为 provider-call canonical producer。

**RFC v2 必须先选边：**

- 若 `usage_id` 的定义坚持为“一次 provider billable call”，canonical producer 应位于 ModelGateway/provider adapter 附近；
- 若第一版只做“一次 CLI invocation 聚合”，必须改名并缩小承诺，不能声称对 provider retry/多 turn 精确计费。

推荐选择第一种。

### 3.2 阻断 B：当前已经存在第二本真实 usage 账本

`ModelGateway._record_usage()` 当前写入：

```text
sessions/<run_id>-gateway-usage.jsonl
```

证据：

- `modelgateway.py:307-349`
- `tests/test_modelgateway.py:161-178`
- `eval_nyu/runner.py:286-312`

尤其 `eval_nyu/runner.py` 明确写道 gateway ledger 对 Pi worker 的真实上游 usage 是 authoritative，而协调器的 `CostController` 只看到自己的调用。

因此 RFC 的“不建第二账本”不能只解释为“不新增一个文件”；它必须处理**现有双源事实**。RFC v2 必须选择并写清迁移：

1. **推荐：**ModelGateway usage 转成 SessionStore 中的 canonical `USAGE_RECORDED`；gateway JSONL 仅保留为可删除的兼容 telemetry，并同步修改 eval；
2. 或 gateway JSONL 作为 write-ahead canonical source，再可靠地桥接为 SessionStore event，定义幂等和 crash recovery；
3. 或明确 M5 不覆盖 Pi provider-call usage，但此时不能称“唯一账本”。

在迁移完成前，不得让 `_stream_cost()` 与 gateway usage 同时写 canonical charge，否则会双计。

### 3.3 阻断 C：内部 LLM 计费链未纳入“唯一账本”

系统还有独立的真实计费入口：

```text
LLMClient._record_cost()
  -> CostController.record()
  -> global/challenge/solver ledger
  -> COST_UPDATE
```

证据：`dswarm/core/llm.py:195-204`，调用发生于 non-stream、stream 等响应路径；`dswarm/core/cost.py:137-183` 更新现有 ledger。

RFC 的 `rebuild_ledger(events)` 只折叠 `kind="usage"`，但施工清单只准备收敛 `add_external_usd`。这会导致：

- 在线 ledger 包含 Reason/协调器内部 LLM 成本；
- replay ledger 只包含 worker usage；
- RFC 测试 6“在线与 replay 逐字段相等”无法成立。

RFC v2 必须二选一：

- **推荐：**内部 LLM 与 worker/provider usage 全部产出同一 `UsageRecord` 协议，只是 `producer` 不同；
- 或把 M5 明确更名/缩小为“worker token budget ledger”，并放弃“系统唯一成本账本”的声明。

### 3.4 阻断 D：当前 usage_id 在 run 恢复/进程重启后会碰撞

RFC 使用：

```text
usage::{run_id}::{solver_id}::{charge_seq}
```

但现有 `solver_id` 由 `_label_seq` 生成（`worker_runtime_mixin.py:610-619`），计数只属于当前 Swarm 实例。backend restart/reopen 后，同一 run 可以再次出现 `cli-pi`、`cli-pi-2`；RFC 的 `charge_seq` 也会从头开始。

因此新调用可能错误复用历史 usage_id，被 replay dedupe 当成重复而丢弃真实费用。

v2 需要持久稳定的身份，例如：

```text
worker_instance_id = UUID/ULID（spawn 时生成并写入 durable lifecycle/dispatch event）
provider_call_id    = gateway/provider request 的稳定唯一 id
usage_id            = usage::<run_id>::<producer>::<provider_call_id>
```

UI 展示用 `solver_id` 不能兼任账本幂等主键。

### 3.5 阻断 E：ModelGateway 缺少 worker/profile/account 身份，且当前固定读取 pi-main

ModelGateway 目前的 task token 映射是：

```python
_tokens: token -> run_id
_runs: run_id -> token
```

见 `modelgateway.py:145-155, 184-210`。同一 run 的 container workers 共用 run token，usage row 只有 `ts/run_id/usage`。

同时 `_real_api_key()` 固定读取 `<account_root>/pi-main/API_KEY`（`modelgateway.py:53-67`），没有按当前 worker profile 的 `credential_account` 选择凭据。

这意味着 provider-call 级 usage 当前无法准确归属到：

- worker_instance_id
- solver_id
- profile_id
- account_id
- provider/model

RFC §6 仅把 profile/account 传给 `CliSolver`，只能给 CLI invocation 汇总归属，不能解决 Gateway 内多次实际 provider call 的归属与凭据选择。

v2 应把 task token 改为至少绑定：

```text
token -> run_id + worker_instance_id + solver_id + profile_id + account_id
```

并由该绑定选择 credential account、生成 usage identity、写 canonical record。不能信任 container 请求体自报 profile/account。

### 3.6 阻断 F：复用 `COST_UPDATE` 会破坏现有累计摘要契约

当前消费者把每个 `COST_UPDATE` 当作**累计 projection**：

- Web reducer：`apps/web/ui/lib/events.ts:1725-1742`
- TUI：`apps/tui/app.py:129-138`
- BTW durable scan：`dswarm/solver/btw.py:562-570`

RFC 计划让同一 EventType 同时承载：

- `kind="usage"`：单次增量 canonical record
- `kind="summary"`：累计摘要

未同时升级所有消费者时，单次 usage 会被当作累计值；若同时发 usage 与 summary，BTW 还会把两种形态相加，产生重复/膨胀统计。旧 session 又没有 `kind` 字段，需要兼容解释。

**建议定稿为新事件类型：**

```python
EventType.USAGE_RECORDED   # canonical 单次 usage，不给旧 UI reducer直接消费
EventType.COST_UPDATE      # 保持累计 summary projection 语义
```

新事件类型仍走同一个 EventBus/SessionStore，不等于建立“第二本账”。

兼容规则至少包括：

- ledger reducer 只消费 `USAGE_RECORDED`；
- Web/TUI/BTW headline 只消费 `COST_UPDATE`；
- 无 `kind` 的历史 `COST_UPDATE` 按 legacy summary 解释；
- 定义一次 usage 后发哪些 scope 的 summary，以及顺序；
- replay canonical usage 时不得向在线 UI 重放出重复 summary。

### 3.7 阻断 G：Pi usage 是 delta 还是累计值尚未证明

`PiDriver.parse()` 对多个 usage event 的实现是覆盖：

```python
if inp is not None: in_tok = inp
if outp is not None: out_tok = outp
```

当前测试只覆盖单个 usage event，没有覆盖：

- 单次 CLI invocation 内多轮 completion；
- resume 同一 session；
- provider retry；
- usage counter 累计、重置或回退。

即使 usage_id 完全幂等，如果数值本身是 session cumulative，continuation 时再次累加仍会重复计费。

v2 必须要求 adapter/gateway fixture 证明 canonical record 是 provider-call delta；未证明前，`_stream_cost` 只能作为 `token_status=estimated/aggregate` 的兼容 fallback。

### 3.8 阻断 H：EventBus 当前不提供“canonical ledger 已持久化”的强确认

`EventBus.emit()` 的顺序虽然是 sink 先于 fan-out，但 sink exception 会被吞掉（`event_bus.py:49-65`）。因此可能发生：

1. 内存 ledger 已更新；
2. SessionStore append 失败；
3. EventBus 吞掉异常并继续 fan-out；
4. UI 看见 charge；
5. 进程重启后 replay 丢失该 charge。

这与 RFC 的“gate 可由 durable event 完全重建”“在线与 replay 逐字段相等”是冲突的。

v2 必须定义 crash/persistence boundary：

- canonical append 成功后再更新 projection，还是 append 失败则整个 charge 失败；
- 调用方如何获得 durable acknowledgement；
- duplicate retry 如何安全；
- JSONL partial line/进程崩溃如何恢复。

若不允许修改 event spine，可增加一个账本专用的 acknowledged append API，再把成功后的 canonical event fan-out；否则必须把承诺降级为 best-effort，不能叫 durable unique ledger。

## 4. 高优先级但可随 v2 一次定稿的问题

### 4.1 `record_usage_once()` 的原子顺序与并发模型

必须明确以下步骤是否属于一个临界区：

1. 检查 usage_id；
2. durable append；
3. 标记 dedupe；
4. 更新五层 projection；
5. 计算 warn/cap crossing；
6. 写 alert/action projection；
7. 发 UI summary。

若 producer 位于 `ThreadingHTTPServer` 的 ModelGateway，不能直接假设 asyncio 单线程串行；需要 thread-safe ingress 或投递回 owning event loop。

### 4.2 run-total 与 `reset_window` 不应同时作为 v1 正式 API

RFC 倾向第一版使用 run-total budget，却仍列出 `reset_window`。没有窗口起点、窗口长度、跨重启时间源和历史折叠规则时，该 action 无定义。

建议 M5 第一版只支持：

- `raise_cap`
- `override`（应含 scope、actor、reason，可选 expires_at）

窗口预算另立后续 RFC，再加入 `reset_window`。

### 4.3 profile/account 双 cap 是独立 blocker，不是“谁先触发”

建议契约：

- profile cap 与 account cap 独立计算；
- 任一 blocker 活跃就拒绝 spawn；
- 一次 usage 同时跨越两阈值时，各发一条有唯一 `alert_id` 的事件；
- `block_reason` 返回所有 active blockers；
- 解除 profile blocker 不得绕过仍活跃的 account blocker，反之亦然；
- UI 可聚合展示，但 canonical event 不丢 scope。

### 4.4 第一版重建范围应固定为 run-scoped

当前 `CostController` 是 per-run 构造（`run_manager.py:722-734`）。建议 M5 v1 明确：

- global/challenge/solver/profile/account 五层都表示“当前 run 内”的投影；
- account 不是跨 run 财务账本；
- 跨 run account quota 以后需要独立 durable account ledger/RFC，不能暗中从多个 session 文件扫描拼接。

### 4.5 RunManager 的 create/reopen 接入点尚未设计

`RunManager.create()` 当前总是新建空 `CostController(bus=bus)`；rehydrate 也通过 `create()` 构造空账本。`_fresh_bus()` 只重绑 `run.cost.bus`，不 rebuild ledger（`run_manager.py:722-750, 1385-1402`）。

v2 必须给出具体顺序：

```text
create/reopen
  -> replay canonical usage + budget action
  -> rebuild projection/gate
  -> rebind live bus
  -> 完成后才允许 scheduler submit/spawn
```

恢复未完成期间必须 fail closed，不能先开放派发再异步补账。

### 4.6 `unknown` 建议升级为三态

仅 `unknown: bool` 难以表达 adapter 聚合估算。推荐：

```text
token_status = known | estimated | unknown
```

并分别约束：

- known：provider-call delta，有精确 token；
- estimated：CLI invocation 聚合/价格反推等非 canonical 精度来源；
- unknown：无可信 token 数值，字段为 null。

预算是否允许 estimated 参与 cap 必须配置化并默认保守说明，不能静默当 known。

## 5. 推荐的 RFC v2 核心模型

### 5.1 Canonical UsageRecord

```python
@dataclass(frozen=True)
class UsageRecord:
    usage_id: str
    producer: Literal["internal_llm", "model_gateway", "cli_adapter"]
    run_id: str
    challenge_id: str | None
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    account_id: str | None
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    usd: float | None
    token_status: Literal["known", "estimated", "unknown"]
    timestamp: float
```

约束：

- usage_id 标识 producer 的一次可计费调用，不使用 UI label 作为唯一身份；
- 数值均为本调用 delta；
- `unknown` 不填 0；
- profile/account 是可信 spawn/token binding 派生，不由 worker 自报；
- provenance gate 与 evidence graph 不读取 usage 作为事实证据。

### 5.2 Producer 优先级与防重复

推荐：

1. 内部 LLM：`LLMClient._record_cost()` 每个完成的 HTTP completion 产生一条；
2. container Pi：ModelGateway 每个实际 upstream request 产生一条；
3. gateway 不可覆盖的 host/custom endpoint：经验证的 adapter 产生 provider-call delta；
4. `_stream_cost()` 只作为兼容 fallback，且必须通过 invocation/provider-call correlation 防止与 gateway 双计；fallback 记录为 `estimated` 或 `unknown`，不得覆盖更高优先级记录。

RFC 必须列出每种 runtime/profile 实际走哪个 producer，不允许同一次费用同时命中两个 producer。

### 5.3 Event 与 projection 分层

```text
USAGE_RECORDED（canonical immutable event）
  -> CostController projection
     global/challenge/solver/profile/account
  -> ProfileBudgetGate projection
  -> COST_UPDATE（UI累计摘要）
  -> BUDGET_ALERT（阈值 crossing）

BUDGET_ACTION（canonical operator action）
  -> ProfileBudgetGate projection
```

`COST_UPDATE` 不是账本事实，只是可重建 projection；删除后可由 `USAGE_RECORDED` 重算。

### 5.4 Budget blocker 模型

```text
active blockers:
  profile:<profile_id>
  account:<account_id>
```

spawn verdict 为所有 blockers 的合取；普通 resume 不修改 blockers；`raise_cap`/`override` 只影响明确 scope。

## 6. RFC v2 必须补充的测试矩阵

在 RFC v1 的 12 项基础上，至少新增：

1. 一个 Pi CLI invocation 内两个 upstream POST -> 两条不同 usage_id；
2. Gateway canonical usage 与 `_stream_cost` fallback 不双计；
3. internal LLM 与 worker usage 同时存在时，在线/replay 五层逐字段相等；
4. backend restart 后同一 run 新 worker 不与历史 usage_id 碰撞；
5. token binding 能区分同一 run 的两个 profile/account；
6. Gateway 使用绑定 account 的 credential，而非固定 pi-main；
7. Pi 多 usage event、resume、retry 的 delta/cumulative fixture；
8. SessionStore append 失败时，不出现“内存已计费但 durable 丢失”的成功状态；
9. duplicate durable retry 只计一次；
10. Web/TUI/BTW 不把 `USAGE_RECORDED` 当累计 `COST_UPDATE`；
11. legacy 无 kind 的 `COST_UPDATE` 仍按历史摘要显示；
12. profile 与 account 同时越线时产生两个独立 blocker/alert；
13. 只解除一个 blocker 仍拒绝 spawn；
14. create/reopen 在 rebuild 完成前不允许派发；
15. run-total 第一版不存在可调用的 `reset_window`；
16. unknown/estimated/known 三态的 UI、预算与 replay 行为；
17. gateway JSONL 迁移后 eval 读取 canonical source，结果与迁移前 fixture 等价；
18. 全量测试绿，provenance gate 与 anti-laundering 测试原样通过。

## 7. 对四个未决问题的裁决建议

### 7.1 事件类型

**选择新 `USAGE_RECORDED`。**事件流仍唯一，事件语义不重载；`COST_UPDATE` 保持累计 UI projection。

### 7.2 预算窗口

**第一版只做 run-total。**删除/推迟 `reset_window`，待真正引入窗口模型后再设计。

### 7.3 profile/account 双 cap

**独立 blocker，任一阻止 spawn；同时越线分别告警。**不采用“谁先触发”的单选模型。

### 7.4 rebuild 范围

**第一版仅 run 内。**字段与 UI 文案应明确为 run-scoped profile/account budget。

## 8. 最终裁决与 Go 条件

### 当前裁决

```text
M5 RFC v1：Design Review Required / 暂不批准实施
```

### RFC v2 获批前必须解决

- [ ] 定稿 usage 粒度为 provider billable call，或诚实缩小为 invocation aggregate；
- [ ] 处理现有 gateway usage ledger 的 canonical 迁移；
- [ ] 把内部 `CostController.record()` 链纳入统一协议，或缩小模块声明；
- [ ] 使用跨重启稳定的 worker/provider call identity；
- [ ] 完成 Gateway 的 worker/profile/account 身份传播与 account credential 选择；
- [ ] `USAGE_RECORDED` 与 `COST_UPDATE` 语义分离；
- [ ] 证明 Pi usage 的 delta/cumulative 行为；
- [ ] 定义 durable acknowledgement、幂等和 crash boundary；
- [ ] 定稿 run-scoped、run-total、双 blocker 语义；
- [ ] 给出 RunManager create/reopen 的同步 rebuild 接入点；
- [ ] 用扩展测试矩阵覆盖 producer 防重复与恢复路径。

满足以上条件后，M5 才可进入实现计划。M5 实施期间不得修改 provenance gate、anti-laundering 或把 usage/预算状态写入 evidence 作为事实来源。
