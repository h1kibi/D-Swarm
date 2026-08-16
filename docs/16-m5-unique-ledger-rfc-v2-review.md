# M5 唯一账本 RFC v2 第二轮评审反馈

> 评审对象：[docs/14-m5-unique-ledger-rfc.md](14-m5-unique-ledger-rfc.md)  
> 上位方案：[docs/10-v4-kernel-improvement-implementation.md](10-v4-kernel-improvement-implementation.md) M5  
> 前轮评审：[docs/15-m5-unique-ledger-rfc-review.md](15-m5-unique-ledger-rfc-review.md)  
> 评审日期：2026-08-15  
> 裁决：**Design Review Required / 暂不批准实施**

## 0. 总结论

RFC v2 已经实质性解决了 v1 的大部分方向错误：

- 用新的 canonical `USAGE_RECORDED` 表达单次 usage，而不是继续重载累计摘要 `COST_UPDATE`；
- 把 container Pi 的真实逐请求捕获点放到 `ModelGateway`，把 `_stream_cost()` 降为非 gateway 路径的 fallback；
- 将内部 `LLMClient` 调用纳入同一协议方向；
- 使用 UUID 级 provider call identity，避免实例内计数在重启后碰撞；
- 将预算第一版收缩为 run-total、显式 `raise_cap`/`override`、profile/account 独立 blocker；
- 保留 unknown/estimated 是状态、cap 不强杀运行中 worker、stop/finalize 不受预算门阻塞等正确原则。

这些方向应保留，RFC v2 也比 v1 更接近可施工状态。

但当前仍不能批准实施。新一轮代码核验发现，v2 的 `worker_instance_id/profile/account` 身份方案与现有 **run 级单 token + run 级单 container** 生命周期不兼容；直接调用 `SessionStore.append()` 会绕过 EventBus 的序号分配与总序保证；gateway JSONL 与 canonical event 的双写目前只有测试期聚合比对，没有生产恢复闭环；`account_id` 仍会把“配置账户”误写成“实际扣费账户”；RunManager 的 create/rehydrate/reopen 也没有在派发前重建账本和 blocker。

此外，RFC 的两个语义仍需收紧：fallback 是 CLI invocation aggregate，不是 provider billable call；新增 profile/account `COST_UPDATE` 并不能与“Web/TUI/BTW reducer 零改动”同时成立。

因此当前裁决为：

```text
M5 RFC v2：Design Review Required / 暂不批准实施
```

建议出 RFC v3，仅修订本评审列出的身份、持久化、恢复和消费契约；在 v3 获批前，不实施 M5，不新增任何 worker 结束后二次计费路径。

## 1. 本轮核验范围与基线

本轮直接核对了：

- `dswarm/solver/modelgateway.py`
- `dswarm/swarm/worker_runtime_mixin.py`
- `dswarm/swarm/swarm.py`
- `dswarm/solver/cli_solver.py::_stream_cost`
- `dswarm/solver/cli_driver.py::CliResult` 与 Pi usage parser
- `dswarm/core/event_bus.py::EventBus.emit`
- `dswarm/core/session_store.py::SessionStore`
- `dswarm/core/cost.py::CostController`
- `dswarm/core/llm.py::LLMClient._record_cost`
- `apps/web/run_manager.py::create/_rehydrate/_fresh_bus/resolve`
- `apps/web/routes/btw.py` 的 gateway token 复用路径
- Web/TUI/BTW 的 `COST_UPDATE` 消费者
- `tests/test_modelgateway.py`、`tests/test_web_server.py` 与相关 worker runtime 测试

启动基线：Windows 工作区中的 `./init.sh` 按项目保护逻辑拒绝从 WSL `/mnt/c/...` 运行；改用其等价主检查 `uv run pytest -q`，退出码为 0，只有 4 个依赖弃用 warning。本评审只修改文档，不实施 M5。

## 2. 已认可、应保留的设计

### 2.1 `USAGE_RECORDED` 与 `COST_UPDATE` 分层

新建 immutable `USAGE_RECORDED` 作为 canonical usage 事实，`COST_UPDATE` 只作为可重建的累计投影，是正确边界。账本重建不得再依赖历史 `COST_UPDATE` 的增量/累计猜测。

### 2.2 Gateway 是 container Pi 的真实逐请求捕获点

`ModelGateway.proxy()` 每次对应一次上游 `/chat/completions` 请求；`_record_usage()` 位于非流式响应和流式响应的请求级结束位置。相比一个完整 CLI invocation 才产生一次的 `CliResult`，Gateway 更接近 provider billable call 的真实粒度。

### 2.3 `_stream_cost()` 只做非 gateway fallback

对持 gateway token 的 worker 禁用 `_stream_cost()` 二次 charge，是防止同一费用进入两个 producer 的必要条件。该原则应保留，但 fallback 本身必须诚实标记为 aggregate/estimated，见阻断 F。

### 2.4 UUID 方向正确

`provider_call_id=UUID` 比 `solver_id + 实例内 charge_seq` 更适合跨重启幂等。`worker_instance_id` 也应在每次 spawn 时生成，而不是从可复用的 label 或 solver counter 推导。

### 2.5 预算第一版收缩合理

第一版仅做当前 run 的累计预算、只阻止未来 spawn、profile/account 独立 blocker、普通 resume 不解除预算 block、显式 `raise_cap`/`override` 写 durable action，这些决策可以保留。

## 3. 阻断级问题

### 3.1 阻断 A：per-worker claims 与当前 run 级单 token 生命周期冲突

RFC v2 要求 task token 携带：

```text
run_id / worker_instance_id / profile_id / account_id
```

但当前实现不是 spawn 级 token：

1. `worker_runtime_mixin.py:518-523` 明确同一 active run 的后续 worker 复用同一个 container；
2. token 只在首次创建 container 时签发（`:551-560`）；
3. 所有后续 worker 在 `_runtime_env_for()` 中读取同一个 `self._gateway_token`（`:404-418`）；
4. `ModelGateway` 当前是 `token -> run_id` 和 `run_id -> token`（`modelgateway.py:148-149`）；
5. `issue(run_id)` 会先 `revoke(run_id)`（`:184-198`），所以同一 run 只能有一个有效 token；
6. BTW 路径甚至明确复用 run token，以避免签发新 token 撤销普通 worker（`apps/web/routes/btw.py:372-385`）。

因此不能只给现有 token 增 claims。如果在每次 spawn 调用现有 `issue(run_id)`，第二个并发 worker会立刻撤销第一个 worker 的 token，正在运行的 worker随后收到 401。

#### RFC v3 必须定稿

Gateway token registry 至少改为：

```text
token -> WorkerClaims
run_id -> set[token]

WorkerClaims:
  run_id
  worker_instance_id
  profile_id
  configured_account_id
  billing_account_id
  worker_kind        # ordinary/review/recon/btw/internal-adapter 等
```

并提供不同语义的 API：

```text
issue_worker(claims) -> token       # 不撤销同 run 的其他 token
revoke_token(token)                 # 单 worker 收尾
revoke_worker(worker_instance_id)   # 可选幂等便利 API
revoke_run(run_id)                  # stop/finalize/teardown 批量撤销
claims_for_token(token) -> claims
```

`worker_instance_id` 必须在 `_make_cli_worker()` 解析 profile 后、构造 worker env 前生成；每个 worker 的 env 注入自己的 token。单 worker 正常结束、取消、异常和 spawn rollback 都要撤销自己的 token；run teardown 再批量兜底。

BTW 也不得继续借用普通 worker 的 token。它应有独立 `worker_instance_id`/token，并明确其 usage 进入哪些投影和预算。

### 3.2 阻断 B：直接 `SessionStore.append()` 会绕过 event seq 与总序

RFC v2 的写入顺序是：

```text
SessionStore.append(USAGE_RECORDED)
-> CostController.register_usage
-> bus.emit(COST_UPDATE)
```

这不符合当前事件脊柱的排序契约：

- `EventBus.emit()` 在自己的 asyncio lock 下分配 `seq`，并把 seq、ring、sink、fan-out 串成一个总序（`event_bus.py:33-66`）；
- 新构造的 `Event` 默认 `seq=0`（`events.py:69-76`）；
- `SessionStore.append()` 只把传入 event 原样写入 JSONL，不负责分配 seq（`session_store.py:36-43`）。

因此从 Gateway writer 直接 `SessionStore.append(Event(...))` 会写入 `seq=0`，而且会与正在经 EventBus 写入的其他事件竞争。即使 SessionStore 的 per-run lock 避免单行交错，也无法保证 canonical usage event 与其他 bus event 的统一顺序，更无法保证 reopen 后 SSE cursor 连续。

#### RFC v3 必须定稿

`USAGE_RECORDED` 必须仍经 EventBus 分配 seq；同时要让生产者能知道 durable sink 是否成功。推荐在不修改 EventBus substrate 的前提下，引入 **checked sink acknowledgement**：

```text
DurableUsageWriter.write(record)
  -> 构造 Event(USAGE_RECORDED)
  -> 在 CheckedSessionSink 注册该 event 的 ack
  -> await bus.emit(event)                 # 由 bus 分配 seq/总序
  -> CheckedSessionSink.require_success(event)
  -> CostController.register_usage(record)
  -> await bus.emit(COST_UPDATE summary)
```

`CheckedSessionSink` 替代 RunManager 当前直接注册的 `store.sink`，内部仍调用同一个 `SessionStore.append()`；append 成功记录 ack，失败记录异常并 re-raise。EventBus 即使继续隔离 sink 异常，writer 也能在 `emit()` 返回后读取该 event 的 checked 结果。

若 RFC 选择其他实现，也必须同时证明：

- usage event 获得与其他事件相同的单调 seq；
- 不访问/修改 EventBus 的私有 `_seq/_lock`；
- append 失败对 usage producer 可见；
- 在线顺序与 replay 顺序一致；
- 不把 SessionStore 变成第二条独立事件序列。

### 3.3 阻断 C：Gateway 线程到 owning asyncio loop 的桥仍未设计完成

`ModelGateway` 使用 `ThreadingHTTPServer`（`modelgateway.py:169-174`）；每个 HTTP handler 在普通线程中执行。`SessionStore` 使用 `asyncio.Lock`，EventBus 也属于创建它的 asyncio loop。handler 线程不能直接 `await writer.write()`，也不能在自己的线程临时 `asyncio.run()` 操作 run bus/store。

RFC v2 仅写“`loop.call_soon_threadsafe` + 队列”，不足以形成可验收契约。必须定义：

- 谁在 run 启动时向 gateway 注册 owning loop 和 `DurableUsageWriter`；
- handler 如何得到调用结果，而不是 fire-and-forget；
- 队列是否有界、满载时如何 backpressure；
- canonical append 超时/失败如何反馈给 handler 和运行时告警；
- stop/finalize 时如何停止接收、drain 已接收 usage、再撤销 token；
- backend restart 后旧 registry 如何清空，避免向已关闭 loop 投递。

推荐第一版使用：

```text
asyncio.run_coroutine_threadsafe(writer.write(record), owning_loop)
-> Future.result(timeout=...)
```

外加 per-run 有界 inflight semaphore/queue、明确 timeout 和 shutdown drain。只调用 `call_soon_threadsafe()` 而不等待结果，无法满足“持久化失败对生产者可见”。

### 3.4 阻断 D：gateway JSONL 与 canonical event 的双写只有测试，没有生产恢复闭环

RFC v2 将 `gateway-usage.jsonl` 称为 producer-local durable buffer，并声称 reconciliation 可兜底。但当前代码事实是：

- `_record_usage()` 由多个 handler 线程写同一文件，没有显式 per-run 文件锁；
- 写入后不 flush/fsync；
- 行内没有 `provider_call_id`、worker claims 或写入状态；
- 整个函数 `except Exception: pass`（`modelgateway.py:307-336`）；
- RFC 只要求测试比较两边聚合总量，没有定义生产启动/重开时如何补写缺失 canonical event。

测试可以发现差异，但不能在真实运行中修复差异。当前设计仍可能出现：Gateway 已实际花费、JSONL 有记录、canonical append 失败、预算 gate 永久低估；或者 canonical 成功、JSONL 失败，eval 仍低估。

#### RFC v3 必须定稿

建议把 gateway buffer 明确定义成 versioned write-ahead buffer：

```text
1. 每次 upstream request 开始即生成 provider_call_id；
2. request 结束形成 measured/unknown 终态 row；
3. 在 per-run 文件锁下 append + flush + fsync buffer row；
4. 通过 checked async bridge 写 USAGE_RECORDED；
5. canonical 成功后可追加 ack 状态，或由 usage_id 集合判定已投影；
6. create/reopen/recovery 在允许 dispatch 前扫描 buffer，补写 canonical 缺口；
7. canonical 已有相同 usage_id 时幂等跳过。
```

必须给出“双写任一边失败”和“进程在任意步骤崩溃”的恢复表。reconciliation 不应只是测试；至少要有生产 replay/recovery 路径。旧格式 buffer 可保留为 eval 历史输入，但新格式行必须带 schema version、provider_call_id 和 claims。

### 3.5 阻断 E：`account_id` 仍把配置归属误写成实际扣费归属

RFC v2 §7 承认 Gateway 的真实 key 仍固定来自 `pi-main/API_KEY`（`modelgateway.py:53-67`），但又计划用 profile claims 中的 `credential_account` 作为 account 投影。

这会产生错误账本：

```text
profile: pi-web
configured account: pi-web-main
Gateway 实际 key: pi-main
RFC v2 account ledger: pi-web-main   # 错误
真实扣费账户: pi-main
```

“run-total token budget 不受影响”只能说明 global/profile token cap 尚可工作，不能证明 account-level cap 正确。RFC 同时承诺 profile/account 双 blocker，所以这个错配是阻断项。

#### RFC v3 必须定稿

身份字段应分离：

```text
profile_id
configured_account_id   # 配置想使用谁
billing_account_id      # 真实用于上游请求的凭据账户
```

在 credential routing RFC 实施前：

- gateway producer 的 `billing_account_id` 必须是实际的 `pi-main`；
- profile 投影仍按真实 worker profile 累计；
- account blocker 只能读取 `billing_account_id`；
- 不得把 `configured_account_id` 当成真实扣费账户；
- 如果无法确定真实账户，则 `billing_account_id=None`，该 usage 仍进入 global/challenge/solver/profile，但不进入虚假的 account bucket。

未来按 profile 路由真实 key 后，二者才可能相同。

### 3.6 阻断 F：create/rehydrate/reopen 没有 ledger rebuild readiness gate

RFC v2 只写“由 `SessionStore.replay(run_id)` 重建”，没有定义实际接入顺序。当前代码：

- `RunManager.create()` 总是新建空的 `CostController(bus=bus)`（`run_manager.py:722-734`）；
- `_rehydrate()` 调用 `create()` 恢复 rail handle，但不重建 cost/gate（`:274-329`）；
- `_fresh_bus()` 只替换 bus 并执行 `run.cost.bus = new_bus`（`:1385-1402`）；
- `resolve()` 在 `_fresh_bus()` 后即可 `scheduler.submit()`（`:1353-1374`）。

所以如果按当前结构接线，后台重启或恢复旧 run 后，历史 usage 和预算 action 不会在新 worker 派发前恢复；超限 profile/account 可继续 spawn。

#### RFC v3 必须选择并写死一种接入方式

推荐契约：

```text
create/rehydrate/reopen
  -> replay USAGE_RECORDED + BUDGET_ACTION
  -> rebuild CostController + ProfileBudgetGate + active blockers
  -> bind current live bus/writer
  -> ledger_ready = true
  -> scheduler.submit / worker spawn
```

由于 `RunManager.create()` 当前是同步函数，RFC 必须明确选择：

1. 增加同步、只读的 ledger replay/projector；或
2. 给 Run 增 `ledger_ready` task/event，并让所有 start/resolve/standby/spawn 入口在派发前 await；或
3. 将相关 create/rehydrate 生命周期改为 async，并证明所有调用点完成迁移。

不能只写“启动时 replay”，也不能让 rebuild 与 scheduler submit 并发竞速。

### 3.7 阻断 G：provider-call 终态与 fallback 粒度仍未闭合

RFC v2 的 canonical 注释把 usage_id 定义为一次 provider call，但 fallback 使用：

```text
{worker_instance_id}:{charge_seq}
```

并从一个 `CliResult` 取 usage。代码中的 `CliResult` 是“一次 CLI run 的 outcome”（`cli_driver.py:173-185`），Pi parser 扫完整 stdout 后只保留最后看到的 usage 值（`:603-653`）。因此 fallback 最多是 invocation aggregate/estimate，不是 provider billable call。

RFC v3 应在 schema 中诚实区分：

```text
record_kind: provider_call | invocation_aggregate
status: measured | estimated | unknown
provider_call_id: Optional[str]
aggregate_id: Optional[str]
```

- gateway/internal 的真实逐请求记录使用 `provider_call + measured/unknown`；
- fallback 使用 `invocation_aggregate + estimated/unknown`；
- fallback 不得伪装成 provider_call；
- gateway token 存在时 fallback 必须跳过；
- 探针只能确定 Pi 输出是 delta 还是 session cumulative，不能把 invocation aggregate 变成逐 provider call。

此外，每个“已发送到上游”的 gateway/internal request 必须产生一个 terminal accounting event：有 usage 时 measured；无 usage、上游中断、usage JSON 非法时 unknown。不能因为 `_record_usage()` 找不到 usage 就直接 return，造成一次真实调用在账本中完全不存在。

RFC v2 把“streaming 客户端提前断开”作为容差问题的表述也不够准确：当前 `_chunk()` 吞 BrokenPipe 后仍继续消费上游流，客户端断开未必导致少记；真正需要覆盖的是上游提前终止、最终帧无 usage、解析失败和 canonical 写入失败。结论仍应是：**不使用聚合数值容差掩盖缺失调用；缺失 usage 显式记 unknown。**

### 3.8 阻断 H：新增 profile/account `COST_UPDATE` 与“消费者零改动”冲突

RFC v2 §1.2 同时提出：

```text
COST_UPDATE 新增 profile/account scope
Web/TUI/BTW reducer 零改动
```

当前消费者不支持这个组合：

- Web reducer 只把 `scope=solver` 作为单 worker 累计，其余 scope 一律按 headline global/challenge 处理（`apps/web/ui/lib/events.ts:1725-1753`）；
- TUI 同样把非 solver scope 当 headline 候选（`apps/tui/app.py:129-141`）；
- BTW 历史摘要对每条 `cost.update` 直接累加，已经依赖现有事件形态（`dswarm/solver/btw.py:562-570`）。

如果在每次 usage 后再发 profile/account 累计摘要，至少 BTW 会重复累加，Web/TUI 也可能把某个 profile/account 局部值误当全局值。

#### RFC v3 必须二选一

- **推荐**：`COST_UPDATE` 继续只发当前三种 headline scope；profile/account 预算展示使用 `BUDGET_ALERT`、专用 `BUDGET_SNAPSHOT` 或 API snapshot，不向旧 reducer 注入新 scope；或
- 修改 Web/TUI/BTW 全部消费者，并给 legacy replay 加兼容测试。

“新增 scope + reducer 零改动”不能保留。

## 4. 高优先级但可随 v3 一并定稿的问题

### 4.1 internal producer 的覆盖范围必须列清单

`LLMClient._record_cost()` 只有在 `cost is not None`、`run_id is not None` 且 provider 返回 usage 时才执行。当前部分 Reason 路径传入 `run.cost`，但 Titler、部分 BTW、Summarizer/测试辅助路径并不都绑定 run CostController。

RFC v3 应列出第一版必须计入账本的内部调用：Reason planner、reviewer、run title、BTW、node summary、provider probe 等分别是 included 还是 explicitly excluded。若排除，文档必须缩小“唯一账本”的声明；若纳入，必须给每条调用链传 run identity/writer，并避免把无 run 的健康探针错误计入某个 run。

### 4.2 nullable identity 不应使用空字符串

RFC v2 未决项提议 internal 未知账户写 `account_id=""`。建议拒绝：

- `account_id/profile_id/worker_instance_id` 使用 `Optional[str]`；
- `None` 表示本次调用确实没有该维度；
- 空字符串不得成为一个可聚合、可设 cap 的伪账户；
- 即使 account 未知，usage 仍进入 global/challenge/solver；profile 已知时仍进入 profile。

### 4.3 幂等必须覆盖并发写，而不只是 replay

`CostController._usage_ids` 只能解决单线程顺序 replay，不自动解决两个 handler 同时提交相同 usage_id。writer 需要 run-scoped async lock 或等价原子状态：

```text
检查 usage_id -> durable append/ack -> 投影 -> 标记已完成
```

append 失败不能提前把 usage_id 标记为完成；并发重复请求只能有一个完成 canonical projection。

## 5. 对 RFC v2 三个未决问题的裁决建议

### 5.1 internal producer 的 account 归属

**选择 nullable actual billing identity，不选择空字符串。**

- 已知真实扣费账户：写 `billing_account_id`；
- 只知道配置账户：写 `configured_account_id`，`billing_account_id=None`；
- 未知 account 不阻止 global/challenge/solver/profile 记账；
- account cap 只对真实 `billing_account_id` 生效。

### 5.2 Gateway 线程与 asyncio writer 的桥

**方向认可，但不能停留在 `call_soon_threadsafe`。**使用 owning loop 上的 coroutine、`run_coroutine_threadsafe`/等价 acknowledged bridge、有界 inflight、超时、异常回传和 shutdown drain。桥必须通过测试证明线程并发、停止和 loop 关闭时不会丢 usage 或死锁。

### 5.3 reconciliation 容差

**数值容差设为 0；缺 usage 用状态表达，不用容差掩盖。**

- 同一 `usage_id` 的 measured token/USD 必须逐字段一致；
- 请求已发送但没有 provider usage，写 `status=unknown`；
- 如 provider 只给部分字段，允许字段级 unknown；
- aggregate 报表可以单独展示 estimated/unknown 数量，但不能把 unknown 当 0 后再用“允许偏差”通过。

## 6. RFC v3 最低实现契约建议

### 6.1 身份

```text
RunIdentity:
  run_id
  challenge_id

WorkerIdentity:
  worker_instance_id
  solver_id
  profile_id?
  worker_kind

BillingIdentity:
  configured_account_id?
  billing_account_id?

CallIdentity:
  producer
  record_kind
  provider_call_id? / aggregate_id?
  usage_id
```

### 6.2 canonical usage 状态

```text
USAGE_RECORDED:
  usage_id
  producer
  record_kind              # provider_call | invocation_aggregate
  status                   # measured | estimated | unknown
  run/challenge/solver/worker/profile identity
  configured_account_id?
  billing_account_id?
  model/provider?
  input_tokens?
  output_tokens?
  usd?
  error_kind?              # unknown 的原因，不存敏感响应
```

### 6.3 在线写入

```text
producer-local capture
  -> stable usage_id
  -> gateway WAL（gateway producer only）
  -> checked EventBus emit(USAGE_RECORDED)
  -> durable ack
  -> idempotent CostController projection
  -> BudgetGate threshold transition
  -> COST_UPDATE existing headline summary
  -> BUDGET_ALERT if threshold crossed
```

### 6.4 恢复

```text
backend/run reopen
  -> replay canonical usage + budget actions
  -> reconcile gateway WAL missing usage_id
  -> replay any newly appended canonical usage
  -> rebuild projections/blockers
  -> mark ledger_ready
  -> allow scheduler dispatch
```

## 7. RFC v3 必须补充的测试矩阵

在 RFC v2 已列测试基础上，至少新增：

1. 同一 run 的两个并发 worker获得不同 token；
2. 签发第二个 token不撤销第一个；
3. 两个 token 的 usage 分别归属各自 worker/profile；
4. 单 worker结束只撤销自己的 token；
5. run stop/finalize 批量撤销全部 token；
6. BTW 使用独立 token，不复用或撤销普通 worker token；
7. spawn 构造失败/取消路径不泄漏 token；
8. `USAGE_RECORDED` 经 EventBus 获得单调 seq，不出现 seq=0；
9. usage 与同时发生的 worker/graph 事件在线、JSONL、replay 顺序一致；
10. SessionStore append 失败时 checked writer 对 producer 返回失败，内存投影不前进；
11. Gateway handler 线程并发写入不会死锁、跨 loop 或无界堆积；
12. shutdown 会 drain 已接收 usage，再撤销 run tokens；
13. gateway WAL 成功、canonical 失败后，reopen 自动补写；
14. canonical 成功、WAL 失败时产生可见告警，eval 不静默低估；
15. 进程分别在 WAL 前、WAL 后/canonical 前、canonical 后/投影前崩溃的恢复结果一致；
16. `configured_account_id != billing_account_id` 时 account ledger 只计真实 billing account；
17. create/rehydrate/resolve 在 ledger_ready 前不能 scheduler submit；
18. provider 无 usage/非法 usage/上游中断各产生一条 unknown terminal record；
19. fallback 记录为 invocation_aggregate + estimated/unknown，不伪装 provider_call；
20. gateway worker 的 fallback 不二次计费；
21. profile/account 预算展示不污染旧 `COST_UPDATE` headline reducer；
22. Web/TUI/BTW 对新增事件和 legacy replay 均无重复计费；
23. internal included/excluded 清单逐路径测试；
24. 同 usage_id 并发提交、重试、重放均只进入投影一次；
25. profile/account 同时越线产生两个 blocker，解除一个仍拒绝 spawn；
26. 普通 resume 不解除预算 blocker，`raise_cap`/`override` 可重建；
27. stop/finalize 不被 ledger writer 或预算门卡住；
28. 全量 `uv run pytest -q` 绿，provenance gate 与 anti-laundering 测试原样通过。

## 8. 最终裁决与 Go 条件

### 当前裁决

```text
M5 RFC v2：Design Review Required / 暂不批准实施
```

### RFC v3 获批前必须解决

- [ ] 把 run 级单 token 改成支持并发的 per-worker claims/token 生命周期；
- [ ] 定义单 worker revoke、run 批量 revoke、BTW 独立 token 与异常回收；
- [ ] 让 canonical usage 仍经 EventBus 获得 seq/总序，并提供 checked durable acknowledgement；
- [ ] 定稿 Gateway handler 线程到 owning asyncio loop 的有界、可等待、可 drain 桥；
- [ ] 把 gateway buffer 从“测试对账”升级为有 provider_call_id 的生产恢复闭环；
- [ ] 分离 configured account 与真实 billing account；
- [ ] 给出 create/rehydrate/reopen 在 dispatch 前完成 rebuild 的明确接入点；
- [ ] 区分 provider_call 与 invocation_aggregate，所有已发送请求都有 measured/estimated/unknown 终态；
- [ ] 删除“profile/account COST_UPDATE + reducer 零改动”的矛盾；
- [ ] 定义 internal producer 第一版的完整 included/excluded 范围；
- [ ] 用本评审测试矩阵覆盖并发、崩溃、重启、恢复和 legacy replay。

满足以上条件后，M5 才可进入实现计划。实施期间不得修改 provenance gate、anti-laundering，不得把 usage/预算状态写入 shared evidence graph 作为事实来源，也不得在 worker 结束时新增第二次 charge。
