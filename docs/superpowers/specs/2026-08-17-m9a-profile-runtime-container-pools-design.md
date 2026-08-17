# M9a Profile/Runtime 长生命周期容器池设计

**日期：** 2026-08-17
**状态：** 设计已获操作员逐节确认；尚未实施
**范围：** M9a，仅重构真实 Worker runtime 的容器编排、身份、探测、恢复与入口策略
**不在范围：** M9 的 Verified-PoC、scope 审计、cleanup registry；M9a 也不修改 provenance gate、事件底座、M5 账本语义或 SharedGraph append-only 语义

---

## 1. 决策摘要

M9a 将 D-Swarm 的真实执行路径从“一个 run 共享一个由首个 Worker 决定的容器”升级为：

```text
一个 run
└─ 一个 run-scoped ContainerPoolManager
   ├─ RuntimePoolIdentity(run_id, PoolKey-A) → 一个长生命周期容器
   ├─ RuntimePoolIdentity(run_id, PoolKey-B) → 一个长生命周期容器
   └─ RuntimePoolIdentity(run_id, PoolKey-C) → 一个长生命周期容器
```

其中：

1. `PoolKey` 表示一个冻结的 profile/runtime 执行规格；
2. `RuntimePoolIdentity = (run_id, PoolKey)`，Pool 永不跨 run 复用；
3. 同一 PoolKey 在一个 run 内最多对应一个活跃容器，V1 不做横向扩容；
4. 同一 Pool 内可以并发启动多个独立 Worker 进程；
5. 所有真实 Web/TUI swarm 默认 Docker-first；
6. 宿主 local Worker 只有在 `--local-dev` **且** `DSWARM_ALLOW_LOCAL_WORKERS=1` 同时满足时才允许；
7. Docker、镜像、网络、RCP、身份或 Probe 失败时 fail closed，绝不静默调用宿主 `pi`；
8. run 创建时冻结非密钥 runtime snapshot，运行中全局设置变化只影响新 run；
9. Pool 第一次被真实派发使用时才启动，并在目标长生命周期容器内完成真实、可计费、tool-disabled 的 Pi one-turn Probe 后才能进入 `ready`；
10. 进程级 reopen 必须先完成 run-wide stale-runtime cleanup barrier，无法证明旧 runtime 已停止则拒绝 reopen。

M9a 是 forward-only 的生产架构迁移。旧 `container_dockerexec` 只保留为受限测试/unsafe local-dev 工具，不是生产 feature-off，也不是自动回退路径。

---

## 2. 动机与现状

当前实现的核心假设是“一个 run 一个容器”：

- `dswarm/solver/container_exec.py` 的模块契约与 `ContainerHandle` 都按 per-run 单容器设计；
- `Swarm` 保存单值 `_container_handle`、`_container_runtime_id`、`_container_unavailable`；
- `worker_runtime_mixin._container_for_engine()` 让第一个 Worker 的 runtime/image 实际冻结整个 run；
- RCP receiver 以 `run_id` 存 `_tokens` 与 `_links`，同 run 第二个容器会覆盖第一个；
- credential projection 会把较大的账户投影挂进容器，并存在 raw-store fallback；
- legacy cleanup 使用宽松 name filter，不足以证明被删除容器属于目标 run；
- `run.sh web` 与 `run.sh tui --swarm` 仍可在宿主直接启动 control plane；
- 正常容器失败路径仍有机会解析或接触宿主 `pi`。

这些约束无法正确表达“不同 profile 使用不同镜像、provider、runtime、network 或资源限制”，也无法满足操作员希望的宿主隔离：真实 swarm/worker 应在 Docker 内运行，不能读取、探测或影响宿主 `pi` 与 `~/.pi`。

---

## 3. 目标、非目标与不可破坏约束

### 3.1 目标

- 每个 run 可同时拥有多个 profile/runtime Pool；
- 一个 PoolKey 对应一个长生命周期容器；
- 同 Pool Worker 作为独立进程并发执行；
- run 创建时冻结可审计的 runtime 配置；
- secret bytes 可轮换，但不能使执行静默换账户或回落宿主环境；
- Pool 故障局部隔离，可从冻结 snapshot 中选择兼容替代 profile；
- RCP、Worker、usage、日志与 API 都能定位到 pool generation；
- Web/TUI 真实执行默认容器化；
- reopen、teardown 与 legacy cleanup 有可证明的安全边界；
- 默认单元测试不依赖 Docker、网络、真实 key 或真实费用。

### 3.2 非目标

- V1 不在同一 PoolKey 下启动多个容器做横向扩容；
- 不跨 run 复用 warm Pool；
- 不把 Kubernetes、Nomad 或远程 Docker 编排纳入本阶段；
- 不提供运行中修改 frozen profile/runtime 配置；
- 不将 runtime failure 写成 challenge evidence、dead-end 或 finding；
- 不改 provenance gate、flag acceptance、M3/M5/M7/M8 的 canonical 契约；
- 不解决不同 Worker 在同一 Linux UID 下的强对抗隔离；M9a 的安全边界是 Pool、挂载、进程组、HOME/session 与 credential binding 隔离，不把同 Pool 内 Worker 宣称为恶意租户级 sandbox。

### 3.3 不可破坏约束

- accepted flag 仍必须来自真实执行输出并通过现有 hardcoded provenance gate；
- SharedGraph 仍 append-only；
- runtime diagnostic 不能伪装为 evidence；
- Gateway Pool 不获得真实 provider key；
- Worker 永不挂宿主 HOME、`~/.pi`、Docker socket、其他 run 或其他 Pool credential；
- 任何失败都不得触发宿主 `pi` fallback。

---

## 4. 术语与身份模型

### 4.1 RuntimePolicy

`RuntimePolicy` 是 run 创建时冻结的不可变策略，至少包含：

```python
@dataclass(frozen=True)
class RuntimePolicy:
    mode: Literal["docker", "local_dev"]
    local_dev_cli_flag: bool
    local_dev_env_allowed: bool
    max_pools_per_run: int = 32
    pool_max_concurrent_workers_default: int | None = None
    probe_timeout_seconds: float = 45.0
    recovery_attempts_per_episode: int = 1
    snapshot_version: int = 1
```

规则：

- 生产默认 `mode="docker"`；
- `local_dev` 只有 CLI flag 与环境变量双重允许时才能构造成功；
- `max_pools_per_run` 默认 32，合法范围 `1..128`；
- Pool 并发上限未配置时继承 run 的 `max_workers`；
- `RuntimePolicy` 必须注入 `Swarm`/driver 层，不能只在 HTTP/CLI 入口判断；
- 禁止通过 `PYTEST_CURRENT_TEST`、检测 pytest 进程或类似 ambient 状态自动放行 local Worker。

### 4.2 PoolKey

`PoolKey` 由规范化执行规格的 canonical JSON 计算：

```text
pool_key = "pool-v1::" + blake2b(canonical_json(spec), digest_size=20).hexdigest()
```

禁止 Python 内置 `hash()`。canonical JSON 必须：

- UTF-8；
- key 字典序；
- 无无意义空白；
- 数值使用已校验的稳定表示；
- enum 在计算前 canonicalize；
- 未知字段拒绝，而不是忽略。

参与摘要的字段：

```text
profile_id
runtime_kind / engine
resolved_image_id
network policy/name
resource limits (cpu/memory/pids/tmpfs 等)
credential_binding_id
provider_binding_id
model identity
numeric uid/gid
runtime feature set / protocol version
```

不参与摘要的字段：

```text
secret bytes
credential version
probe timestamp
container id/name
pool_instance_id
generation
worker_instance_id
```

credential version 不改变 PoolKey，但会使 Probe cache 失效。binding identity 改变会生成不同 PoolKey。

### 4.3 三层 runtime 身份

```text
run_id
└─ pool_id                 # PoolKey 的稳定、URL-safe 表示
   └─ pool_instance_id     # UUID4，每次容器 generation 新值
```

并定义：

```python
RuntimePoolIdentity = tuple[str, str]  # (run_id, pool_id)
```

- `pool_id` 在一个 run 的 snapshot 生命周期内稳定；
- `pool_instance_id` 每次启动或重建容器都变化；
- `generation` 从 1 单调增加，用于人类诊断，不代替 UUID 身份；
- Worker 另有 UUID4 `worker_instance_id`；
- Probe 另有 UUID4 `probe_id`，并使用独立 `worker_instance_id`；
- usage identity 继续遵守 M5，不得用 pool generation 的局部计数制造跨重启碰撞。

### 4.4 Docker labels

每个新 Pool 容器必须携带：

```text
com.dswarm.managed=true
com.dswarm.run_id=<run_id>
com.dswarm.pool_id=<pool_id>
com.dswarm.pool_instance_id=<uuid>
com.dswarm.generation=<positive integer>
```

label 值在写入前做长度和字符校验。cleanup 必须 inspect 后逐字段精确比较，不能仅依赖 container name。

---

## 5. Frozen runtime snapshot

### 5.1 路径与所有权

run 创建时写入：

```text
sessions/<run-id>/.runtime/pool-snapshot.v1.json
```

`.runtime` 为 coordinator-private：

- 不挂入 Worker；
- 不进入 challenge evidence；
- API 只返回经过 allowlist/sanitize 的派生视图；
- 使用 temp → flush → fsync → atomic replace 的 durable 写入方式。

### 5.2 创建流程

run 在进入可派发状态前：

1. 读取并规范化所有可用于该 run 的 profile/runtime 配置；
2. 检查 Pool 数量不超过 `max_pools_per_run`；
3. 对每个唯一 image tag 执行 Docker inspect/pull policy，解析成 immutable image ID；
4. 对每个唯一 image ID 做无 secret、无共享 mount、`network=none` 的轻量身份 Probe，取得 `kali` 的数字 UID/GID；
5. 验证同 run 所有 Worker image 的 UID/GID 完全一致；
6. 解析有效 network、resource、provider/credential binding identity；
7. 计算 PoolKey；
8. durable 写 snapshot；
9. 后续 lazy Pool 只能使用 snapshot 内的 image ID 与冻结规格。

身份 Probe 不是 LLM/CLI Probe，不访问 provider、不产生费用，也不创建长生命周期 Pool。若无法证明 image 中存在 `kali`，或 UID/GID 不一致，run 创建失败并报告 `worker_identity_mismatch`。

### 5.3 Snapshot 内容

snapshot 至少包含：

```json
{
  "version": 1,
  "run_id": "...",
  "created_at": 0.0,
  "runtime_policy": {},
  "shared_uid": 1000,
  "shared_gid": 1000,
  "pools": [
    {
      "pool_id": "pool-v1::...",
      "profile_id": "...",
      "runtime_kind": "pi",
      "resolved_image_id": "sha256:...",
      "requested_image_ref": "...",
      "network": {},
      "resources": {},
      "credential_binding_id": "...",
      "provider_binding_id": "...",
      "model": "...",
      "uid": 1000,
      "gid": 1000,
      "pool_max_concurrent_workers": 8
    }
  ]
}
```

不得写入 secret、token、完整 account 配置、宿主 HOME 或未经净化的 host path。

### 5.4 配置变化语义

- snapshot 创建后，全局 profile/image/network/resource 设置变化只影响新 run；
- lazy Pool 仍使用冻结的 image ID，tag 后续移动不影响当前 run；
- secret bytes 不冻结，可按 binding 取当前版本；
- binding 被删除时 Pool 进入 `degraded`，不得换到默认账户、环境变量账户或其他 binding；
- snapshot 本身不因 secret 轮换重写。

---

## 6. Secret、credential 与挂载边界

### 6.1 Secret 规则

冻结的是账户/provider binding identity，不是 secret bytes：

- 新 Worker/Probe 在调用开始时解析当前 secret version；
- 进行中的 provider 调用保持入口时取得的 secret/claims 快照；
- secret 轮换不改变 PoolKey；
- credential version 变化使该 Pool 的成功 Probe cache 失效；
- binding 消失或无法读取时 Pool degraded；
- 禁止 raw account store fallback；
- 禁止读取宿主环境中的同名 key 作为替代。

### 6.2 Gateway Pool

Gateway 路径：

- Pool 容器不挂真实 provider key；
- 每个 Worker/Probe 获得独立 M5 per-worker task token；
- claims 包含 run、worker、profile、configured account、billing account/unknown、operation kind；
- token 经 RCP Worker spec 注入对应进程，不写容器全局环境；
- Worker 结束立即 revoke；run teardown 调用 revoke_run 兜底。

### 6.3 Direct/custom Pool

Direct/custom 路径只可投影 snapshot 指定的单一 binding：

- secret 通过受控 projector 或 RCP per-worker secret channel 注入；
- 不挂完整 account store；
- 不允许 Worker 枚举其他账户；
- secret 文件/环境仅对对应 Worker 生命周期有效；
- Worker 结束时清理；清理无法确认时该 generation degraded/作废；
- diagnostics 只能记录 binding ID 的安全别名或 digest，不能记录账户名、key、token 或原始 provider 响应。

### 6.4 Mount 表

允许：

```text
同 run shared workspace
同 run evidence/blackboard/artifacts/player-facing files
Pool control socket/transport
必要的只读 runtime assets
每 Worker 独立 HOME/session/workdir 根
```

禁止：

```text
host HOME
host ~/.pi
Docker socket
sessions/<other-run>
其他 Pool credential
完整 credential account store
sessions/<run-id>/.runtime
challenge solution/reference files
```

control-plane 容器为了创建 sibling Worker 可以挂宿主 Docker socket；该能力绝不传给 Worker Pool。

---

## 7. 组件架构

### 7.1 新组件

```text
Swarm
└─ ContainerPoolManager (run-scoped)
   ├─ RuntimePolicy
   ├─ RuntimeSnapshotStore
   ├─ ContainerPoolEntry[pool_id]
   │  ├─ ContainerRuntimeExecutor
   │  ├─ RuntimeProbe
   │  ├─ pool semaphore
   │  └─ lifecycle diagnostics
   ├─ CredentialProjector
   ├─ RuntimeCleanupInspector
   └─ RuntimePoolView
```

拟新增/拆分的职责：

- `runtime_policy.py`：policy、PoolKey canonicalization、snapshot model/validation；
- `runtime_snapshot.py`：durable snapshot 与 private state；
- `container_pool.py`：manager、entry、lease、状态机、single-flight；
- `container_runtime.py`：单个 container generation 的 create/exec/signal/close；
- `runtime_probe.py`：tool-disabled Pi CLI Probe 与结果分类；
- `runtime_cleanup.py`：label/inspect 证明与 reopen barrier；
- `runtime_diagnostics.py`：sanitized lifecycle JSONL 与 API view；
- `control_receiver.py`：RCP v2 pool-instance identity；
- `container_exec.py`：逐步收缩为兼容 facade 或底层 executor，不能继续持有 run-global 假设。

具体文件名可在实施计划中按现有包边界微调，但职责不能重新合并成一个单例容器对象。

### 7.2 公共接口

```python
class ContainerPoolManager:
    async def acquire(
        self,
        *,
        pool_id: str,
        worker_instance_id: str,
        operation_kind: str,
    ) -> WorkerRuntimeLease: ...

    async def mark_failure(
        self,
        *,
        pool_instance_id: str,
        failure: RuntimeFailure,
    ) -> None: ...

    async def close(self) -> PoolCloseReport: ...

    def snapshot_view(self) -> tuple[RuntimePoolView, ...]: ...
```

```python
class WorkerRuntimeLease:
    pool_id: str
    pool_instance_id: str
    generation: int
    worker_instance_id: str
    executor: ContainerRuntimeExecutor

    async def release(self) -> None: ...
```

Lease 必须：

- 在 Pool `ready` 后才返回；
- 占用 Pool semaphore；
- 一次只属于一个 Worker operation；
- release 幂等；
- manager close 后不可新 acquire；
- Worker 结束、取消、异常都在 `finally` release。

### 7.3 Pool 状态机

```text
new → starting → probing → ready
ready → recovering → starting → probing → ready
new/starting/probing/ready/recovering → degraded
任意非终态 → stopping → stopped
```

规则：

- 状态变化在 entry 锁内完成；
- 并发首次 acquire 只触发一次 create/Probe，其他调用等待同一 future；
- 每个 failure episode 最多自动重建一次；
- infrastructure failure 可触发一次 generation 重建；
- auth/model/config failure 不通过重建重复真实付费 Probe；
- 无法确认旧 generation 已停止时，不得启动同 identity 的新 generation；
- `degraded` 可以因明确的 credential/version/config 修复操作重新进入 recovery，但不能 ambient 自动换配置。

---

## 8. RCP v2：Pool-instance 身份

### 8.1 Receiver 数据模型

现有 `run_id → token/link` 改为：

```text
pool_instance_id → ExpectedRuntimeIdentity
pool_instance_id → token
pool_instance_id → live link
run_id → set[pool_instance_id]
pool_id → current pool_instance_id
```

`ExpectedRuntimeIdentity` 至少包含：

```text
run_id
pool_id
pool_instance_id
generation
expected image ID
protocol_version=2
```

### 8.2 Hello v2

```json
{
  "protocol_version": 2,
  "run_id": "...",
  "pool_id": "...",
  "pool_instance_id": "...",
  "token": "..."
}
```

receiver 必须同时校验全部字段。任一不匹配：

- 拒绝 link；
- 写 sanitized diagnostic；
- 不覆盖已存在 link；
- 不把失败映射成 challenge evidence。

### 8.3 API 语义

至少提供：

```text
issue_pool(expected_identity) -> token
wait_pool(pool_instance_id, timeout) -> link
link_for(pool_instance_id) -> link | None
revoke_pool_instance(pool_instance_id)
revoke_pool(pool_id)
revoke_run(run_id)
```

- 同一 `pool_instance_id` 只允许一个有效 link；
- 新 generation 不复用旧 token；
- revoke 一个 Pool 不影响同 run 其他 Pool；
- receiver/server shutdown 时 fail 所有 pending waiters；
- production 新 Pool 只接受 protocol v2；v1 仅可在明确的 legacy test/local-dev 兼容路径使用。

---

## 9. Worker 进程隔离与容量

同 Pool Worker 不是共享一个 Pi session，而是容器内独立进程。每个 Worker 必须拥有独立：

```text
worker_instance_id
process group
HOME
PI_CODING_AGENT_DIR
session/conversation state
workdir
Gateway token 或 direct secret projection
usage identity
stdout/stderr stream
signal/status lifecycle
```

共享内容仅限同 run 的 workspace/evidence/artifacts/player files。worker-private 路径采用不可碰撞 UUID，不依赖可复用的 planner intent ID。

容量：

- 每 Pool 一个 semaphore；
- `pool_max_concurrent_workers` 缺省继承 `run.max_workers`；
- Pool 满员时等待，不创建第二个相同 Pool 容器；
- 全局 WorkerLaneGate 的 ordinary/review 约束保持权威；Pool semaphore 是额外资源约束，不得绕开 lane gate；
- acquire 等待取消后不能泄漏 permit；
- Pool degraded 时 pending acquire 得到结构化 runtime error，scheduler 可选择冻结 snapshot 中的兼容 profile。

---

## 10. UID/GID 与文件所有权

- 所有 Worker image 必须存在 `kali` 用户；
- 同 run 所有 image 的数字 UID/GID 必须一致；
- UID/GID 通过 image identity Probe 动态取得，禁止硬编码 1000；
- 不一致或不存在时错误码为 `worker_identity_mismatch`，对应 Pool/run 不进入派发；
- 禁止 `chmod 777` 修复权限；
- chown 必须使用 snapshot 中经验证的数字 UID/GID；
- 所有基于 image 的权限决策必须读取实际 `ContainerHandle.image`/resolved image ID，不能回读全局默认 image；
- symlink、workspace 与 player files 的可写性在 Pool CLI Probe 前做确定性检查。

---

## 11. Lazy Pool 与真实 CLI Probe

### 11.1 Lazy 创建

run 创建只完成 snapshot 与轻量 Docker/image/identity 预检，不启动全部长生命周期 Pool。首次真实 dispatch 调用 `acquire(pool_id, ...)` 时：

1. single-flight 创建 container generation；
2. 等待 RCP v2 Hello；
3. 校验 image、labels、mounts、network、UID/GID；
4. 执行 RuntimeProbe；
5. Probe 成功后标记 `ready`；
6. 返回 Worker lease。

### 11.2 Probe 必须验证什么

Pool `ready` 必须由目标长生命周期容器内的一次真实 Pi one-turn 证明，且使用冻结的：

```text
image ID
runtime/engine
network
profile/model
credential/provider binding
UID/GID
```

仅 HTTP endpoint health check、TCP connect、image inspect 或 `pi --version` 均不能替代真实 CLI Probe。

### 11.3 Tool-disabled 契约

Probe 运行在目标长生命周期容器内，因此仅“独立 cwd”不足以证明它不能遍历共享 workspace。M9a 采用以下硬契约：

- 先在 `CliDriver`/runtime driver 增加公共 `probe_argv()` / `parse_probe_result()` 契约；
- Probe 必须使用 CLI 明确定义的 **tool-disabled、non-agentic one-turn** 模式；
- Probe 不注入 `DSWARM_BLACKBOARD_DB`、challenge prompt、target、player files、graph path 或 provenance sink；
- Probe 使用独立 HOME/session/workdir；
- 若目标 CLI/runtime 无法证明工具已禁用，该 Pool 不得被 Probe 标为 `ready`；
- 不允许退化成可使用 shell/文件工具的 agentic Probe；
- 本设计只承诺在 driver 合同层禁止工具并不注入 challenge 数据，不对容器内恶意二进制作超出 Docker/Pool 边界的声明。

### 11.4 Probe 的 M5 语义

Probe 是真实可计费调用：

- 使用独立 `probe_id`、`worker_instance_id`、task token/usage context；
- `operation_kind="runtime_probe"`；
- 归属当前 run/profile/configured account/billing account；
- 纳入 M5 run/profile/account budget；
- Probe 调用上游前必须独立通过 M5 ledger/profile/account budget gate；预算阻断时零上游请求，Pool 保持非 ready 并报告 budget-blocked，而不是伪装成 provider 故障；
- started journal 写失败时 fail closed，零上游请求；
- provider 返回 usage 时按 M5 measured 记录；无法取得单次 usage 时按既有合法 estimated/unknown/fallback 契约记录，不伪造 0；
- 不进入 solve-rate、M7/M8 数据集，不写 graph/provenance/finding/dead-end；
- Probe cost 可以在 runtime diagnostics 与预算 snapshot 中看到，但不能作为 Worker solve credit。

### 11.5 Timeout、清理与 cache

- Probe 有明确 timeout；
- timeout 后 cancel/signal 对应进程组并等待终态；
- 无法确认进程组已停止时，整个 generation 作废，先清理容器再决定是否重建；
- infrastructure error 每个 failure episode 最多重建一次；
- auth/model/config error 直接 degraded，不重复付费；
- Probe cache key 至少包含：pool_id、pool_instance_id/generation、resolved image ID、model、credential version；
- secret 轮换、model 变化、image/generation 变化均使 cache 失效；
- 一个 generation 内并发 acquire 共用同一 Probe future。

---

## 12. Dispatch 数据流

真实 Worker 的统一数据流：

```text
Reason/operator/review/recon/recovery/BTW 产生真实 shell 请求
→ SpawnGuard/M5 ledger_ready 检查
→ RuntimePolicy 拒绝非法 local path
→ 从 frozen snapshot 选择 profile/runtime Pool
→ ContainerPoolManager.acquire(pool_id)
→ lazy start + RCP Hello + Probe（如需要）
→ 创建 per-worker HOME/session/workdir/credential/token
→ ContainerRuntimeExecutor.start_worker()
→ 流式 stdout/stderr → CliSolver
→ provenance gate 按现有规则处理真实输出
→ terminal usage/worker status
→ revoke token/清理 private runtime/release lease
```

必须审计并统一接线的真实 shell 入口包括：

- initial/bootstrap Worker；
- ordinary intent Worker；
- review Worker；
- recon Worker；
- recovery Worker；
- standby 后恢复；
- `/resolve` 继续执行；
- BTW deep-audit/container BTW；
- 任何 Pi/custom CLI driver 的真实 invocation。

禁止某个旁路继续直接读写 `Swarm._container_handle` 或调用宿主 `run_cli`。

---

## 13. Docker-first Web/TUI 启动策略

### 13.1 默认入口

```text
./run.sh web
→ docker compose 启动 board + web-api + ui
→ web-api 通过宿主 Docker daemon 启动 sibling Worker Pool

./run.sh tui --swarm
→ docker compose run/等价方式启动交互式 tui-control 容器
→ tui-control 通过宿主 Docker daemon 启动 sibling Worker Pool

./run.sh tui
→ 仅宿主 mock UI；不创建真实 Worker
```

默认 Web/TUI control plane 容器可以挂 Docker socket；Worker Pool 不能。

### 13.2 Local developer escape hatch

只有两项同时满足才允许真实 host-local Worker：

```text
CLI: --local-dev
ENV: DSWARM_ALLOW_LOCAL_WORKERS=1
```

任一缺失均拒绝。Python API 直接构造 `Swarm` 时也必须通过 `RuntimePolicy` 明确满足同样条件，不能绕过入口检查。

### 13.3 Fail-closed

以下失败都必须返回结构化 runtime error，不得调用宿主 `pi`：

```text
Docker daemon/socket 不可用
image 不存在/拉取失败/inspect 失败
network 不存在或策略不合法
RCP receiver/link 失败
UID/GID 不匹配
credential projection 失败
Probe 失败
Pool cap 超限
```

正常 container path 不调用 `resolve_engine_bin("pi")`。容器内 Pi 路径由目标 image/runtime 的 Probe 和 executor 在容器内解析。

### 13.4 端口与认证

- compose 默认 host 端口绑定 loopback；
- 非 loopback bind 必须配置 Web 密码，否则拒绝启动；
- sibling Worker control receiver 默认只在 compose network 可达；
- control token 与 provider token 不写日志；
- UI proxy 保持 same-origin。

---

## 14. Pool 故障隔离、恢复与改派

### 14.1 故障分类

```text
infrastructure: daemon/container/network/RCP/process cleanup
identity: labels/hello/image/uid/gid/mount mismatch
auth: credential/provider authentication
configuration: model/profile/runtime unsupported
capacity: pool/run cap or semaphore wait
worker: 单 Worker 非零退出/超时/取消
```

- 单 Worker 失败不自动 degraded 整个 Pool，除非表明 supervisor/generation 不可信；
- identity failure 不自动恢复到 ready；
- infrastructure failure 每个 episode 最多一次容器重建；
- auth/config failure 不用重建重复调用；
- 无法确认旧进程或容器已停止时 generation 作废。

### 14.2 改派

单 Pool degraded 默认不结束 run：

- 其他 Pool/active Worker 继续；
- 替代 profile 只能来自 frozen snapshot；
- 同一方向仅更换 profile/runtime 时写 runtime failover diagnostic；
- 方向真正变化继续使用 M4 `direction_override`，不得用 runtime event 代替；
- 不从当前全局配置临时加入新 profile；
- runtime failure 不生成 dead-end、finding 或 challenge evidence。

只有当：

```text
全部兼容 Pool 不可用
AND 无 active Worker
AND 无正在进行的唯一允许 recovery
```

才把 run 标记为 `runtime_unavailable`。

---

## 15. Reopen cleanup barrier

### 15.1 为什么是 run-wide barrier

Pool-local failure 通常局部隔离；但进程级 reopen 是安全例外。旧 Worker 可能继续：

- 写同一 SharedGraph/workspace；
- 产生 provider 费用；
- 提交与新 scheduler 冲突的结果；
- 使用旧 credential/token。

因此 reopen 在任何新 cycle、Probe 或 dispatch 前必须执行 run-wide stale-runtime cleanup barrier。

### 15.2 新 Pool 清理证明

新容器只按以下证据清理：

1. Docker label `com.dswarm.managed=true`；
2. inspect 得到的 `run_id` 精确等于目标 run；
3. `pool_id`、`pool_instance_id`、generation 格式合法；
4. mount/network/image 信息与 private runtime state 可交叉验证。

禁止 container-name substring 批量删除。

### 15.3 Legacy 容器

legacy 容器没有完整 labels 时，仅在以下证据同时成立才可删除：

- exact legacy container name；
- mount source 精确指向目标 run workspace/control root；
- env/control token path 能证明目标 run；
- inspect 未显示属于其他 run。

证据不足时不删除，并拒绝 reopen；不能为了可用性冒险杀未知容器。

### 15.4 Barrier 结果

```text
发现并确认全部旧 runtime 已停止/删除 → 允许初始化新 PoolManager
任一旧 runtime 无法确认清理             → reopen fail fast，无新 dispatch
```

barrier 还要 revoke 旧 RCP pool-instance token/link 与 M5 worker token；每个清理动作有 sanitized 诊断。

---

## 16. Teardown 与生命周期终点

`RUN_FINISHED` 不是可靠的实际 teardown 点，因为 run 可能继续 standby、BTW、follow-up 或 reopen。

真正执行：

```python
await pool_manager.close()
```

的时机：

- operator 删除/归档 run；
- server 正常 shutdown；
- TUI 真实 swarm 退出；
- 明确的 run dispose；
- reopen barrier 接管旧 runtime 前。

`close()`：

- 先停止新 acquire；
- 尝试停止所有 Worker/process group；
- revoke Pool RCP link/token 与 worker tokens；
- 删除所有已证明归属的 Pool 容器；
- 一个 Pool cleanup 失败不能阻止其他 Pool cleanup；
- 返回逐 Pool `PoolCloseReport`；
- 任一残留使总结果非 clean，并保留可供下一次 barrier 使用的 private state；
- 幂等，多次调用不能误删其他 run。

---

## 17. 可观测性与数据安全

### 17.1 不新增 graph runtime 事件

M9a 不新增 canonical SharedGraph runtime 事件，不改 EventBus substrate。复用：

- 现有 `WORKER_STATUS.runtime` 扩展 Pool identity；
- 现有 `PROVIDER_ERROR` 表达对操作员有意义的 runtime/provider 错误；
- coordinator-private sidecar/state；
- 只读 runtime pool API。

runtime lifecycle 不进入 Reason prompt、provenance corpus、fact verification 或 solve-rate。

### 17.2 Private state

```text
sessions/<run-id>/.runtime/pools/<pool-id>/state.v1.json
sessions/<run-id>/.runtime/pools/<pool-id>/diagnostics/lifecycle.jsonl
```

state durable 写；diagnostics append-only、单写锁、尾部 partial-line 容错。二者都不挂入 Worker。

### 17.3 RuntimeExecRecord

扩展字段：

```text
run_id
pool_id
pool_instance_id
generation
worker_instance_id
profile_id
runtime_kind
operation_kind
image_id_short
failure_code
sanitized_error
started_at/finished_at
```

禁止：

```text
host absolute path
secret/token
raw account/provider payload
完整 credential identity
challenge prompt/flag/reference
未经净化的 stderr/provider response
```

### 17.4 API

```text
GET /api/runs/<run_id>/runtime-pools
```

返回 snapshot + manager 的只读 allowlist 视图：

```text
pool_id/profile/runtime/state/generation
ready/degraded reason code
active/waiting worker counts
capacity
image short ID
last transition time
probe status（无 prompt/usage secret）
```

API 不启动 Pool、不执行 Probe、不改变状态。run 不存在或无权限时沿用现有 Web auth 语义。

---

## 18. Forward-only 与兼容边界

- snapshot `version=1`；未知新版本必须拒绝，而不是猜测；
- M9a 不提供生产一键退回“单 run 单容器”的开关；
- 旧 `container_dockerexec` 只允许确定性测试或双重显式 unsafe local-dev；
- 任何 production failure 都不得自动 fallback 到 legacy/local；
- 旧 binary 不保证自动拒绝打开 M9a run；运维回滚前必须先用新版本停止并清理所有 Pool；
- 不承诺活动 M9a run 可被旧 binary 无损接管；
- legacy state 迁移只负责安全清理，不把旧单容器冒充新 Pool snapshot；
- 发布文档必须说明 forward-only 操作顺序与回滚前置条件。

---

## 19. 预期代码改动边界

### 19.1 核心 runtime

- `dswarm/solver/container_exec.py`：拆除 run-global 单容器假设，保留兼容 facade；
- `dswarm/solver/control_receiver.py`：RCP v2 pool-instance identity；
- `dswarm/solver/cli_driver.py`：公共 tool-disabled Probe 契约；
- `dswarm/solver/credential_accounts.py`：最小单-binding projection，删除 raw-store fallback；
- `dswarm/swarm/worker_runtime_mixin.py`：统一从 PoolManager acquire lease；
- `dswarm/swarm/swarm.py`：持有 run-scoped manager，删除单值 container 状态；
- 新增 policy/snapshot/pool/probe/cleanup/diagnostics 小模块。

### 19.2 Web/TUI/入口

- `apps/web/run_manager.py` 与 drivers：创建/reopen 注入 frozen RuntimePolicy/PoolManager；
- runtime-pools 只读 endpoint；
- TUI real swarm 生命周期接入 manager close；
- `run.sh`：Docker-first web 与 real TUI；
- `docker-compose.yml`：loopback/auth 默认、tui-control 支持、control-plane/Worker 权限边界；
- `.env.example`/README/README_CN：新增明确配置与 forward-only 说明。

### 19.3 明确不改

- `dswarm/solver/gate.py`；
- anti-laundering；
- SharedGraph canonical schema/append-only 语义；
- M5 usage event schema；
- M7 energy 与 M8 advisor 公式/sidecar；
- first-valid-flag/multi-flag completion 语义。

---

## 20. 实施分期与提交边界

每阶段独立提交，阶段间保持全量绿色。

### M9a-1 — RuntimePolicy、模型与 snapshot

- immutable models/validators；
- canonical PoolKey；
- image ID + UID/GID preflight；
- snapshot durable store；
- local-dev 双重门；
- 默认不改 Worker dispatch。

### M9a-2 — RCP pool-instance identity

- Hello v2；
- receiver maps/API；
- labels/expected identity；
- per-Pool revoke 与并发 link；
- 旧 v1 限定到测试/unsafe local-dev。

### M9a-3 — ContainerRuntimeExecutor、PoolManager 与 Probe

- 单 generation executor；
- Pool lifecycle/single-flight/semaphore；
- tool-disabled Pi Probe；
- M5 Probe accounting；
- recovery/degraded；
- exact cleanup primitives。

### M9a-4 — Swarm 全部真实 spawn 接线

- worker/review/recon/recovery/bootstrap/standby/resolve/BTW；
- frozen snapshot profile selection；
- runtime failover；
- 删除生产单值 container handle 路径；
- provenance/graph prompt 等价回归。

### M9a-5 — Docker-first Web/TUI

- `run.sh web` → compose；
- real TUI control container；
- mock TUI 保持宿主；
- loopback/password；
- Docker unavailable fail closed；
- Python API policy 防绕过。

### M9a-6 — 可观测性、reopen/legacy cleanup 与文档

- private state/diagnostics；
- runtime-pools endpoint；
- run-wide cleanup barrier；
- close/dispose 全路径；
- forward-only 运维文档；
- legacy compatibility 删除或明确封存。

每阶段完成：

```bash
git diff --check
uv run pytest -q
```

Docker 相关阶段另执行：

```bash
bash -n ./run.sh
docker compose config
DSWARM_RUN_DOCKER_TESTS=1 uv run pytest -q tests/integration/test_container_pools.py
```

Windows checkout 不能通过 WSL `/mnt/c` 运行 `init.sh`；该环境使用原生 PowerShell 的 `uv run pytest -q`，与仓库启动保护逻辑一致。

---

## 21. 测试矩阵

默认测试全部使用 Fake Docker/RCP/Gateway/CredentialProjector，不要求 Docker、key、network 或真实费用。Docker integration 使用 fake `pi` 与 fake OpenAI endpoint。

### 21.1 Policy、PoolKey 与 snapshot

1. Docker 是真实 Worker 默认模式；
2. 仅 `--local-dev` 不放行；
3. 仅环境变量不放行；
4. 双重允许才构造 local-dev policy；
5. Python API 无法绕过 policy；
6. pytest ambient 环境不自动放行；
7. `max_pools_per_run` 默认 32；
8. 小于 1 或大于 128 拒绝；
9. Pool 容量缺省继承 run.max_workers；
10. canonical JSON key 顺序不影响 PoolKey；
11. 未知字段拒绝；
12. Python `hash()` 不参与身份；
13. secret bytes/version 不改变 PoolKey；
14. binding/image/network/resource/profile 改变会改变 PoolKey；
15. 相同 PoolKey 在不同 run 不共享容器；
16. image tag 被解析并冻结为 image ID；
17. tag 移动不影响已创建 snapshot；
18. snapshot temp+fsync+replace；
19. snapshot 不含 secret/token/host HOME；
20. snapshot 未知 version 拒绝；
21. pool 数超限在创建阶段拒绝；
22. 全局设置变化不改变已冻结 run。

### 21.2 UID/GID、mount 与 credential

23. image 缺少 `kali` → `worker_identity_mismatch`；
24. 同 run image UID 不一致拒绝；
25. GID 不一致拒绝；
26. chown 使用实际 image 的 snapshot UID/GID；
27. 禁止 chmod 777；
28. Worker mount 无 host HOME/`~/.pi`；
29. Worker mount 无 Docker socket；
30. Worker mount 无 `.runtime`；
31. Worker 不能挂其他 run；
32. Gateway Pool 无真实 provider key；
33. Gateway Worker token 彼此独立；
34. direct projection 只包含目标 binding；
35. projection 失败不读 raw store；
36. binding 删除不换默认账户；
37. secret 轮换只影响新调用；
38. Worker 收尾 revoke/清理 secret；
39. 清理无法确认时 generation 作废；
40. diagnostics 不泄漏 account/key/token/path。

### 21.3 RCP v2

41. 同 run 两个 Pool 可同时连接；
42. 第二个 Pool 不覆盖第一个 link；
43. run_id 不匹配拒绝；
44. pool_id 不匹配拒绝；
45. pool_instance_id 不匹配拒绝；
46. token 不匹配拒绝；
47. protocol v1 在 production 拒绝；
48. 同 instance 重复 link 拒绝且不覆盖；
49. revoke_pool_instance 不影响 sibling Pool；
50. revoke_run 唤醒/失败全部 waiter；
51. generation 重建使用新 UUID/token；
52. stale generation 不能抢占 current link；
53. labels 与 expected identity 一致才 ready；
54. receiver shutdown 不遗留永久等待。

### 21.4 Pool lifecycle、容量与 Probe

55. run 创建不启动长生命周期 Pool；
56. 第一次 acquire lazy 创建；
57. 并发 acquire single-flight；
58. 同 Pool 只有一个容器；
59. Pool 满员等待而非横向扩容；
60. acquire 取消不泄漏 permit；
61. lease release 幂等；
62. manager close 后拒绝 acquire；
63. `ready` 前必须真实 one-turn Probe；
64. HTTP health 成功不能替代 CLI Probe；
65. Probe 使用目标 image/runtime/profile/model/network/binding；
66. Probe 使用独立 HOME/session/workdir/token；
67. Probe 不注入 graph/target/challenge/player files；
68. driver 不能证明 tool-disabled 时不得 ready；
69. Probe 不写 evidence/provenance/dead-end；
70. Probe 不进入 solve-rate/M7/M8；
71. Probe usage 进入 M5 run/profile/account；
72. accounting started 失败时零上游请求；
73. unknown usage 不记作 0；
74. timeout 后清理进程组；
75. 无法确认清理则 generation 作废；
76. infrastructure error 最多自动重建一次；
77. auth/model/config error 不重试付费 Probe；
78. credential version 变化使 Probe cache 失效；
79. image/model/generation 变化使 cache 失效；
80. Probe success 后等待 acquire 一起获得 ready 结果。

### 21.5 Swarm dispatch 与故障隔离

81. bootstrap/ordinary/review/recon/recovery 都走 manager；
82. standby、resolve、reopen 后 dispatch 走 manager；
83. BTW container/deep audit 走同 run manager；
84. 每 Worker HOME/session/workdir/instance/token 独立；
85. 同 Pool Worker 可在容量内并发；
86. Pool semaphore 不绕过 WorkerLaneGate；
87. 正常 container path 不调用宿主 `resolve_engine_bin("pi")`；
88. Docker failure 不启动宿主 Pi；
89. 单 Pool degraded 时其他 Pool 继续；
90. 替代 profile 只来自 frozen snapshot；
91. 同方向 profile failover 不伪造 direction override；
92. 真正方向变化仍走 M4 override；
93. runtime failure 不生成 graph fact/dead-end；
94. 有 active Worker/recovery 时不提前 run-level fail；
95. 全部兼容 Pool 不可用才 `runtime_unavailable`；
96. provenance gate 输入仍是实际 Worker 输出；
97. feature 未启用/无多 Pool 差异时 flag 语义不变。

### 21.6 Reopen、cleanup 与 teardown

98. reopen 在任何 cycle/dispatch 前运行 barrier；
99. labels 精确匹配的新容器可清理；
100. name substring 不足以删除；
101. legacy exact name 但 mount 不匹配时不删除；
102. legacy 证据不足时 reopen fail fast；
103. 任一旧 runtime 无法确认停止时无新 dispatch；
104. barrier revoke 旧 RCP 与 worker token；
105. 一个 Pool cleanup 失败不阻止清理其他 Pool；
106. close 幂等；
107. close 不误删其他 run；
108. `RUN_FINISHED` 本身不强制 teardown；
109. delete/archive/server shutdown/TUI exit 调 close；
110. close 残留写入 private state 供下一次 barrier。

### 21.7 Web/TUI、可观测性与安全回归

111. `run.sh web` 使用 compose control plane；
112. `run.sh tui --swarm` 使用交互式 control container；
113. `run.sh tui` mock 不启动 Docker Worker；
114. control plane 可挂 Docker socket，而 Worker 不可；
115. 默认端口 loopback；
116. 非 loopback 无密码拒绝；
117. runtime-pools GET 无副作用；
118. API 字段为 allowlist 且无 secret/path；
119. `WORKER_STATUS.runtime` 含 pool identity；
120. runtime diagnostic actor/内容不污染 Reason/evidence；
121. `PROVIDER_ERROR` 使用 sanitized error；
122. private state 不挂 Worker；
123. production 不自动 fallback legacy/local；
124. Docker integration fake Pi 可完成两 Pool 并发；
125. fake provider 验证 Probe 在 Worker 前发生；
126. fake provider 验证 Probe 与 Worker usage 分开归属；
127. Docker 集成验证容器 labels/mount/network；
128. Docker 集成验证重建 generation 与 stale link 拒绝；
129. Docker 集成验证 teardown 无残留；
130. 全量 `uv run pytest -q` 绿色且 provenance/append-only/M5 consumer 回归不变。

---

## 22. 完成判据

M9a 只有在以下条件全部满足后才算完成：

- 六阶段实现与独立提交完成；
- 130 条验收语义有确定性测试覆盖；
- 默认全量测试绿色；
- Docker integration 在显式开关下绿色；
- Web 与 real TUI 默认 Docker-first；
- 所有真实 shell 入口均通过 PoolManager；
- 正常容器路径无法触发宿主 Pi；
- RCP 能同时区分同 run 多 Pool、多 generation；
- Probe 是 tool-disabled 的真实 one-turn，并进入 M5 但不进入 solve-rate/evidence；
- reopen barrier 无法证明清理时 fail fast；
- Worker 不获得 host HOME、`~/.pi`、Docker socket、其他 run/Pool credential；
- provenance gate、append-only graph、M5 ledger 与 first-valid-flag 语义无回归；
- 运维文档明确 forward-only 和回滚前必须清理 Pool。

本规范的设计选项均已裁决。后续实施计划只能分解这里已经定稿的契约，不得在编码时重新引入跨 run Pool、同 Pool 多容器、自动 local fallback、agentic Probe 或 raw credential fallback。
