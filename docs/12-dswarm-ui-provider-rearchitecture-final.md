# D-Swarm UI 与 Provider / Model 重构正式方案

> 状态：Provider/UI 主体与 Target/Run/Observability 第一版已落地；fallback、能力目录与 Pentest 专属 UI 仍为后续 RFC  
> 日期：2026-09-03  
> 范围：CTF Mode 优先；Pentest 暂复用 CTF UI  
> 参考：OpenCode 的 provider/model registry、上下文分层、terminal/tmux 工作流

## 1. 产品目标

D-Swarm 的核心对象从“一个 Run”升级为“一个 Target 下的多次独立 Run”。

```text
Target
├── 题目信息 / 目标地址 / 附件 / hint
├── Primary Direction
├── Verified Facts（跨 Run 沉淀）
└── Runs
    ├── 独立的 Reason / Worker / Review / Titler binding
    ├── 独立事件流、证据链、Intent、Blackboard、Graph
    ├── 独立成本、token、耗时与运行状态
    └── 独立 replay / raw output / detail views
```

用户在主界面首先理解 Target 与 Run 的从属关系；需要深入调查时，再进入 Run Detail 查看 Worker、Intent、证据链、路线分支、时间线、知识黑板、Graph 和 Ledger。

## 2. 已确认的产品决策

- 默认深色安全指挥台，支持浅色主题切换；不使用网格背景。
- 单用户工作台，通常同时运行不超过 5 个 Run。
- 一个 Target 可以启动多个 Run；每个 Run 可选择不同主方向。
- Reason、Worker、Review、Titler 可以绑定不同 Provider / Model。
- Provider 页面保持极简：只编辑 provider、协议、endpoint origin、模型和 credential account，不在主面板展开复杂能力矩阵。
- 当前优先支持 OpenAI-compatible 与 Anthropic 协议；官方 DeepSeek、OpenAI、Anthropic、智谱、Kimi、Grok 以及自定义中转站通过同一 provider registry 接入。
- Run 创建流程：`Create Run → Select Target → Select Worker Profile → Select role bindings → Start`。
- 默认策略是“手动触发 + 缓存 + 明确状态”。Provider fallback 只作为后期扩展，不在当前版本做隐式重试。
- 主 UI 保持简约；复杂信息放入 Inspector 和详情页。

## 3. OpenCode 借鉴与边界

### 借鉴

1. **Provider registry**：provider 是可发现、可配置、可扩展的注册对象；model 是 provider 下的可选资源。
2. **协议与传输分离**：OpenAI-compatible、Anthropic 等 wire API 不应与 UI 页面硬编码绑定。
3. **上下文分层**：全局配置、项目配置、会话配置和临时覆盖应有清楚优先级。
4. **Terminal / tmux 观念**：D-Swarm 继续使用 shelled CLI Worker，让 UI 订阅事件、展示输出和发送指导，而不是重新实现模型 agent loop。
5. **可观察性**：模型、provider、endpoint origin 和运行状态应能追溯，但 secret 永不进入前端。

### 不照搬

- 不把 D-Swarm 改造成 OpenCode 的 agent runtime。
- 不让 UI 直接调用 solver core。
- 不弱化 provenance gate，不把 Blackboard、Reason 推断或用户 hint 当作 accepted flag 来源。
- 不把 shared evidence graph 改为可覆盖的状态表；它必须保持 append-only。

## 4. Provider / Model 数据模型

### Provider

```text
Provider
├── id / label
├── wire_api: openai | anthropic
├── base_url（展示 origin，禁止展示 query/path secret）
├── credential_account（只保存引用）
├── enabled
└── models[]
```

### Model binding

```text
RoleBinding
├── provider_ref
├── model
├── wire_api
├── endpoint origin
├── credential_account
├── effort / timeout 等非 secret 参数
└── source: target preset | worker profile | run override
```

绑定只允许引用 credential account，不携带 API key、token、password、authorization value、private key 或完整 credential header。

## 5. Target / Run 启动语义

Target 可以提供默认 preset，Worker Profile 可以提供能力、镜像和资源配置，Run Wizard 提供本次运行的明确覆盖。启动时解析顺序为：

```text
Run override > Worker Profile / Target preset > system default
```

首次启动时保存一个**非 secret 的 RunBindingSnapshot**。快照冻结后，Run 的历史解释不依赖后续 Provider 设置修改。当前 Worker / Review 通过既有 runtime overlay 接入；这不代表新增了独立的多 agent runtime。

### 5.1 Draft, startup, and restart recovery

A Target-bound draft is an **unstarted Run**, not a live solver session. It may be persisted
in a Run sidecar so a browser refresh or backend restart can restore the operator's work,
but it stores only:

- target_id, direction, challenge/attachment references, and human guidance context;
- non-secret role-binding request values;
- inherited verified-fact references;
- draft lifecycle state.

Provider secrets, complete credential headers, signed URLs, and temporary gateway tokens never
enter the sidecar. In sidecars, `base_url`, `endpoint_url`, and `api_base_url` retain only a
safe routing URL with query, fragment, and userinfo removed.

On backend restart, RunManager restores in this order:

```text
sidecar -> dispatch body -> binding snapshot -> live event/session replay
```

A started Run prefers its frozen non-secret binding snapshot. New worker tokens can only be
minted on the host when the currently configured Provider credential still exists. Therefore:

- later Provider endpoint edits do not change the historical meaning of an already frozen Run;
- deleting a Provider or removing its secret never silently routes an old Run to another Provider;
- when an old Run needs a new worker token and the original Provider is unavailable, it becomes
  `blocked` / `provider_unavailable` with an explicit recovery action;
- a legacy Run without a snapshot is shown as `legacy / not frozen`, without inventing model,
  endpoint, or credential state.

The rule is: configuration interpretation is frozen, but credentials are never copied into
Run files permanently. This keeps historical observability and current fail-closed behavior
compatible.

## 6. Run Observability Projection

新增只读 API：

```http
GET /api/runs/{run_id}/observability
```

Projection 由现有事件日志、UsageLedger 和 binding snapshot 计算，不修改任何 substrate：

```json
{
  "run_id": "...",
  "lifecycle": { "status": "running", "started_at": 0, "finished_at": null, "elapsed_ms": 0 },
  "binding": { "version": 1, "frozen": true, "source": "role_binding_overlay", "roles": [] },
  "usage": { "calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0, "unknown_calls": 0, "estimated_calls": 0 },
  "health": { "status": "healthy", "issues": [], "degraded": [] }
}
```

### 三层绑定语义

| 层 | 含义 | 是否可变 |
|---|---|---|
| configured | 用户在 Target/Profile/Wizard 中请求的配置 | 启动前可变 |
| frozen | 首次启动时保存的非 secret 快照 | 不可变 |
| effective | 当前 runtime overlay 实际解析出的角色绑定 | 只读投影 |

当前实现中 effective 通常等于 frozen 经过 runtime overlay 后的结果，不应被 UI 描述成独立 agent 实例。没有快照的旧 Run 显示 `legacy / not frozen` 或 `unavailable`，不得伪造绑定。

### Usage 与 Health

- usage 按 Reason / Worker / Review / Titler 归类，显示 calls、输入/输出/总 token、USD。
- underlying usage 为 estimated 或 unknown 时必须保留标记，不得制造虚假精度。
- fatal provider/runtime issue 显示 `blocked`；普通错误显示 `degraded`。
- 只向 UI 发送规范化的 category、severity、retryable、safe user message、suggested action 和 provider label。
- 原始 provider exception 不越过 projection 边界。

## 7. UI 分层

### 主 Target Workspace

只回答：这是哪个 Target、是否解决、主方向、Run 数量、哪个 Run 需要注意、当前选中的 Run 是什么。主区使用轻量关系图：

```text
Target → Direction → Run
```

### Run Detail

Workers 详情页顶部显示完整 RunBindingInspector：

- configured / frozen / effective 三层；
- Reason / Worker / Review / Titler provider、model、endpoint origin；
- role status、calls、tokens、cost；
- elapsed、总 token、总 USD；
- provider issue / degraded issue。

Evidence、Intent、Graph、Blackboard、Activity Timeline、Raw Output、Replay、Ledger 继续作为独立详情入口。

## 8. Secret 与安全不变量

以下内容永不进入 DTO、SSE、URL、localStorage、binding snapshot、日志或 UI：

- API key、token、password；
- Authorization 值、private key；
- credential 文件内容；
- 包含 credential 的 endpoint query/path；
- 未规范化的 provider exception。

前端只能展示 provider/model、endpoint origin、credential account label 等非 secret 元数据。用户指导只能成为 HITL context，不能成为 flag 来源。

### 8.1 Gateway and sidecar boundary

Worker containers receive only a revocable gateway task token. The real upstream transport
for that token stays in the host-side gateway memory table. Run snapshots, dispatch sidecars,
SSE, logs, and UI state retain only the non-secret metadata needed for audit: provider/model,
wire protocol, endpoint origin, and credential account. Provider exceptions must be normalized
before crossing the public boundary; request URLs, Authorization values, absolute paths, and
raw SDK exception text must not be emitted as-is.

If the gateway cannot resolve an explicitly bound Provider, it must fail closed. The current
version must not automatically use DeepSeek, `pi-main`, or another default account: that would
make the observable Run configuration disagree with the actual execution.

## 9. 实施状态与路线

### 已完成

- Target Library / Target Card / Target Workspace；
- Target → Direction → Run 层级及多 Run 启动；
- Run Launch Wizard；
- role binding 与非 secret RunBindingSnapshot；
- Target API、Run 绑定、Hints、Directions、Verified Facts、Activity；
- Run Observability Projection 及 `/api/runs/{id}/observability`；
- 主 UI compact inspector 与 Run Detail workers inspector；
- 深色默认、浅色主题、无网格背景；
- 保留现有 CLI Worker、ReasonSwarm、ReviewFlow、shared graph、usage ledger、provenance gate。

### 后续计划

1. Provider fallback (later explicit extension; implicit fallback is forbidden now):
   candidate chains, trigger conditions, max attempts, cost ceilings, audit events,
   and visible status. Before implementation, add an explicit Run policy, user
   authorization, a binding-snapshot event for every switch, usage attribution, and
   a UI `fallback active` state. Fallback cannot ship without these audit fields.
2. Target verified fact 的跨 Run 继承确认和“只读 facts / 新 Run context”选择器。
3. 更完整的 replay 与 raw output 分层，并在 UI 中标识 evidence provenance。
4. Provider capability catalog、模型能力筛选和批量健康检查。
5. Pentest 专属信息架构；在此之前继续复用 CTF UI。

任何新功能都必须有确定性测试，并通过 `uv run pytest -q`、UI typecheck、Vitest 和 production build。


## 10. Worker Gateway Protocol Translation (implemented)

The worker container always receives only a revocable **gateway task token** and
continues to speak OpenAI-compatible `POST /v1/chat/completions` to the local
model gateway.  It never receives the selected provider API key.

At task-token issuance, D-Swarm resolves a private immutable transport binding on
the host:

- provider ID (for internal diagnostics/accounting only);
- base URL, protocol, authentication header/prefix;
- upstream credential, retained only in the gateway in-memory token table.

This means one Target may start concurrent Runs whose Worker/Review clusters use
different providers and protocols. Settings edits after a task begins cannot change
that task's upstream binding. Token revocation removes both the worker claims and
the private transport binding. No key is included in worker environment beyond the
task token, API/SSE DTOs, binding snapshots, event payloads, UI state, or gateway
error responses.

### Current protocol support matrix

| Role | OpenAI-compatible | Native Anthropic Messages |
| --- | --- | --- |
| Reason | supported | supported |
| Worker / Review (Pi container) | supported | supported through gateway translation |
| Titler | supported | supported |

For a native Anthropic Worker/Review binding, the gateway converts the Pi
OpenAI-shaped request into `POST /v1/messages`, applies `x-api-key` and
`anthropic-version`, and translates both non-streaming and SSE responses back into
the OpenAI response/SSE shape expected by Pi. System text, tools, tool calls, tool
results, thinking deltas, text deltas, stop reasons, and token usage are mapped.
Native `tool_use` becomes OpenAI `tool_calls`; Anthropic input/output token counts
feed the existing immutable usage journal and cost projection.

Provider fallback remains a **future explicit feature**. There is no silent protocol
or provider fallback: an unavailable selected provider binding fails visibly rather
than routing a Worker to another provider.
