# Target-centric / Run-centric 架构

> 日期：2026-09-03  
> 目标：让 D-Swarm 从 Run-first 工作台升级为 Target-first、多 Run 的 CTF 指挥台。

## 1. 核心关系

```text
Target
├── metadata / description / attachments / hints
├── solved state
├── primary direction
├── verified facts（跨 Run 只读聚合）
└── Runs[]
    ├── direction
    ├── worker profile
    ├── Reason / Worker / Review / Titler bindings
    ├── immutable binding snapshot
    ├── event stream
    ├── evidence / intents / graph / blackboard
    ├── activity / raw output / replay
    └── usage / cost / elapsed / health
```

Target 是问题的长期记忆；Run 是一次独立的智能体集群尝试。失败 Run 不污染其他 Run 的调度，只有经过 provenance 规则确认的事实才能投影到 Target 的 Verified Facts。

## 2. Target 模型

```json
{
  "id": "target-web-portal",
  "name": "web-portal",
  "category": "web",
  "description": "...",
  "target": "http://...",
  "attachments": [],
  "hints": [],
  "solved": false,
  "primary_direction_id": "direction-auth",
  "directions": [],
  "verified_facts": [],
  "runs": [],
  "activity": []
}
```

Target 卡片展示摘要，不承载完整执行日志。卡片至少展示 solved 状态、主方向、Run 数、最近 Run、verified fact 数量和需用户关注的信号。

## 3. Direction 与 Run

Direction 是探索主方向，例如 Authentication、Source Review、Crypto Analysis。它不是 solver 状态，也不替代事件图。一个 Target 可以有多个 Direction，每个 Direction 下可以有多个 Run。

Run 必须拥有独立 id 和独立事件流。Run 状态来自既有生命周期和事件折叠，不能由 UI 猜测。Run 的 binding snapshot 在首次启动冻结，后续 Provider 页面变化不改变历史解释。

## 4. 主界面

```text
┌─────────────────────────────────────────────────────────────┐
│ D-Swarm  Target Library                         status/cost │
├──────────────┬───────────────────────────────┬─────────────┤
│ Target Rail  │ Target Workspace               │ Inspector   │
│              │ Target header                  │ selected    │
│ target cards │ Target → Direction → Run map   │ Run summary │
│  ├ run-001   │ Run stack                      │ binding     │
│  └ run-002   │ Decision feed                  │ health      │
├──────────────┴───────────────────────────────┴─────────────┤
│ HITL guidance / Reason hint / pause / resume / submit       │
└─────────────────────────────────────────────────────────────┘
```

关系图只显示三层父子关系：Target → Direction → Run。完整 Evidence Graph 仍在 Run Detail → Graph，避免主界面被复杂节点淹没。

## 5. Run Launch Wizard

固定流程：

1. 选择或创建 Target；
2. 选择主方向或新增方向；
3. 选择 Worker Profile；
4. 分别绑定 Reason、Worker、Review、Titler 的 provider/model；
5. 查看非 secret 配置摘要；
6. 启动并冻结 snapshot。

默认使用手动触发。缓存只用于明确可复用的非 secret metadata / provider catalog；不得缓存 credential 内容或将缓存结果伪装为新执行证据。

## 6. Verified Facts

Verified Fact 必须有真实执行来源，至少能回溯到 Run、事件或 artifact。Target 只读聚合事实，不合并不同 Run 的原始 graph，不覆盖 shared graph 事件。

```text
Target fact
├── fact id / text / type
├── source run id
├── source event / artifact reference
├── verified timestamp
└── provenance status
```

新 Run 可以选择继承 Target 的 verified facts 作为上下文；继承是显式、只读、可追踪的，不会把 fact 变成 flag，也不会绕过 gate。

## 7. 观察与详情

主 UI 仅显示摘要；用户点击后进入：

- Workers：Worker lanes、intent、tool progress、raw output；
- Evidence：证据链与 provenance；
- Graph：完整路线分支与关系图；
- Blackboard：知识黑板和事实状态；
- Timeline：活动时间线；
- Findings / Credentials / POCs / Routes / Directives：结构化产物；
- Ledger：按角色和 provider 的 usage/cost；
- Replay：基于事件重放，不改变历史。

Run workers 详情页额外显示 `/api/runs/{run_id}/observability` 的完整 binding 和 health inspector。

## 8. 迁移策略

允许大版本切换，优先清理旧的 Run-first 入口，但保留旧 Run 的 replay 和选择能力。没有 target_id 的旧 Run 归入 Legacy / Unassigned，并允许用户以后归档或迁移到 Target。旧 Run 没有 binding snapshot 时必须显示 legacy，不得推断历史 provider。

## 9. 不做的事情

当前阶段不做：Run 之间自动合并 graph、自动 provider fallback、复杂 Target 对比、Pentest 专属布局、独立于既有 CLI 的 Worker runtime、任何绕过 provenance gate 的“智能判断”。


## Implementation note — creating a Direction during Run launch (2026-09-03)

The Run Launch Wizard now lets the operator create a reusable Direction without leaving the Target workflow. The new Direction is persisted through `POST /api/targets/{target_id}/directions`, immediately selected for the pending Run, and remains subordinate to the current Target. It does not start a Run or change any provider/model binding; those are still frozen only when the Run is launched.

## 10. Target metadata and operator hints (2026-09-03)

Target Details is an operational editing surface rather than a second Run editor. An operator can update the Target name, description, target address, and flag-format hint, or append a reusable operator hint for future Runs. These mutations use the Target API and are reflected on the card after the next projection refresh.

The edit surface never edits a Run event, a shared evidence graph, or a binding snapshot. A hint is Target context only: it may be included in a later launch request, but it is not evidence and cannot satisfy the provenance gate. Metadata remains safe to persist because provider credentials and gateway tokens are excluded from all Target and Run snapshots.

## 11. Direction relationship integrity (2026-09-03)

The Target API treats a Direction as a child resource, not as a free-form label:

- a `primary_direction_id` is accepted only when it belongs to the same Target;
- a `POST /api/targets/{target_id}/runs` request with a foreign or stale
  `direction_id` returns `400` instead of creating an orphaned relationship;
- when a valid `direction_id` is used and the request omits a display label, the
  Run receives the persisted Direction label automatically;
- legacy card files are normalized at read time so an invalid primary direction
  is exposed as `null`, while the original event-sourced Run logs remain untouched.

This keeps the UI relationship map deterministic: every explicit Run branch is
`Target -> Direction -> Run`; only old/unassigned Runs use the visible fallback
branch. The rule is covered by deterministic Target API and store tests.
## 12. Nested Run projection and flag containment (2026-09-04)

The Target API returns a nested `runs` projection so the UI can show the Target -> Direction -> Run hierarchy without making one request per card. That projection follows the same passwordless containment rule as `/api/runs`: when no authenticated operator token is present, accepted flag values are removed while status, counts, timestamps, direction, and relationship metadata remain available.

The redacted shape is explicit rather than ambiguous:

```json
{
  "flag": null,
  "flags": [],
  "flag_count": 1,
  "flags_redacted": true
}
```

This prevents a worker or an unauthenticated local/network caller from bypassing the run rail by reading `GET /api/targets`, `GET /api/targets/{target_id}`, or `GET /api/targets/{target_id}/runs`. The aggregate `accepted_flag_count` remains available for progress UI, but the flag values themselves stay behind the existing authenticated boundary. Deterministic API coverage verifies the nested Target projection and the per-target Run list.


## 13. Run summary as a final secret-free DTO boundary (2026-09-04)

`Run.summary()` is consumed by more than the ordinary Run rail: Target projections,
Run creation responses, and compatibility routes also use it. Therefore the summary
now applies a final public allowlist to `binding_snapshot` before returning it.

The allowlist keeps the operator-visible routing contract (`provider_ref`, provider,
model, protocol, endpoint origin, role and profile metadata) but drops credentials,
headers, tokens, query strings, fragments, userinfo, and unknown nested fields. This
is defense in depth for legacy sessions or imported snapshots that may predate the
secret-free snapshot writer. The persisted private snapshot is not mutated; only the
DTO is projected. Consequently Target cards, `/api/runs`, and nested Target Run
projections cannot become an accidental credential exfiltration path.

## 14. Target hints 注入新 Run（2026-09-04）

Target 卡片上的 hints 是可复用的操作员 guidance，不是 Evidence。Run Launch Wizard 会在 Target 预览区展示当前 hints，并默认启用“Include saved hints in this Run”；操作者可以在启动前关闭它，避免把历史提示带入本次独立尝试。

### 14.1 启动契约

创建 Target-bound Run 时，前端只发送选择状态，不提前拼接 hint 文本：

```json
{
  "include_target_hints": true,
  "target_hint_ids": ["hint-stable-id"]
}
```

这两个字段会持久化到 Run 的非 secret dispatch sidecar，保证 Run 在真正启动前经历后端重启时，操作者的选择仍然有效。语义如下：

- `include_target_hints=false`：完全不注入 Target hints，且该开关优先级最高；
- `include_target_hints=true` 且明确提供 `target_hint_ids`：只注入这些稳定 ID 对应的 hints；空数组表示明确选择零条；
- 两个字段均缺省：兼容旧客户端和直接调用 `/start` 的场景，默认允许注入 Target 当前全部 hints；
- 后端只按 Target card 上的 stable hint ID 解析文本，不信任客户端提供的 hint 正文，也不按数组下标选择。

### 14.2 后端统一注入

`POST /api/runs/{run_id}/start` 是唯一的 hint prompt 拼接点。它依次读取本次 start body、Run 已保存的 dispatch context 和 Target card，最终使用固定边界标记：

```text
[OPERATOR HINTS FROM TARGET CARD]
Treat these as operator guidance only; verify every claim through real execution.
```

前端启动 prompt 只包含 Target description（或 Target 名称兜底），从而避免重复注入，也避免 UI 关闭 hints 后被后端默认逻辑重新展开。

无效、空白或已不存在的 hint 会在注入前被过滤。hint 只影响本次 Run 的启动上下文，不修改 Target Verified Facts、Run binding snapshot、shared evidence graph 或任何 provenance 记录。真实 worker stdout/stderr/artifact 仍是 provenance gate 的唯一 flag 来源，因此 hint 不能直接成为 fact、flag 或 accepted result。

该设计保持“Target 长期记忆、Run 独立尝试”的边界：Target hints 可以被多个 Run 有选择地复用，但每个 Run 必须通过真实执行重新验证线索。后续增加 hint 来源、过期、确认、引用关系或更细粒度的 UI 选择时，仍应保持 guidance 与 evidence 的类型隔离。


## Implementation status (2026-09-05)

Target → Direction → Run、nested projection、relationship validation、Run
summary secret containment、Target hints、restart-persistent launch context、
reverse fact index and source evidence navigation are implemented and covered by
deterministic tests. Remaining P1 work is lifecycle failure/recovery/concurrency
auditing; this does not change the event-sourced Run authority.
