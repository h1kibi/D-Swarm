> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../00-architecture-spec.md) 取代；本文保留作为时代记录。

# M5 token accounting 唯一账本 RFC v4.1（自包含实施规范）

> 状态：**RFC v4.1，已获第五轮评审批准（2026-08-15）**。第四轮评审见
> [docs/18](18-m5-unique-ledger-rfc-v4-review.md)，最终放行见
> [docs/19](19-m5-unique-ledger-rfc-v4-1-review.md)。本版按要求：
> ① 恢复为自包含规范（v3 已认可契约不再以"保留不变"指代，全文重述）；
> ② 拆分 `call_outcome` 与 `usage_status`；③ 统一 `UsageJournal` 承载 durable started；
> ④ `emit_checked` 定稿为 append_checked(flush+fsync) 双算法；⑤ 定义"上游已计费后 canonical
> 失败"的 failed→rebuild 行为 + `SpawnGuard` 注入。
> 本文档获批即作为 M5 实施依据；实施前不新增任何二次计费路径。

---

## 1. 已定稿契约（完整重述，实施依据）

### 1.1 Per-worker token

```python
@dataclass(frozen=True)
class WorkerClaims:
    run_id: str
    challenge_id: str | None
    worker_instance_id: str          # spawn UUID4
    solver_id: str | None
    profile_id: str
    configured_account_id: str | None
    token_scope: str                 # "worker" | "review" | "recon" | "btw"

class ModelGateway:
    _tokens: dict[str, WorkerClaims]       # token -> claims
    _run_tokens: dict[str, set[str]]       # run_id -> {token}（不互撤）
    max_active_tokens: int = 1024

    def issue_worker(self, claims) -> str          # 达上限 → raise TokenCapError → 告警
    def claims_for_token(self, token) -> WorkerClaims | None
    def revoke_token(self, token) -> None
    def revoke_worker(self, worker_instance_id) -> None
    def revoke_run(self, run_id) -> None
```

- worker/review/recon/BTW 各自独立 token；BTW 每次 side-worker 一个；
- **claims 在鉴权入口捕获为 immutable snapshot**；token 中途 revoke 只阻止新请求，已开始的
  streaming 请求继续用入口 claims 结算；
- **无 LRU 驱逐**：worker/BTW `finally` → revoke；run 收尾 → revoke_run；硬上限达 → 拒绝
  新签发 + `BUDGET_ALERT(level=error, reason=token_cap)`；
- **现有 run 级 token 路径必须删除**：在 `_make_cli_worker()` 已解析 profile、solver identity
  后生成 `worker_instance_id`，构造 claims 并 `issue_worker()`；token 以显式参数传入
  `_runtime_env_for(..., task_token=token)`，只注入该 Worker 的 exec env。统一 async task wrapper
  在正常、异常、取消及 spawn rollback 的 `finally` 中 `revoke_token(token)`，run teardown 再
  `revoke_run(run_id)` 兜底。不得继续读取或复用 `Swarm._gateway_token`；BTW 使用同一流程签发
  独立 `token_scope="btw"` token。

### 1.2 双账户身份

`configured_account_id`（配置期望）/ `billing_account_id`（实际扣费），均为
`str | None`（未知一律 None，禁 `""` 假桶）。account 预算 blocker **只读
billing_account_id**。gateway 在"按 profile 路由上游 key"RFC 落地前，
`billing_account_id="pi-main"`；internal/fallback 未知 → None。

### 1.3 Gateway acknowledged bridge

```python
class GatewayUsageBridge:
    def __init__(self, owning_loop, *, maxsize: int = 256)
    def submit(self, record, timeout: float = 5.0) -> bool:
        # loop 已关闭 → False；inflight 满 → 阻塞至 timeout（backpressure）
        # future = asyncio.run_coroutine_threadsafe(writer.write(record), loop)
        # future.result(timeout)；异常/超时 → False。绝不 fire-and-forget。
    async def drain(self) -> None     # shutdown hook
```

### 1.4 Producer 互斥与 fallback 防双计

| producer | record_kind | usage_status |
|---|---|---|
| gateway / internal | provider_call | measured / unknown |
| fallback（非 gateway 的 pi invocation 汇总） | invocation_aggregate | estimated / unknown |

- 持 gateway token 的 worker：fallback **禁记**；
- 上游已请求但无 usage 返回 → 必记 `unknown`（不得丢弃，容差 0）；
- BTW 按**真实传输路径**分类：container BTW（注入 gateway token）→ `gateway, scope=btw`；
  host 直连 `LLMClient` 的 BTW → `internal`。

### 1.5 消费者兼容

`COST_UPDATE` 仅保留 solver/challenge/global scope；profile/account 预算经
`BUDGET_ALERT` + budget snapshot API（`GET /api/runs/<id>/budget`）展示；Web/TUI/BTW
reducer 零改动；旧 JSONL 重放语义不变（兼容测试锁定）。

### 1.6 预算契约

run-total；warn ≥ 80% `warn_at_tokens` → `BUDGET_ALERT(level=warn)`；cap ≥ `token_budget`
→ `BUDGET_ALERT(level=cap)` + profile 进 dispatch-blocked；resume 动作仅
`raise_cap`/`override`（`BUDGET_ACTION` 事件持久化，gate 折叠）；profile 与 account 两个
独立 blocker（任一超限继续拒绝新 spawn，billing 维度判 account）；cap 不强杀运行中
worker；stop/finalize 无预算门；无参数 resume 不解除 block。

### 1.7 Reconciliation（容差 0）

Gateway journal 与 canonical `USAGE_RECORDED` 严格一致；internal journal 同理；缺 usage
显式 unknown，不允许误差带。

---

## 2. 统一 `UsageJournal`（阻断 3：internal 的 durable started 载体）

Gateway 与 internal 共用的 run-scoped crash-recovery journal：

```text
sessions/<run_id>-usage-journal.jsonl
首行 header: {"format":"usage-journal","version":1}
记录: {provider_call_id, phase: "started"|"finished", ts, claims 快照字段, call_outcome?,
       usage_status?, usage?, provider_call_id 唯一}
```

```python
class UsageJournal:
    def __init__(self, path: Path)          # 单文件写锁 + flush + fsync
    def append_started(self, rec) -> None    # 预写失败抛异常（fail-closed 依赖此语义）
    def append_finished(self, rec) -> None
    def reconcile(self, canonical_usage_ids: set[str]) -> list[UsageRecord]
        # finished 缺 canonical → 返回原 terminal；只有 started → 合成
        # interrupted/unknown terminal。均沿用原 provider_call_id/usage_id 幂等补写。
```

- Gateway：`started`（claims 快照）→ 上游 → `finished`（call_outcome + usage_status + usage）
  → canonical（§4 顺序）；
- internal `LLMClient.chat()`：生成 provider_call_id → journal.started → 请求 →
  `finally` journal.finished → canonical；
- **崩溃折叠**：只有 started 无 finished → `call_outcome=interrupted,
  usage_status=unknown, tokens/usd=None`（**绝不折叠成零费用**）；
- **fail closed**：started 预写失败 → 不调用上游 → 503 `accounting_unavailable` +
  `BUDGET_ALERT(level=error)`（仅发生在发请求之前；上游已发后见 §5，绝不事后 503）。

## 3. UsageRecord schema（阻断 2：status 拆分）

```python
call_outcome: str    # succeeded | provider_error | transport_error |
                     # timeout | cancelled | interrupted
usage_status: str    # measured | estimated | unknown
# 两个独立维度：调用是否成功 ≠ usage 是否完整
# （provider_error 仍可能产生可计费 usage → call_outcome=provider_error,
#   usage_status=measured 是合法组合）

@dataclass(frozen=True)
class UsageRecord:
    usage_id: str            # provider call: usage::<run_id>::<producer>::<provider_call_id>
                             # fallback:      usage::<run_id>::fallback::<invocation_id>
    producer: str            # internal | gateway | fallback
    record_kind: str         # provider_call | invocation_aggregate（与 id 字段互斥）
    provider_call_id: str | None
    invocation_id: str | None
    run_id: str
    challenge_id: str | None
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None
    call_outcome: str
    usage_status: str
    input_tokens: int | None   # unknown → None，绝不 0
    output_tokens: int | None
    usd: float | None
```

**invocation_id 定稿**：在 CLI invocation **开始时**生成（与 worker_instance_id 同源），
随 `CliResult` 稳定携带；**retry/recovery 不得重新生成**（同一 invocation 的多次结算
尝试共用同一 id → usage_id 幂等收敛）。

## 4. EventBus 双算法与 append_checked（阻断 4：真 durability）

已核实 `SessionStore.append` 无 flush/fsync（session_store.py:36-43），不能作为 crash-safe
commit point。定稿两个独立算法：

```python
# emit（现有行为，不变）：
#   锁内: seq → ring.append → 全部 sinks best-effort（异常吞）→ fan-out

# emit_checked（usage 专用）：
#   锁内: seq → checked_sink(event)                  # SessionStore.append_checked
#         失败 → 不进 ring、不执行 non-critical sinks、不 fan-out、raise
#         成功 → ring.append → non-critical sinks best-effort
#                （跳过 paired normal SessionStore sink，防重复追加）→ fan-out
```

```python
# core/session_store.py
async def append_checked(self, event: Event) -> None:
    # 与 append 共用 per-run asyncio lock 和 JSONL 格式；追加后
    # f.flush() + os.fsync(f.fileno())；异常向上抛

# core/event_bus.py
def add_critical_sink(self, normal_sink, checked_sink) -> None:
    # v1 只允许一对；normal emit 使用 normal_sink，emit_checked 使用 checked_sink
    # 并从 non-critical 循环中排除 paired normal_sink。
```

- `SessionStore` 通过 `add_critical_sink(store.sink, store.append_checked)` 注册为**唯一
  critical sink**；`emit_checked` 对其走 checked 路径，不走 paired normal sink（**同一事件
  绝不 double-append**）；其余 non-critical sink 与现有 `emit()` 一样逐个隔离异常，不得把已
  durable 的 canonical event 反向判成失败；
- seq 允许空洞；未持久化事件对在线消费者不可见；
- **重试规则**：新 `Event` 对象 + 新 seq + 同 `usage_id`（投影幂等折叠）；
- `RunManager._fresh_bus()` 重建时必须重新注册 critical sink（测试锁定）。

## 5. 上游已计费后 canonical 失败的恢复（阻断 5）

窗口包括：

1. `started journal 成功 → 上游已执行/已开始 streaming → finished journal 写失败`；
2. `finished journal 成功 → emit_checked(USAGE_RECORDED) 失败`。

两者都发生在上游可能已计费之后，**不得返回 503**（streaming headers 可能已发出），行为定稿：

```text
finished journal 成功时：该 terminal = recovery truth
finished journal 失败时：保留 started；恢复时诚实折叠为 interrupted/unknown
→ ledger_state = failed（立即）
→ 阻止后续 provider call 与一切 worker spawn（§6）
→ ledger_error = "journal_terminal_append_failed" | "canonical_append_failed"
→ 先写内存 runtime-health + logger.error，再 best-effort emit BUDGET_ALERT(level=error)
  （告警不得只依赖正在失败的 checked SessionStore 路径）
→ snapshot API 直接读取内存 ledger_state/ledger_error，在线仍可见
→ rebuild：journal.reconcile 按原 usage_id 幂等补写 canonical
→ 补写成功后 ledger_state 回 ready
```

（fail-closed 503 仅限"发上游之前"的 started 预写失败；发上游之后一律走上述 failed 路径。）

## 6. ledger_state 状态机 + SpawnGuard 注入（阻断 5 接口补全）

```text
ledger_state: "rebuilding" | "ready" | "failed"
ledger_error: str | None
```

```python
class SpawnGuard:
    """RunManager 构造，注入 Swarm 与所有 spawn 路径。"""
    async def ensure_ready(self, run_id: str) -> None:
        # rebuilding → 有界等待（超时按 failed）；ready → 放行；
        # failed → raise LedgerNotReady(ledger_error)
```

- **注入覆盖**（不只 HTTP API）：`RunManager.start()` 的 `scheduler.submit()`、
  `resolve()` 的 submit、scheduler 排队 driver 启动、`_ensure_standby()` 内 `_go()`、
  BTW side-worker、**Swarm 内部 bootstrap/review/recon/recovery worker 创建**
  （经 `SpawnGuard` 从 RunManager 注入 Swarm，检查点 = spawn/driver 统一 helper）。
- rebuild 顺序：replay `USAGE_RECORDED` → replay `BUDGET_ACTION` →
  journal.reconcile 补写 → 重建五层投影与 gate → ready；失败 → failed + 告警 +
  提供 rebuild 操作；stop/finalize 任何状态可用。

## 7. internal producer 接线（含 Titler/Summarizer/BTW）

```python
@dataclass(frozen=True)
class UsageContext:
    run_id: str
    challenge_id: str | None
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None
    producer: str = "internal"

LLMClient(..., usage_writer: UsageWriter | None = None,
          usage_context: UsageContext | None = None)
# usage_writer = UsageJournal + emit_checked 的封装；None → 行为同现状（不声称已入账）
```

- `chat()` 生命周期：provider_call_id → journal.started → 请求 → finally
  journal.finished(call_outcome, usage_status, usage) → emit_checked（checked 成功才算完成；
  失败走 §5）；
- 接线范围：Reason 规划器、Titler（现 titler.py:90 裸 `LLMClient()`）、Summarizer
  （现 summarizer.py:140/213 裸 `LLMClient()`）、host BTW 直连；
- 排除（明示）：provider probe、connectivity test 诊断调用不入账（后续另议）；
- container BTW → gateway producer（§1.4）。

## 8. 最终测试矩阵（自包含，1–30）

1. usage_id 幂等（双重 append 收敛）；2. 三 producer 互斥（gateway-token worker 禁 fallback）；
3. COST_UPDATE 消费回归（reducer 逐字节同行为）+ normal `emit()` raising sink 仍不阻断健康
sink/fan-out；4. per-worker token 并发 issue 不互撤 + revoke 三接口 + Worker finally/spawn
rollback 撤销；5. claims（含 challenge/solver）打标与每 Worker exec env 注入端到端；
6. emit_checked 失败不进 ring/fan-out；7. retry 新
Event/新 seq/同 usage_id 计一次；8. `_fresh_bus` 后 critical sink 仍在；9. append_checked
真 fsync（崩溃后可读）；10. 无 double-append（critical 直写跳过通用 sink 循环）；
11. journal：started 在 mock upstream 收到请求前已 durable；12. 只有 started 无 finished →
折叠 interrupted/unknown/None；13. started 预写失败 → 上游请求 0 次 + 503 + 告警；
14. journal 并发写不交错；15. reconcile 幂等补写；16. call_outcome×usage_status 组合合法
（provider_error+measured 可表达）；17. 上游后 finished-journal 失败或 canonical 失败 → failed →
阻止 spawn/provider call → 内存告警可见；streaming 不事后 503；rebuild 补写 → ready；
18. invocation_id 在 invocation 开始生成、retry 不重生成；
19. SpawnGuard：Swarm 内部 bootstrap/review/recon/recovery 均受 gate；failed 拒绝 + stop/
finalize 正常；20. token hard cap 拒绝不驱逐；21. streaming 中 revoke 仍用入口 claims；
22. Titler/Summarizer 成功/无 usage/超时三态终态；23. container BTW → gateway；24. 五层投影
与 replay 逐字段相等；25. warn→cap→block→raise_cap/override→恢复；26. 无参数 resume 不解除；
27. profile/account 双 blocker 独立（billing 维度）；28. budget snapshot API 一致；
29. ledger_state 三态 + rebuild 操作；30. 全量 pytest 绿 + provenance gate、anti-laundering、
shared evidence graph 事实语义原样。

（编号 1–30 覆盖四轮评审全部验收；实施时每项落一条确定性测试。）

## 9. 实施顺序（分阶段，每阶段可独立提交、全量绿）

1. **Phase 1（已完成，2026-08-15）**：`SessionStore.append_checked` + `EventBus.emit_checked`（双算法）+ critical
   sink 注册（测试 6-10）；已通过目标测试、相关 Web/SessionStore 回归和全量 pytest；
2. **Phase 2（已完成，2026-08-15）**：`UsageJournal`（journal + 崩溃折叠 + reconcile；测试 11-15）；已实现 started/finished `flush+fsync`、同路径并发串行化、started-only `interrupted/unknown/None` 恢复、preflight `AccountingUnavailable` 与 canonical usage_id 幂等折叠；补充契约测试拒绝空字符串账户假桶、非法 producer/status 组合及损坏 started 行；目标测试、Web/SessionStore 相关回归与全量 pytest 均以退出码 0 通过；
3. **Phase 3（已完成，2026-08-15）**：`WorkerClaims` + ModelGateway per-worker token 改造 + hard cap（测试 4-5、20-21）；已完成普通 Swarm worker、review/recon worker、容器 BTW 的独立 token 与 exec-env 注入，显式 endpoint 不签 gateway token，构造失败/runtime finally/run teardown 均有撤销保护；定向回归与全量 pytest 绿色。
4. **Phase 4（已完成，2026-08-15）**：producer 接线——gateway journal 两阶段 + bridge（§1.3）+
   internal `UsageContext`/`UsageWriter` + Titler/Summarizer/BTW（测试 1-2、16、18、22-23）；
   `tests/test_phase4_wiring.py`、LLM、ModelGateway、Web 回归通过。
5. **Phase 5（已完成，2026-08-15）**：ledger 重建 + SpawnGuard + 五层投影 + `ProfileBudgetGate` +
   `BUDGET_ALERT/ACTION`（测试 17、19、24-28）；`tests/test_phase5_ledger.py` 17 项通过。
6. **Phase 6（已完成，2026-08-16）**：budget snapshot/rebuild API + UI 展示（测试 28-29）+
   全量回归（测试 3、30）；前端 Vitest 与 Next production build 通过。

## 10. 未决问题

第五轮评审已批准本规范并将身份完整性、per-worker exec-env 接线、terminal journal 失败、
critical sink 配对接口及确定性测试细节直接补入正文；无新增未决项。实施按 Phase 1→6 顺序，
每阶段独立测试、全量绿色后再进入下一阶段；RFC §8 的 30 项验收矩阵已全部有测试或回归证据，M5 v4.1 收尾完成。后续仅在偏离上述
canonical identity/durability/producer 互斥契约时才需要重新设计评审。
