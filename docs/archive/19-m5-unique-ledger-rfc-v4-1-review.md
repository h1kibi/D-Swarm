> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../00-architecture-spec.md) 取代；本文保留作为时代记录。

# M5 唯一账本 RFC v4.1 第五轮最终评审

> 评审对象：[docs/14-m5-unique-ledger-rfc.md](14-m5-unique-ledger-rfc.md)  
> 上位方案：[docs/10-v4-kernel-improvement-implementation.md](../10-v4-kernel-improvement-implementation.md) M5  
> 前轮评审：[docs/18-m5-unique-ledger-rfc-v4-review.md](18-m5-unique-ledger-rfc-v4-review.md)  
> 评审日期：2026-08-15  
> 裁决：**Approved for Staged Implementation / 允许进入 Phase 1**

## 0. 最终结论

RFC v4.1 已关闭第四轮列出的全部 Go 条件。第五轮未发现需要推翻两阶段 journal、
per-worker token、双账户、producer 互斥、run-total 预算或 checked canonical commit point 的
新架构问题。

本轮代码核验额外发现了几处会影响“自包含可施工性”的字段/接口遗漏，但它们都是已批准架构
内的局部规范补全，而不是第五轮新方案：

1. Gateway claims/canonical usage 必须携带 `challenge_id` 与 `solver_id`，否则不能只靠 canonical
   usage 事件重建 challenge/solver 两层投影；
2. 当前 `Swarm._gateway_token` 是 run 级共享值，RFC 必须明确在 Worker identity/profile 确定后
   签发 token、逐 Worker 注入 exec env，并在 task `finally`/spawn rollback 撤销；
3. 上游执行后不仅 canonical append 可能失败，`finished` journal 自身也可能失败；两者都必须
   fail-stop，且告警不能只依赖正在失败的 checked persistence 链；
4. `append_checked` 应为 async、复用 SessionStore per-run lock；EventBus 需要显式配对 normal/
   checked sink，才能在不改变 normal `emit()` 的前提下避免 double append；
5. 最终测试矩阵要显式覆盖 normal emit 兼容、Worker lifecycle revoke、streaming 不事后 503、
   terminal-journal failure 和 challenge/solver identity。

上述五点已由本评审直接写回 docs/14，避免再开启 v4.2 文档循环。修订后裁决为：

```text
M5 RFC v4.1：Approved for Staged Implementation
立即允许：Phase 1 — SessionStore.append_checked + EventBus.emit_checked
后续允许：Phase 2–6 在前一阶段确定性测试和全量 pytest 绿色后顺序推进
```

## 1. 基线与代码事实核验

### 1.1 测试基线

评审开始前在 Windows 宿主执行：

```text
uv run pytest -q
```

结果：退出码 0，进度到 100%，无失败，4 条第三方弃用警告。`bash ./init.sh` 在 WSL 读取
Windows workspace 时按仓库防护主动拒绝，故采用 AGENTS.md 允许的等价主检查。

### 1.2 当前实现事实

本轮重新核对：

- `EventBus.emit()` 仍是 `seq -> ring -> sinks(best-effort) -> fan-out`，且全部在同一 async lock；
- `SessionStore.append()` 仍只写 JSONL，没有显式 `flush/fsync`；
- `ModelGateway` 仍使用 `ThreadingHTTPServer`、run 级单 token，`issue(run_id)` 会撤销同 run 旧
  token；当前 `_record_usage()` 无锁、无 fsync 并吞异常；
- `Swarm._gateway_token` 仍为 run 级共享 token，`_runtime_env_for()` 把同一个 token 注入多个
  Worker；
- `CliResult` 尚无 `invocation_id`，`_stream_cost()` 仍是 invocation 汇总结算点；
- `LLMClient._record_cost()` 只在成功且 provider 返回 usage 时记录；Titler/Summarizer 仍存在
  裸 `LLMClient()` 构造；
- `RunManager.create()`/`_fresh_bus()` 当前只注册普通 `store.sink`，尚无 checked sink；
- run id（如 `run-0001`）与 challenge id 是不同身份，canonical usage 不能省略
  `challenge_id` 后仍声称可独立重建 challenge projection。

这些事实与 v4.1 的改造动机一致，也证明本轮补入的 identity/token 接线不是文字润色。

## 2. 第四轮 Go 条件逐项结论

| 第四轮条件 | v4.1 核验 | 裁决 |
|---|---|---|
| 自包含恢复已认可契约 | WorkerClaims、五个 token API、bridge、producer、双账户、预算与 reconciliation 已重述 | 通过 |
| 完整测试矩阵 | docs/14 §8 连续列出 1–30，并由本轮补齐 lifecycle/compatibility/postflight 细节 | 通过 |
| `call_outcome` / `usage_status` 分离 | schema、journal terminal 与测试均使用双维度 | 通过 |
| internal durable started/finished | Gateway/internal 共用 run-scoped `UsageJournal`，started-only 折叠 unknown | 通过 |
| checked flush/fsync | `append_checked` 明确 flush + `os.fsync`，失败不在线可见 | 通过 |
| normal/checked 双算法 | normal emit 不改；checked 使用 paired critical sink，跳过 normal SessionStore sink | 通过 |
| postflight fail-stop/reconcile | terminal journal 或 canonical 失败均转 failed，原 identity 重建，成功回 ready | 通过 |
| fallback invocation identity | invocation 开始生成，随 CliResult 稳定携带，settlement retry 不重生成 | 通过 |
| injected SpawnGuard | RunManager driver 与 Swarm bootstrap/review/recon/recovery 都在真实 launch 前检查 | 通过 |
| per-worker token exec-env 接线 | 已明确删除 run 级 `_gateway_token`，逐 Worker 签发/注入/finally revoke | 通过 |
| 确定性测试锁定 | 1–30 覆盖幂等、崩溃、并发、replay、budget、消费者兼容与 gate 红线 | 通过 |

## 3. 实施许可与边界

### 3.1 Phase 1 获准范围

只实施：

- `SessionStore.append_checked()`；
- `EventBus` 的单一 paired critical sink 注册；
- `EventBus.emit_checked()`；
- `RunManager.create()` / `_fresh_bus()` 的 critical sink 重接；
- docs/14 测试 6–10 及 normal emit 兼容回归。

Phase 1 **不得**提前加入 USAGE_RECORDED producer、token migration、预算 gate 或 UI；这些属于
后续阶段。现有 `EventBus.emit()` 的对象 mutation、seq、ring、普通 sink isolation、fan-out
顺序和回归测试必须保持兼容。

### 3.2 后续阶段

Phase 2–6 可按 docs/14 §9 顺序继续，不要求每阶段重新做架构评审；但每阶段必须：

1. 先写对应确定性测试；
2. 只实现该阶段的接口；
3. 运行阶段测试与 `uv run pytest -q`；
4. 绿色后独立提交，再进入下一阶段。

若实现需要改变 canonical identity、producer 互斥、checked commit point、ledger fail-stop 或
run-total budget 语义，才应暂停并回到设计评审。

## 4. 非阻断实施注意事项

1. `flush + fsync` 在 EventBus lock 内会阻塞 owning loop；这是 Phase 1 正确性优先的已接受
   代价。后续若优化到线程池，必须保持“checked 持久化成功前不进 ring/fan-out”和全局 seq
   顺序，不能先异步发布再补写；
2. checked persistence 失败允许 seq 空洞；subscriber/replay 不得假设 seq 连续，只能要求
   单调；
3. non-critical sink 在 checked commit 后仍应 best-effort 隔离。它失败不能把已 durable 的
   canonical event误判为 canonical append 失败；
4. preflight accounting failure 在 Gateway HTTP 路径映射为 503；internal 调用应抛统一的
   `AccountingUnavailable`（由调用方形成运行错误反馈），不要让核心 LLMClient 伪造 HTTP
   response；
5. started bridge timeout/cancel 后即使产生孤立 started，reconcile 也必须诚实折叠为
   interrupted/unknown，不得猜测 0 token/USD。

## 5. 红线

本批准不授权修改：

- `dswarm/solver/gate.py` provenance gate；
- `cli_solver.py` anti-laundering；
- shared evidence graph 的事实语义；
- 现有 `COST_UPDATE` consumer contract；
- normal `EventBus.emit()` 的 best-effort 兼容行为。

M5 可以进入 Phase 1。完成 Phase 1 后应提交其代码 diff、测试 6–10 和全量 pytest 结果，再继续
Phase 2。
