# M5 唯一账本 RFC v3 第三轮评审反馈

> 评审对象：[docs/14-m5-unique-ledger-rfc.md](14-m5-unique-ledger-rfc.md)
> 上位方案：[docs/10-v4-kernel-improvement-implementation.md](10-v4-kernel-improvement-implementation.md) M5
> 前轮评审：[docs/16-m5-unique-ledger-rfc-v2-review.md](16-m5-unique-ledger-rfc-v2-review.md)
> 评审日期：2026-08-15
> 裁决：**Design Review Required / 暂不批准实施**

## 0. 总结论

RFC v3 对第二轮评审的响应是实质性的。以下方向已经可以定稿，不应在下一版重新推翻：

- per-worker token 取代 run 级单 token；
- configured account 与真实 billing account 分离；
- Gateway handler 使用 acknowledged、有限流、可 drain 的线程到 asyncio 桥；
- `COST_UPDATE` 不增加 profile/account scope；
- profile/account 预算改由 `BUDGET_ALERT` 和 budget snapshot API 表达；
- fallback 明确为 `invocation_aggregate`，不伪装成 provider call；
- reconciliation 数值容差为 0；
- `ledger_ready` 必须先于恢复派发；
- 诊断调用明确排除在第一版账本声明之外。

但是，v3 仍不能直接施工。第三轮核验发现 5 个实现前必须定稿的阻断：

1. Gateway WAL 的顺序仍缺少“上游调用开始/终态”两阶段，当前文字在知道 usage 前就要求写包含 usage 的 WAL；“WAL 写前崩溃后告警”在没有预写记录时不可实现。
2. `emit_checked()` 的提交点定义错误：如果 critical sink 失败后仍把事件放入 ring/fan-out，再抛错重试，就会向在线消费者暴露未持久化事件，并可能产生新 seq 的重复事件。
3. canonical usage 身份模型不完整：v3 没有重新定义 `usage_id`；fallback 没有 `provider_call_id`；请求处理中 token 被撤销时 claims 如何保留也没有定稿。
4. `ledger_ready` 只列了 API 名称，没有落到真正的 driver/worker 启动临界点，也没有定义 rebuild 失败后的 fail-closed 状态；`_fresh_bus()` 重新接线 critical sink 的要求同样缺失。
5. internal producer 只有覆盖清单，没有定义 `LLMClient` 的 writer/identity 接口和异常终态；并且 BTW side-worker 当前实际走 ModelGateway，应属于 `gateway` producer，而不是 `internal`。

因此建议出 RFC v4。v4 不需要改变 v3 的总体架构，只需补齐本评审要求的调用生命周期、提交点、身份 schema 和失败状态。v4 获批前不实施 M5。

## 1. 本轮代码核验范围

本轮重新核对了：

- `dswarm/core/event_bus.py::EventBus.emit/add_sink/remove_sink`
- `dswarm/core/session_store.py::SessionStore.append/sink/replay`
- `dswarm/core/events.py::Event`
- `dswarm/solver/modelgateway.py::ModelGateway.issue/revoke/proxy/_proxy_json/_proxy_stream/_record_usage`
- `dswarm/swarm/worker_runtime_mixin.py::_ensure_container/_worker_env_for_profile/_make_cli_worker`
- `dswarm/swarm/swarm.py` 的 gateway token 收尾
- `apps/web/routes/btw.py` 的 container BTW gateway token 路径
- `apps/web/run_manager.py::create/_rehydrate/resolve/_fresh_bus/_ensure_standby`
- `dswarm/core/llm.py::LLMClient.chat/_chat_once/_chat_stream/_record_cost`
- `apps/web/titler.py::generate_title`
- `dswarm/solver/summarizer.py::summarize_node/translate_need_to_zh`
- `apps/web/routes/runs.py` 的后台 Titler 调用
- `apps/web/drivers.py` 的 Reason `LLMClient` 构造
- `dswarm/solver/cli_solver.py::_summarize_async`

测试基线在评审完成后重新运行，结果记录于 §8。

## 2. 已关闭的前轮阻断

### 2.1 Billing identity：通过

v3 将：

```text
configured_account_id
billing_account_id
```

拆开，并规定 account blocker 只读 `billing_account_id`。这与当前 Gateway 固定读取 `pi-main/API_KEY` 的事实一致。未知账户使用 `None`、禁止 `""` 假桶，也满足第二轮要求。

实施时应由真实 credential resolver 决定 internal producer 的 billing identity；不能无条件把所有 internal 调用写成 `None`。这是接线要求，不改变双字段契约。

### 2.2 Consumer compatibility：方向通过

v3 已删除 profile/account `COST_UPDATE` scope，避免破坏 Web/TUI/BTW 的既有累计摘要 reducer。profile/account 状态经专用预算事件和 snapshot API 展示，方向正确。

这里的“Web/TUI/BTW reducer 零改动”只应理解为 **现有 `COST_UPDATE` reducer 字节级不变**。为了向用户展示 `BUDGET_ALERT`，前端仍需增加专用告警事件处理；不能把“reducer 不变”解释为“前端完全不用改”。

### 2.3 Record kind：方向通过

v3 正确区分：

```text
provider_call
invocation_aggregate
```

并规定 Gateway/internal 使用 provider-call、fallback 使用 invocation aggregate。Gateway-token worker 禁止 fallback 二次计费，也符合 producer 互斥要求。

### 2.4 Thread bridge：主体方向通过

`run_coroutine_threadsafe()` + `future.result(timeout)`、有界 inflight、backpressure、drain、loop closed 语义已经覆盖第二轮要求。

实现时必须保留超时 future 的引用：`future.result(timeout)` 超时不代表 coroutine 已停止，它仍可能稍后完成。bridge 的 inflight/drain 集合必须跟踪到 future 真正完成；WAL recovery 和 canonical writer 再以 usage identity 幂等收敛。

## 3. 阻断 A：Gateway WAL 仍缺少可执行的调用生命周期

### 3.1 v3 当前顺序在时间上不可执行

v3 §4 写的是：

```text
生成 provider_call_id
→ WAL append（记录包含 status/usage）
→ flush + fsync
→ bridge.submit(USAGE_RECORDED)
→ 投影更新
```

但当前 Gateway 的真实调用顺序是：

```text
ModelGateway.proxy()
→ httpx.stream("POST", upstream, ...)
→ _proxy_json() / _proxy_stream()
→ 收到响应后 _record_usage()
```

也就是说，只有上游响应结束后才能知道 `status=measured/unknown` 和 usage 数值。不能在发送上游请求前写一条已经包含最终 usage 的 WAL 记录。

### 3.2 “WAL 写前崩溃后告警”当前不可实现

如果上游请求已经发送，但进程在第一条 WAL 记录前崩溃，重启后磁盘上没有 provider_call_id，也没有任何证据表明请求曾经存在。系统不可能凭空产生准确的 `gateway_usage_gap`。

要实现“所有已发送请求都有 measured/unknown 终态”，必须在发请求前持久化调用身份。

### 3.3 RFC v4 必须采用两阶段 append-only WAL

建议协议：

```text
1. 鉴权成功，捕获不可变 WorkerClaims 快照
2. 生成 provider_call_id
3. WAL append call_started + flush + fsync
4. 只有第 3 步成功后才发送上游请求
5. 上游成功且有 usage：WAL append call_finished(measured) + fsync
6. 上游成功但无 usage：WAL append call_finished(unknown) + fsync
7. 上游异常/中断：WAL append call_finished(unknown, error_code) + fsync
8. bridge.submit canonical USAGE_RECORDED
```

WAL 至少需要：

```text
record_type: call_started | call_finished
provider_call_id
claims snapshot
started_at / finished_at
status
usage
error_code
```

reopen 时遇到只有 `call_started`、没有 `call_finished` 的调用，应合成为：

```text
USAGE_RECORDED(status=unknown, reason=interrupted_before_terminal)
```

### 3.4 WAL 写失败必须 fail closed 或明确降级

v3 目前写“WAL 写失败 → error 日志 + 代理继续”。如果 WAL 预写失败后仍然发送上游请求，系统会主动制造一笔不可恢复的潜在费用，与唯一账本目标冲突。

第一版建议 fail closed：

```text
call_started WAL 重试失败
→ 不发送上游请求
→ 返回 503 accounting_unavailable
→ 发用户可见告警
```

若未来要支持 fail-open，必须是显式运维开关，并且 UI 明确显示“账本降级，费用可能不完整”；不能默认为日志后继续。

## 4. 阻断 B：`emit_checked()` 的 commit point 必须重写

### 4.1 当前 EventBus 的真实顺序

`EventBus.emit()` 当前在同一把锁内执行：

```text
seq++
→ event.seq 赋值
→ ring.append(event)
→ sinks
→ fan-out
```

sink 异常会被吞掉，然后仍然 fan-out。

v3 提议 critical sink 异常在 fan-out 后 re-raise。这会产生以下问题：

1. 在线 UI/订阅者已经看到了没有 durable record 的 `USAGE_RECORDED`；
2. producer 收到异常后重试，会得到新的 seq；
3. 同一个 usage_id 可能在在线流中出现两次；
4. 如果错误地重用同一个 `Event` 对象，第二次 seq 赋值还会修改 ring 中对同一对象的引用；
5. 非 critical sink 可能已经观察到失败尝试，无法回滚。

usage_id 幂等只能保护成本投影，不能自动修复在线事件流的重复和持久化可见性。

### 4.2 RFC v4 必须定义 critical commit point

推荐 `emit_checked()` 使用：

```text
lock
→ 分配 seq（失败时允许留下 seq gap）
→ 先执行 critical sink
→ critical 失败：不进 ring、不执行 non-critical sink、不 fan-out，向调用者抛错
→ critical 成功：ring.append
→ non-critical sinks（保持隔离）
→ fan-out
→ unlock
```

约束：

- critical sink 成功是事件的 commit point；
- seq gap 可以接受，seq 重复和倒序不可以接受；
- retry 必须构造新的 `Event` 对象，但保留同一个 `usage_id`；
- `SessionStore` 只能有一个 canonical critical registration，避免多个 critical sink 的部分提交问题；
- `_fresh_bus()` 必须把 `run.store.sink` 重新注册为 critical；meta/help/UI sinks 仍是 non-critical；
- `remove_sink()` 必须保留 sink 的 critical 元数据，不能只操作裸 callable 后丢失属性。

如果选择允许 critical sink 成功后、返回前发生“结果未知”，replay/projector 必须按 usage_id 幂等；但在线 fan-out 仍只能发生在 commit point 之后。

## 5. 阻断 C：canonical identity schema 仍不完整

### 5.1 v3 没有重新定义 `usage_id`

v3 多次依赖 usage_id 去重，但没有给出生成公式。v2 的公式依赖 `provider_call_id`，而 v3 又规定 fallback 是 invocation aggregate；fallback 本身不存在真实 provider call id。

RFC v4 应给出完整 schema，例如：

```python
@dataclass(frozen=True)
class UsageRecord:
    usage_id: str                 # 全局稳定幂等键
    producer: str                 # gateway | internal | fallback
    record_kind: str              # provider_call | invocation_aggregate
    provider_call_id: str | None
    invocation_id: str | None
    run_id: str
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None
    status: str                   # measured | estimated | unknown
    input_tokens: int | None
    output_tokens: int | None
    usd: float | None
```

推荐：

```text
provider call usage_id = usage::<run_id>::<producer>::<provider_call_id>
fallback usage_id      = usage::<run_id>::fallback::<invocation_id>
```

unknown 的 token/USD 必须是 `None`，不能写 0。

### 5.2 Claims 必须在请求入口捕获快照

per-worker token 会在 Worker 收尾时撤销，但一个已经鉴权并发往上游的请求可能仍在 streaming。不能在 `_record_usage()` 时重新查 token，否则 token 已撤销时会丢失 profile/account/worker identity。

handler 必须在鉴权成功时执行一次：

```text
claims = claims_for_token(token)
```

然后将不可变 claims 快照和 provider_call_id 一直传给：

```text
proxy → stream/json terminal → WAL → canonical writer
```

撤销 token 只阻止新请求，不得抹除已进入处理中的请求身份。

### 5.3 Token 上限裁决

不建议使用“LRU 1024 自动淘汰 active token”。LRU 可能撤销仍在运行或正在发请求的 Worker，制造随机 401。

建议：

- 每个 worker/BTW side-worker 在 `finally` 中 `revoke_token` 或 `revoke_worker`；
- run 收尾执行 `revoke_run`；
- 正常 active token 数量应受 Worker 并发上限约束，而不是受历史 spawn 总数约束；
- 增加 `max_active_tokens` 安全上限，达到上限时拒绝新签发并产生告警，不淘汰 active token；
- 测试异常构造失败、取消、超时、stop、provider recovery、BTW disconnect 均不泄漏 token。

这个裁决关闭 v3 唯一未决问题。

## 6. 阻断 D：`ledger_ready` 必须落到真实启动临界点并定义失败状态

### 6.1 只列 API 名称还不足以阻止派发竞速

当前真实启动点包括：

- `RunManager.start()` 中的 `scheduler.submit()`；
- `RunManager.resolve()` 中的 `scheduler.submit()`；
- queued run 后续由 scheduler 启动 driver；
- `_ensure_standby()` 内部 `create_task(_go())`；
- driver 创建 Swarm 后，Swarm 内部继续启动普通/review/recovery Worker；
- BTW route 独立构造 side-worker。

因此不能只在 HTTP route 或 `resolve()` 入口 await 一次。最稳妥的临界点是：

```text
任何 run driver 真正执行前 await ledger_ready
任何独立 BTW/standby driver 真正执行前 await ledger_ready
```

新 run 的空账本 rebuild 完成后立即 set；rehydrated/reopened run 则在 replay、WAL reconcile、projection/blocker rebuild 全部完成后 set。

### 6.2 rebuild 失败不能无限等待

v3 只有 `asyncio.Event`，没有失败状态。若 JSONL 损坏、WAL 无法读取或 canonical 补写失败，所有入口可能永久挂在 `await ledger_ready.wait()`。

RFC v4 应定义：

```text
ledger_state: rebuilding | ready | failed
ledger_error: structured error | None
```

规则：

- ready：允许派发；
- failed：fail closed，拒绝新 spawn；
- failed 必须产生用户可见告警和可重试的“重新构建账本”操作；
- stop/finalize 即使 ledger failed 也必须可执行；
- 不允许无超时、无状态地永久等待。

### 6.3 fresh bus 必须重接 critical sink

`RunManager._fresh_bus()` 当前重新建立：

```text
run.store.sink
meta sink
```

M5 实施后，新的 bus 必须将 `run.store.sink` 重新标记为唯一 critical sink；否则 reopen 后 `emit_checked()` 会退化成没有 durable acknowledgement 的普通 emit。

## 7. 阻断 E：internal producer 接线和 BTW 分类未闭合

### 7.1 当前代码并未让所有列入范围的调用进入账本

代码事实：

- Reason 的 `LLMClient` 当前注入了 `cost=run.cost`、`bus=run.bus`；
- Titler 默认构造裸 `LLMClient()`，调用 `chat()` 时没有传 run_id；
- 部分 Summarizer 默认构造裸 `LLMClient()`，虽然传 run_id，但没有 durable usage writer/cost；
- `LLMClient._record_cost()` 只在请求成功且 usage 非空后调用；异常、超时、无 usage 不会产生 unknown terminal；
- 当前 `LLMClient` 没有 provider_call_id、profile/account identity 或 checked writer 接口。

因此“Reason/Titler/Summarizer 已纳入 internal producer”目前只是目标清单，不是可以直接实现的接口契约。

### 7.2 RFC v4 必须定义 `LLMClient` usage context

建议显式注入：

```python
@dataclass(frozen=True)
class UsageContext:
    run_id: str
    challenge_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None
    producer: str = "internal"

LLMClient(..., usage_writer: UsageWriter | None, usage_context: UsageContext | None)
```

每次 `chat()`：

```text
生成 provider_call_id
→ durable call_started
→ 发请求
→ finally 产生 measured/unknown terminal
→ checked canonical append
```

Titler、Summarizer 等 fire-and-forget 调用即使吞掉业务异常，也不能吞掉 usage writer 的可见告警；账本错误与“标题生成失败后回退”是两种不同状态。

### 7.3 BTW side-worker 的 producer 分类需要修正

当前 container BTW 明确把 `DSWARM_TASK_TOKEN` 和 Gateway URL 注入 side-worker，它的上游请求由 ModelGateway 捕获。因此：

```text
container BTW side-worker → producer=gateway, token_scope=btw
```

不是 `producer=internal`。

若存在非 Gateway 的本地 BTW Pi invocation，则它只能按实际路径归为 fallback `invocation_aggregate`；只有直接调用 host `LLMClient` 的 BTW 请求才属于 internal provider call。

RFC v4 的覆盖表应按“真实传输路径”分类，而不是按功能名称分类。

## 8. 测试矩阵补充与验证结果

在 v3 现有 18 项基础上，RFC v4 至少补充：

1. WAL `call_started` 必须在 mock upstream 收到请求前 durable；
2. 只有 started、无 terminal 的 WAL 在 reopen 后生成 unknown canonical terminal；
3. preflight WAL 失败时 upstream 请求次数为 0，并返回 accounting unavailable；
4. critical sink 失败时 ring、subscriber、non-critical sink 均看不到该事件；
5. checked retry 使用新 Event/新 seq、同 usage_id，投影只计一次；
6. `_fresh_bus()` 后 SessionStore 仍是 critical sink；
7. token 在 streaming 中被 revoke 后，终态仍保留入口 claims 快照；
8. max_active_tokens 达限拒绝签发，不 LRU 淘汰 active token；
9. Titler/Summarizer 请求成功、无 usage、超时分别产生 measured/unknown terminal；
10. container BTW 记为 gateway producer，非 Gateway invocation 才记 fallback；
11. ledger rebuild 失败进入 failed 状态、拒绝 spawn，但 stop/finalize 正常；
12. `BUDGET_ALERT` 在 Web UI 可见，同时旧 `COST_UPDATE` reducer 输出逐字节不变。

本轮在文档更新后于 2026-08-15 执行：

```text
uv run pytest -q
```

结果：**退出码 0**；完整测试进度到达 `100%`，仅有 4 条第三方弃用警告，
无失败。该结果只证明当前工作区基线绿色，不表示 M5 已实施或 RFC v3 已获批。

## 9. 最终裁决与 RFC v4 Go 条件

### 当前裁决

```text
M5 RFC v3：Design Review Required / 暂不批准实施
```

### RFC v4 获批前必须解决

- [ ] Gateway/internal 均有 call_started → terminal 的可恢复调用生命周期；
- [ ] preflight WAL 失败默认不发送上游请求，或给出经明确批准的 fail-open 降级模式；
- [ ] `emit_checked` 以 critical durable success 为 commit point，失败事件不进 ring/fan-out；
- [ ] 定义完整 UsageRecord schema、usage_id、provider_call_id/invocation_id 互斥规则；
- [ ] claims 在鉴权入口捕获快照，token revoke 不破坏 in-flight 归属；
- [ ] token 上限采用 active hard cap，不 LRU 驱逐 live token；
- [ ] ledger gate 落到真实 driver 启动临界点，并有 ready/failed 状态；
- [ ] `_fresh_bus()` 保持 SessionStore critical registration；
- [ ] 定义 `LLMClient` usage writer/context 接口和 unknown 终态；
- [ ] 修正 BTW producer 分类；
- [ ] 定义 `BUDGET_ALERT` 的用户可见消费路径；
- [ ] 补齐 §8 的确定性测试矩阵。

满足以上条件后，M5 才可进入测试先行实施。实施期间不得修改 provenance gate、anti-laundering 或 shared evidence graph 的事实语义，也不得重新增加 Worker 结束时的第二次计费。
