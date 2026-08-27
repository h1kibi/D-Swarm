> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../00-architecture-spec.md) 取代；本文保留作为时代记录。

# M5 唯一账本 RFC v4 第四轮评审反馈

> 评审对象：[docs/14-m5-unique-ledger-rfc.md](14-m5-unique-ledger-rfc.md)
> 上位方案：[docs/10-v4-kernel-improvement-implementation.md](../10-v4-kernel-improvement-implementation.md) M5
> 前轮评审：[docs/17-m5-unique-ledger-rfc-v3-review.md](17-m5-unique-ledger-rfc-v3-review.md)
> 评审日期：2026-08-15
> 裁决：**Architecture Accepted / v4.1 Spec Amendment Required / 暂不实施**

## 0. 总结论

RFC v4 已经把第三轮提出的五个架构阻断按正确方向闭环：

- Gateway 改为 `call_started -> upstream -> call_finished -> canonical` 两阶段日志；
- `emit_checked` 将 critical persistence 放到 ring/fan-out 之前；
- provider call 与 fallback invocation 使用不同 identity；
- `ledger_state` 增加 `rebuilding | ready | failed`；
- internal LLM 引入 `UsageContext + UsageWriter`，BTW 按真实传输路径分类。

这些选择本轮**认可，不要求 v5 再推翻**。但 v4 仍不能直接进入实现，原因不是总体架构错误，而是实现级契约尚有五个必须在文档中闭合的问题：

1. v4 覆盖写掉了 v3 的已认可细节，却只用一行“保留不变”代替；当前文档不再是自包含、可施工的唯一规范，且宣称保留的 18 项测试并未列出；
2. `call_finished(status=...|error)` 与 `UsageRecord.status=measured|estimated|unknown` 自相矛盾，调用结果和 usage 完整度被混成一个字段；
3. internal producer 的 durable `call_started` 没有实际载体；Gateway 有 WAL，但 internal 既没有 WAL 协议，也没有 canonical started 事件；
4. `critical durable success` 与当前 `SessionStore.append()` 不做 `fsync` 的事实不一致，同时“既有 emit 行为不变”和“critical sink 先于 ring”没有给出可同时成立的精确算法；
5. 上游已经计费后，`call_finished` WAL 成功但 canonical append 失败时的在线状态、streaming 响应和恢复行为未定义；fallback retry 的 `invocation_id` 也没有稳定携带点。

因此本轮裁决为：

```text
M5 RFC v4：总体架构通过；需要 v4.1 文档修订后再 Go。
在 v4.1 闭合以下契约前，不实施 M5。
```

v4.1 应是一次**局部、确定性的规范补全**，不需要再重做 M5 架构。

## 1. 本轮代码核验范围

本轮重新核对了：

- `dswarm/core/event_bus.py::EventBus.emit/add_sink/remove_sink`
- `dswarm/core/session_store.py::SessionStore.append/sink/replay`
- `dswarm/core/events.py::Event/EventType`
- `dswarm/core/llm.py::LLMClient.chat/_chat_once/_chat_stream/_record_cost`
- `dswarm/solver/modelgateway.py::ModelGateway.issue/proxy/_proxy_json/_proxy_stream/_record_usage`
- `dswarm/solver/cli_driver.py::CliResult`
- `dswarm/solver/cli_solver.py::_stream_cost`
- `dswarm/swarm/worker_runtime_mixin.py::_ensure_container/_runtime_env_for/_make_cli_worker`
- `apps/web/run_scheduler.py::RunScheduler.submit/next_to_dispatch`
- `apps/web/run_manager.py::_launch/_fill_slots/_fresh_bus/_ensure_standby/resolve`
- `apps/web/routes/btw.py` 的 container BTW Gateway 路径
- `tests/test_event_bus.py` 当前 sink isolation 与 fan-out 回归契约
- RFC v1-v3 评审记录 `docs/15`、`docs/16`、`docs/17`

测试基线将在本评审文档更新后重新执行，结果记录于 §9。

## 2. 第三轮五项阻断的关闭情况

### 2.1 A：两阶段 WAL——架构通过

v4 的顺序在时间上可执行：

```text
claims snapshot
-> provider_call_id
-> call_started + fsync
-> upstream
-> call_finished + fsync
-> USAGE_RECORDED
```

只有 started、没有 terminal 的记录折叠为 unknown，token/USD 使用 `None` 而不是 0；preflight WAL 失败不调用上游并返回 503，也符合 fail-closed 要求。

该方向通过。仍需补的是 internal producer 如何使用同一恢复协议，见 §5。

### 2.2 B：commit point——架构方向通过

v4 明确未持久化事件不能进入 ring/subscriber，允许 seq 空洞，并规定 retry 使用新 Event、新 seq、同 usage_id。这解决了第三轮的可见性和对象复用问题。

但“durable”的具体实现仍与当前 SessionStore 不匹配，见 §6。

### 2.3 C：Usage identity——主体通过

以下区分正确：

```text
provider call:
usage::<run_id>::<producer>::<provider_call_id>

fallback aggregate:
usage::<run_id>::fallback::<invocation_id>
```

claims 入口快照、streaming 中 revoke 不改变在途调用归属、active token hard cap 且不 LRU 驱逐，也均通过。

fallback 的 invocation_id 必须在一次 CLI invocation 开始时生成并随结果携带，不能在 `_stream_cost()` 每次执行时临时重新计算，见 §8.1。

### 2.4 D：ledger_state——状态模型通过

`rebuilding | ready | failed`、有界等待、failed 拒绝新 spawn、stop/finalize 永远可用，方向正确。

但 helper 的所有权和注入路径仍需写成接口；当前 Swarm 内部 Worker 工厂无法直接访问 RunManager helper，见 §8.2。

### 2.5 E：internal producer 与 BTW 分类——方向通过

v4 正确识别：

```text
container BTW -> gateway / token_scope=btw
host LLMClient BTW -> internal
non-gateway Pi invocation -> fallback aggregate
```

`UsageContext + UsageWriter` 也是正确的依赖注入边界。剩余问题是 internal 的 started/terminal 究竟写到哪个 durable journal，见 §5。

## 3. 阻断 1：v4 不是自包含实施规范，已认可契约被覆盖丢失

v4 §0 仅列出：

```text
Per-worker token；configured/billing 双账户；acknowledged bridge；
COST_UPDATE compatibility；reconciliation；budget actions...
```

但当前 `docs/14` 已被覆盖为 v4，仓库中不存在一份仍保留 v3 完整正文的权威规范。`docs/17` 是评审意见，不是 v3 的完整接口定义。

当前 v4 中没有重新定义或稳定引用：

- `WorkerClaims` 字段和 `issue_worker/claims_for_token/revoke_token/revoke_worker/revoke_run` API；
- per-worker token 如何从当前 `_ensure_container()` 的 run 级 `_gateway_token` 移到每个 Worker 的 exec env；
- Gateway 线程桥的 `run_coroutine_threadsafe + future.result(timeout)`、inflight、backpressure、drain 和 loop-close 行为；
- producer 互斥矩阵以及 Gateway worker 禁止 `_stream_cost` fallback 二次计费的判定字段；
- configured/billing account 的解析责任；
- WAL reconciliation 到 canonical event、projection、CostController、profile/account blocker 的完整顺序；
- `BUDGET_ACTION` 的 `raise_cap/override` durable 语义；
- replay 后在线 projection 与预算 blocker 的逐字段等价条件。

### 3.1 测试矩阵也不完整

v4 写“v3 18 项保留 + 新增 12 项”，但正文只列了 19-30。由于 v3 正文已被覆盖，实施者无法从当前权威 RFC 得到 1-18 的确切版本。

### 3.2 v4.1 必须做的修订

二选一，推荐第一种：

1. **推荐：把所有已认可契约恢复进 `docs/14` 的附录**，使 v4.1 自包含；
2. 或把旧 v3 正文恢复成独立、不可变的 `docs/14a-...-v3.md`，然后 v4.1 对每个继承章节做精确锚点引用。

最终 RFC 必须完整列出最终测试矩阵；不要只写“保留前 18 项”。测试总数可以超过 30，准确性优先于维持数字。

## 4. 阻断 2：`status` 字段把调用结果与 usage 完整度混在一起

v4 §1 定义：

```text
call_finished(status=measured|unknown|error)
```

但 §3 的 `UsageRecord.status` 只允许：

```text
measured | estimated | unknown
```

这不是文字小错。provider 调用可能：

- HTTP/provider error，但仍返回可计费 usage；
- 请求成功，但 provider 不返回 usage；
- streaming 中断，调用结果未知且 usage 未知；
- fallback invocation 有 estimated usage，但 CLI 本身失败或超时。

一个字段无法同时表达“调用是否成功”和“费用数据是否完整”。

### 4.1 v4.1 推荐定稿

```python
call_outcome: str   # succeeded | provider_error | transport_error |
                    # timeout | cancelled | interrupted
usage_status: str   # measured | estimated | unknown
```

`UsageRecord` 是 terminal 记录，不应把 `started` 放进 `usage_status`。

示例：

```text
HTTP 429，无 usage       -> call_outcome=provider_error, usage_status=unknown
HTTP 500，带 usage       -> call_outcome=provider_error, usage_status=measured
stream client disconnect -> call_outcome=interrupted, usage_status=unknown
fallback timeout 有汇总  -> call_outcome=timeout, usage_status=estimated
```

预算投影只根据 token/USD 是否存在和 `usage_status` 计算；错误韧性与告警根据 `call_outcome` 计算。两者不能互相覆盖。

## 5. 阻断 3：internal producer 没有 durable `call_started` 的实际载体

Gateway 的 started/finished 可以写 Gateway WAL，但 v4 对 internal 只写：

```text
provider_call_id -> durable call_started（经 checked 路径）
-> request -> terminal -> checked canonical append
```

当前 EventBus 只有完整 `Event`；RFC 没有定义：

- `call_started` 是哪一种 EventType；
- 如果使用 `USAGE_RECORDED`，projection 如何区分 started 与 terminal；
- 如果不是 canonical event，internal WAL 的路径、锁、fsync 和 reopen reconciliation 是什么；
- internal started 成功、进程崩溃、terminal 未写时如何恢复成 unknown。

### 5.1 v4.1 推荐选择：统一 `UsageJournal`

不要为 internal 再发明第三套持久化。建议定稿一个 run-scoped append-only `UsageJournal`：

```python
append_started(call_identity, claims_snapshot) -> None   # flush + fsync
append_finished(call_identity, outcome, usage) -> None   # flush + fsync
reconcile(run_id) -> list[UsageRecord]
```

- Gateway 线程通过同步、加锁的 journal API 写入；
- host `LLMClient` 通过 async writer adapter 调用同一个 journal；
- journal started/finished 都以 provider_call_id 幂等折叠；
- canonical `USAGE_RECORDED` 只表达 terminal UsageRecord；
- 只有 started 的记录在 reconcile 时生成 unknown terminal；
- fallback aggregate 可以直接走 checked canonical，或也走 journal，但必须只选一种并写清楚。

这样 `USAGE_RECORDED` 仍是唯一 canonical ledger，journal 只是 crash-recovery source，不会成为第二套业务账本。

## 6. 阻断 4：`critical durable success` 尚未对应真实 durability 和兼容算法

### 6.1 当前 SessionStore 不是 fsync durability

`SessionStore.append()` 当前是：

```python
with path.open("a", encoding="utf-8") as f:
    f.write(line)
```

文件 close 会刷新 Python/OS 缓冲，但没有 `os.fsync()`。因此 RFC 不能同时声称：

```text
Gateway WAL: flush + fsync
canonical commit point: SessionStore.append durable success
```

如果 canonical event 在断电/进程崩溃模型下必须作为 commit point，checked 路径也需要明确的 fsync 语义。

### 6.2 `emit()` 兼容描述仍有矛盾

当前 `EventBus.emit()` 和 `tests/test_event_bus.py::test_sink_exception_does_not_block_fanout` 的契约是：

```text
ring.append -> all sinks best-effort -> fan-out
raising sink 不阻断后续 sink 和 subscriber
```

v4 又规定 critical sink 必须先于 ring，但同时写“既有 emit() 行为不变”“emit_checked 与 emit 共享锁与顺序”。三句话无法同时严格成立。

### 6.3 v4.1 推荐精确算法

保留两个明确入口：

```text
emit(event):
  lock -> seq -> ring -> all sinks best-effort -> fan-out
  # 保持现有兼容行为

emit_checked(event):
  lock -> seq -> checked critical append+fsync
  failure: no ring, no non-critical sinks, no fan-out, raise
  success: ring -> non-critical sinks best-effort -> fan-out
```

实现上可以共享 seq 分配、锁和 fan-out helper，但不应声称两个入口拥有完全相同的 sink 顺序。

SessionStore 应增加显式 checked API，例如：

```python
async def append_checked(event: Event) -> None:
    # per-run lock, write, flush, os.fsync
```

`RunManager.create()` 和 `_fresh_bus()` 必须注册：

- 普通 `emit()` 使用的 best-effort store sink；
- `emit_checked()` 使用的 checked/fsync store sink；
- 不能让同一 checked event 被两个 store sink 重复追加。

测试除了“critical 失败不可见”，还要断言 checked path 真正调用 flush/fsync，并保留当前 normal emit 的 sink isolation 回归。

## 7. 阻断 5：上游已计费后的 canonical 失败状态未定义

存在合法崩溃窗口：

```text
call_started WAL 成功
-> upstream 已执行并可能已向 Worker streaming
-> call_finished WAL 成功
-> emit_checked(USAGE_RECORDED) 失败
```

此时费用已经发生，terminal WAL 可恢复，但在线 ledger 尚未更新。Gateway 的 response headers 甚至可能已经发送，不能再返回 503。

v4 目前只规定 preflight started 失败的 503，没有规定这个 postflight failure。

### 7.1 v4.1 必须定稿

推荐：

```text
terminal WAL 成功 + canonical 失败
-> terminal WAL 保持 recovery truth
-> ledger_state 立即转 failed
-> 阻止新的 provider call / Worker spawn
-> 记录 ledger_error=canonical_append_failed
-> 尝试用户可见告警；若 EventBus/SessionStore 本身不可用，至少结构化日志 + API 状态可见
-> 当前已经开始的 streaming 响应不得伪装成 503
-> rebuild/reconcile 以相同 usage_id 补写 canonical，成功后才可回 ready
```

对 host internal 调用也必须定义：业务调用本身成功但 accounting postflight 失败时，是返回业务结果并 fail-stop 后续工作，还是向调用方抛 accounting error。推荐前者加 run fail-stop，避免把已经产生的结果和费用伪装成“请求未发生”；但无论选择哪种，都必须测试锁定。

### 7.2 告警通道不能只依赖失败的持久化链

若 canonical SessionStore 正在失败，`BUDGET_ALERT` 本身也可能无法 durable。用户反馈至少需要双通道：

- EventBus 可用时发送告警；
- `ledger_state/ledger_error` snapshot API 与结构化日志始终可查询。

## 8. 两个必须补齐的实施接口

### 8.1 fallback `invocation_id` 必须随 invocation 稳定携带

v4 当前写：

```text
invocation_id = worker_instance_id + 该 CliResult 的调用序号
```

当前 `CliResult` 没有 invocation identity，`_stream_cost()` 也会吞掉 cost 异常。如果 checked append 失败后重新执行 settlement，而序号在 `_stream_cost()` 内重新递增，会生成不同 usage_id，导致重复计费。

v4.1 应规定：

- invocation_id 在 `_run_streaming()`/CLI invocation **开始前**生成；
- 随 `CliResult` 或独立 settlement envelope 返回；
- 同一结果的所有 durable retry 复用同一个 invocation_id；
- backend restart 后新 invocation 使用新的 worker_instance_id/UUID，不与历史碰撞。

### 8.2 ledger gate 必须通过依赖注入进入 Swarm Worker 启动点

`ensure_ledger_ready(run_id)` 若只存在于 RunManager，Swarm 内部 `_make_cli_worker()` 和各 worker task 启动路径无法天然调用它。

v4.1 必须指定所有权，例如：

```python
SpawnGuard = Callable[[str, str], Awaitable[None]]
# args: run_id, spawn_kind

build_driver(..., spawn_guard=mgr.ensure_ledger_ready)
Swarm(..., spawn_guard=spawn_guard)
```

然后在统一的 async Worker task launch wrapper 中调用，而不是只在同步 `_make_cli_worker()` 构造对象时检查。这样可覆盖 bootstrap/review/recon/recovery/operator spawn，同时不把 Web RunManager 反向 import 进内核。

per-worker token 也应在 Worker identity/profile 已确定后签发，注入该 Worker 的 exec env，并在该 task 的 `finally` 中 revoke；当前 run 级 `Swarm._gateway_token` 路径应由规范明确迁移/删除。

## 9. 测试矩阵补充与验证记录

v4.1 在恢复完整 1-30 项测试后，至少再锁定以下语义；可以重新编号，不要求强行维持“30 项”：

1. provider error 带 measured usage 时，`call_outcome` 与 `usage_status` 可同时表达；
2. internal started durable 后进程中断，reconcile 生成 unknown terminal；
3. checked SessionStore 路径执行 flush/fsync；
4. normal `emit()` 的 raising sink 仍不阻断健康 sink 与 fan-out；
5. terminal WAL 成功、canonical append 失败后，ledger 转 failed 且不再 spawn；
6. streaming headers 已发后 canonical 失败，不错误返回第二个 503；
7. rebuild 以原 usage_id 补写后恢复 ready，projection 只计一次；
8. 同一 fallback CliResult settlement retry 复用 invocation_id；
9. RunManager driver gate 与 Swarm 内部 worker gate 都经 injected SpawnGuard；
10. per-worker token 在 Worker finally revoke，两个并发 Worker 不互撤；
11. 最终 RFC 中可以直接读到全部测试矩阵，不依赖已被覆盖的旧正文。

本轮文档更新后于 2026-08-15 执行：

```text
uv run pytest -q
```

结果：**退出码 0**；完整测试进度到达 `100%`，无失败，只有 4 条第三方弃用警告。
该结果只证明当前工作区基线绿色，不表示 M5 已实施或 RFC v4 已取得实施许可。

## 10. v4.1 Go 条件

### 当前裁决

```text
总体架构：Accepted
实施许可：暂缓
所需动作：RFC v4.1 文档修订，不需要重新设计 M5
```

### v4.1 获批前必须完成

- [ ] 恢复自包含的已认可契约，或链接到保留的不可变 v3 正文；
- [ ] 在最终 RFC 中完整列出全部测试矩阵；
- [ ] 将 `call_outcome` 与 `usage_status` 分离；
- [ ] 为 internal producer 定稿可恢复的 started/finished journal；
- [ ] 给 checked SessionStore 定义 flush/fsync durability；
- [ ] 精确定义 normal `emit` 与 `emit_checked` 的不同 sink 顺序和兼容行为；
- [ ] 定义 terminal WAL 成功、canonical 失败后的 fail-stop/reconcile 行为；
- [ ] 让 fallback invocation_id 在 invocation 开始时生成并跨 retry 稳定携带；
- [ ] 通过 injected SpawnGuard 覆盖 RunManager 与 Swarm 内部真实执行点；
- [ ] 明确 per-worker token 从当前 run 级 `_gateway_token` 迁移到 Worker exec env 的接线点；
- [ ] 用 §9 的确定性测试锁定上述语义。

完成这些局部修订后，M5 可以直接进入测试先行实施；无需再次讨论两阶段 WAL、per-worker token、双账户、BTW 分类或 run-total 预算的总体方向。实施期间仍不得修改 provenance gate、anti-laundering 或 shared evidence graph 的事实语义。
