# D-Swarm UI 审计、线框与内核适配改造方案

- **文档状态**：已实施完成（2026-08-07，Phase 0–9 全部落地并通过验证矩阵）
- **审计日期**：2026-08-06
- **覆盖范围**：Web UI、Desktop 壳层、ReasonSwarm 可观测性、Board/pheromone、历史 session 重放、Pi-only 配置、品牌迁移
- **基线约束**：当前工作区未提交的 ReasonSwarm、MemoryBoard、PostgresBoard、projection、runtime、extensions、Pi worker 等改动均为有效基线，后续不得 reset、覆盖或清理

> 本文记录本轮发现的 UI 问题、目标信息架构、线框、内核—UI 事件契约、兼容策略、品牌清理范围、实施阶段、风险和验收标准。本文本身不代表已经修改任何 UI 或内核代码。

## 1. 已确认的产品约束

### 1.1 品牌

- 唯一正式名称为 **D-Swarm**。
- 没有中文名称、中文副标题或英文副标题。
- Logo 使用一个深绿色的字母 **D**。
- 保持现有深色安全指挥台风格，不转为消费级聊天产品风格。
- 中英文界面必须同步、完整维护；品牌名不翻译。
- 除上游归属、AGPL 许可证法律文本和历史鸣谢外，项目中的 `Muteki/muteki/MUTEKI` 名称均需迁移。

### 1.2 内核方向

- 正式方向为 **Pi-only**。
- UI 中 Claude、Codex、Cursor 的所有配置、图标、配色、状态和展示可以彻底删除。
- 正式主路径为新的 **ReasonSwarm**。
- race、旧 coordinator 等旧路径不再作为 UI 功能出现。
- MemoryBoard、PostgresBoard、pheromone 仍标记为实验功能，但 UI 需要直接展示 pheromone 强度。

### 1.3 目标用户与体验优先级

主要用户：

1. CTF 选手；
2. 渗透测试人员。

体验优先级：

1. 观察 swarm 决策；
2. 随时人工干预；
3. 管理大量并发 run；
4. 快速检查结构化 fact、intent 和 provenance；
5. 在需要时展开 Worker 完整 tool output。

### 1.4 兼容性

- 现有 `sessions/` 历史记录必须能由新版 UI 完整重放。
- 现有 `_worker_config.json`、账号配置和 runtime 配置允许用户在新版 UI 中重新配置，不强求自动迁移。
- 本轮尽量不修改外部事件名，仅清理 UI 和增加 payload 字段。
- API path、SSE event type、数据库 schema 应避免不必要的破坏性变更。
- Web UI 与 Desktop 必须同步完成。

---

## 2. 当前架构审计

### 2.1 Web UI

当前 UI 是三栏 conversation-first 结构：

```text
ThreadRail（Run 列表）
│
├─ Conversation（对话/协调器主轴）
│
└─ ArtifactPanel（图、黑板、Workers、Timeline、Evidence 等）
```

主要文件：

- `apps/web/ui/app/page.tsx`
- `apps/web/ui/components/Conversation.tsx`
- `apps/web/ui/components/ThreadRail.tsx`
- `apps/web/ui/components/ArtifactPanel.tsx`
- `apps/web/ui/components/RunInspector.tsx`
- `apps/web/ui/components/WorkerLanes.tsx`
- `apps/web/ui/components/Blackboard.tsx`
- `apps/web/ui/components/EvidenceChain.tsx`
- `apps/web/ui/components/ActivityStream.tsx`
- `apps/web/ui/components/WorkerSettings.tsx`
- `apps/web/ui/lib/events.ts`
- `apps/web/ui/lib/useRun.ts`
- `apps/web/ui/lib/i18n.tsx`

### 2.2 Desktop

Desktop 是 Wails 包装器，主要加载同一套 Next.js UI：

- `desktop/main.go`
- `desktop/svc.go`
- `desktop/frontend/index.html`
- `desktop/wails.json`

因此大部分 UI 改造可直接由 Web/Desktop 共享；Desktop 仍需单独处理窗口标题、启动页、图标、应用名、输出二进制、环境变量、打包信息和文档。

### 2.3 当前 UI 的可复用优点

- SSE + Last-Event-ID 已为实时消费和历史重放提供基础。
- Run rail 已有搜索、文件夹和归档能力。
- Artifact panel 已包含 graph、blackboard、workers、timeline、evidence、findings、credentials、PoCs、routes、directives 等视图。
- Worker 展开详情已支持 reasoning、tool lines、runtime、token、kill。
- EvidenceChain 已区分 verified、candidate、dead-end。
- HITL、spawn、kill、pause、resolve 等入口已存在。
- 中英文使用同一套 flat dictionary，一个 key 同时维护 `zh/en`。
- TypeScript reducer 已能读取大量旧 coordinator/blackboard 事件。

结论：不需要推翻 SSE 主干、重写所有面板或复制两套 Web/Desktop UI；重点是补齐 ReasonSwarm 事件、重排信息优先级并增加兼容标准化层。

---

## 3. 核心问题与适配缺口

### P0-1：ReasonSwarm 几乎没有完整的 UI 可观测事件

`muteki/swarm/reason_scheduler.py` 接收了 bus，但当前没有完整使用 `bus.emit` 表达调度过程。UI 无法稳定获得：

- Recon 开始、结束和结果摘要；
- Reason cycle 开始、结束、代次和耗时；
- 触发 Reason 的原因；
- audit 结果；
- Intent 的 priority、mode、profile、surface target、task kind、host scan；
- `from_facts`；
- dedupe key 和跳过原因；
- DispatchDecision；
- fallback bootstrap 和原因；
- budget、stop reason、等待原因；
- Intent claim、执行、完成状态。

生产集成主要位于 `muteki/swarm/swarm.py::_run_reason_scheduler`，目前最终主要补充 `RUN_FINISHED`；Worker 自身仍发出部分旧事件，但不足以重建 ReasonSwarm 的完整决策链。

**影响**：UI 无法回答“为什么这样做”“为什么没派 Worker”“哪个事实导致了调度”“Reason 是否卡住”。

**方案**：保留现有外部 SSE event type，通过现有事件 payload 增加结构化 `delta_type` 和附加字段，前端建立 Reason cycle/Intent/Dispatch view model。

### P0-2：当前 `ReasonView` 太薄

当前模型主要只有：

```ts
interface ReasonIntent {
  id: string;
  goal: string;
  workerClass: string;
}

interface ReasonView {
  goalMet: boolean;
  intents: ReasonIntent[];
  audit: string[];
}
```

缺失运行状态、generation、触发原因、planner、pinned facts、`from_facts`、priority、dedupe、profile、surface target、task kind、host scan、dispatch/claim/completion、fallback/skip 原因等字段。

`ReasonPanel.tsx` 只展示 intent goal 和 audit 文本，不能承担正式主路径的决策观察。

### P0-3：阶段模型仍属于旧路径

现有 `SwarmDigest.phase` 包含：

```text
draft / racing / running / collecting / paused /
solved / goal_met / finished
```

问题：

- `racing` 是旧路径痕迹；
- 流程阶段和运行状态混在同一枚举；
- 大部分时间只能显示模糊的 `running`；
- 无法区分 recon、reason、dispatch、execute、review；
- 无法区分正常等待、降级等待和卡住。

**方案**：拆分为两维状态：

```text
stage:
queued → prepare → recon → reason → dispatch → execute → review → finalize

status:
active / waiting / paused / degraded / failed / solved / completed
```

例如：

```text
stage=reason,   status=active
stage=dispatch, status=waiting
stage=execute,  status=degraded
stage=review,   status=paused
```

### P0-4：pheromone 没有 UI/API/event 通道

实验 Board 定义在：

- `muteki/swarm/board.py`
- `muteki/swarm/postgres_board.py`
- `muteki/swarm/projection.py`

Finding 已有：

```text
pheromone_base
half_life_sec
created_at
pheromone(now)
kind
target
payload
source_seq
```

但 Web 没有查询实验 Board 的正式 UI API；当前 `/api/blackboard/{run_id}` 是 Worker 命令代理，访问 SharedGraph skill，不是 MemoryBoard/PostgresBoard 查询接口。BoardProjector 只把 SharedGraph fact 投影到 Board，UI event payload 没有 pheromone 参数。

**方案**：不要让 UI 直接依赖 MemoryBoard/PostgresBoard；在现有 fact/`blackboard.delta` 中投影不可变参数：

```text
finding_kind
pheromone_base
pheromone_half_life_sec
pheromone_created_at
source_seq
experimental=true
```

UI 使用与内核相同的半衰期公式计算当前强度：

```text
strength = base × 2 ^ (-age / half_life)
```

Live 使用当前时间；Replay 使用 replay cursor 的虚拟时间。旧 session 缺字段时显示 `N/A`。pheromone 只表示当前活跃程度/调度影响，不能和 verified/confidence 合并。

### P0-5：现有 session 需要兼容标准化层

新版不能要求修改原始 `sessions/`。建议：

```text
Raw SSE / Session Event
          │
          ▼
Legacy + New Event Normalizer
          │
          ▼
DSwarmEvent / DSwarmViewModel
          │
          ▼
UI Components
```

旧 race/coordinator 事件继续被 reducer 读取，但映射为通用 `legacy execution activity`，不重新暴露旧运行模式。原始 payload 可在 Raw Event 展开区查看。

### P1-1：主界面过于 conversation-first

当前注释和结构明确把 ChatGPT/Claude 风格 conversation 当作中心。这与新的优先级不符。普通 prompt/composer 应保留，但中心区域应变为 **Decision Timeline**，聊天消息只是 Timeline 中的一类事件。

### P1-2：大量并发 Run 的管理密度不足

当前 ThreadRail 缺少：

- 当前 stage/status；
- queue position；
- 活跃 Worker 数；
- 等待人工处理 badge；
- flag 进度；
- cost/budget；
- elapsed time；
- degraded/runtime failure；
- attention filter。

建议增加 All、Active、Needs Attention、Queued、Paused、Solved、Failed、Archived 筛选，以及高密度 compact row。批量 pause/resume/stop 可先由 UI fan-out 现有单 Run API，避免修改 API；批量 stop 必须二次确认。

### P1-3：Worker 原始输出的视觉优先级过高

折叠态应只突出：

- worker/profile/mode；
- 当前 intent；
- structured findings；
- provenance result；
- latest activity；
- token/cost/runtime；
- health/timeout。

展开后再显示 reasoning、tool command、tool result、raw terminal output 和 artifact links。

### P1-4：HITL 指令缺少消费反馈

UI 需要显示：

```text
Queued → Consumed by Reason cycle → Applied to Intent/Dispatch → Completed
```

并明确：普通 hint/redirect 不打断当前 single-shot Worker；redirect 默认作用于下一次 dispatch；pause/kill 属于即时控制；未消费指令持续显示 pending。

### P1-5：Pi-only 方向与旧 provider UI 冲突

UI、注释、示例、设置、状态配色中仍存在 Claude、Codex、Cursor、engine race、degradation 等旧语义，需要从正式 UI 完全删除。只保留 Pi profile/provider/model/runtime/account/credential。

### P1-6：中英文机制可用，但需要防漂移

新增所有文案必须同时维护 `zh/en`；增加 key parity 测试；禁止直接向 UI 暴露未经翻译的内核枚举；品牌名始终为 `D-Swarm`。

---

## 4. 内核—UI 能力矩阵

| 内核能力 | 当前 UI | 缺口 | 方案 |
|---|---|---|---|
| ReasonSwarm | 几乎没有完整决策视图 | 无 cycle、trigger、decision | 增加结构化 delta，建立 Reason Timeline |
| Recon | 从零散活动推断 | 无开始/完成/摘要 | 增加 recon started/completed payload |
| Intent | 仅 id/goal/workerClass | 缺 priority、来源事实、状态 | 扩展 ReasonIntentView |
| Dispatch | 无结构化决策 | 不知道为何选择 Worker | 增加 dispatch decision payload |
| Fallback | 基本不可见 | 易被误判为卡住 | 显示 fallback 和原因 |
| Dedupe/skip | 不可见 | 无法解释未执行 Intent | 展示 dedupe key、skip reason |
| SharedGraph | 已有 Blackboard/Evidence | 与 Board 关联不足 | 用 fact ID/source sequence 统一关联 |
| Memory/PostgresBoard | 无 UI 契约 | 直接查询会耦合存储 | 通过事件投影 |
| pheromone | 不可见 | 无 strength/base/half-life | 发不可变参数，UI 计算 |
| Worker lifecycle | 基础较好 | 与 Intent/cycle 关联不足 | 增加 cycleId、intentId、dispatch reason |
| Tool output | 可展开 | 主视图信息噪声高 | 默认折叠，结构化摘要优先 |
| Provenance | 已有 Evidence | 跨视图跳转不足 | Fact/Worker/Intent/source event 互链 |
| HITL | 有入口 | 消费状态不明确 | 增加指令生命周期 |
| 多 Run | 有 rail | attention 和密度不足 | Fleet 视图、筛选、批量控制 |
| History | 有 replay 基础 | 混有旧语义 | Legacy normalizer |
| Pi-only | 仍有旧 provider | 产品方向不一致 | 删除旧 provider UI |
| Desktop | 共用 Web | 壳层品牌仍需单改 | 同步标题、图标、binary、env |

---

## 5. 目标信息架构

新版保持三栏深色安全指挥台，但从 Conversation-first 调整为 Command-center：

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ [深绿 D] D-Swarm | Run/Target | Stage Rail | Budget | Connection | Controls │
├───────────────┬──────────────────────────────────┬──────────────────────────┤
│ RUN FLEET     │ DECISION TIMELINE                │ LIVE SWARM               │
│               │                                  │                          │
│ Active        │ Recon started                    │ [Workers] [Intents]      │
│ Attention     │ Surface discovered               │ [Evidence] [Runtime]     │
│ Queued        │ Reason cycle #3                  │                          │
│ Paused        │ ├ Audit                          │ Worker cards             │
│ Solved        │ ├ Intent I-12                    │ Intent queue             │
│               │ └ Dispatch → pi-worker-2          │ Pheromone evidence       │
│ Run rows:     │                                  │ HITL requests            │
│ stage         │ Fact verified                    │                          │
│ workers       │ Provenance accepted              │                          │
│ flags         │                                  │                          │
│ cost          │ [collapsed worker tool output]   │                          │
│ HITL badge    │                                  │                          │
├───────────────┴──────────────────────────────────┴──────────────────────────┤
│ Operator Command: Hint / Redirect / Focus / Pause / Resume / Spawn / Stop  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 顶部状态栏

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ [ D ] D-Swarm                                                              │
│ Run: web-042  Target: 10.0.2.15  REASON ▸ DISPATCH  Flags 1/3  $1.42  LIVE │
│ [Pause] [Redirect] [Spawn] [Stop] [⋯]                                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

只显示 D-Swarm、当前 Run/Target、Stage、flag、budget/cost、连接/重放状态和高频控制，不出现 Claude、Codex、Cursor、race、coordinator、多引擎竞争状态。

### 5.2 Run Fleet

```text
┌─ RUN FLEET ────────────────────────────┐
│ [Search runs...]                       │
│ [All] [Active] [Attention] [Queued]    │
│                                       │
│ ● web-042                    ATTENTION │
│   REASON · 4 workers · Flags 1/3       │
│   $1.42 · 18m · HITL pending           │
│                                       │
│ ● pwn-017                         LIVE │
│   EXECUTE · 8 workers · Flags 0/1      │
│   $3.21 · 42m                          │
│                                       │
│ ○ recon-104                     QUEUED │
│   Queue #3 · 0 workers                 │
│                                       │
│ ✓ crypto-008                   SOLVED  │
│   Flags 1/1 · $0.81 · 09m              │
└────────────────────────────────────────┘
```

Compact 模式：

```text
web-042  REASON   W4  F1/3  $1.42  !HITL
pwn-017  EXECUTE  W8  F0/1  $3.21
recon104 QUEUED   W0  F0/1  #3
```

筛选：All、Active、Needs Attention、Queued、Paused、Solved、Failed、Archived。

排序：Attention first、Newest、Oldest、Cost、Elapsed、Pheromone activity、Worker count。

### 5.3 Stage Rail

```text
QUEUE ─ PREPARE ─ RECON ─ REASON ─ DISPATCH ─ EXECUTE ─ REVIEW ─ FINALIZE
  ✓        ✓         ✓       ●          ○          ○         ○          ○
```

视觉约定：

- 深绿色实心：当前 active；
- 绿色勾选：completed；
- 灰色圆点：pending；
- 黄色：waiting/degraded；
- 红色：failed；
- 蓝灰色：skipped；
- 特殊边框：operator intervention。

点击阶段后，Decision Timeline 滚动到对应阶段。

### 5.4 Decision Timeline

```text
┌─ DECISION TIMELINE ────────────────────────────────────────────────────────┐
│ 14:32:08  RECON COMPLETED                                                  │
│            7 surfaces · 3 credentials · 2 candidate endpoints             │
│                                                                           │
│ 14:32:11  REASON CYCLE #4                                      1.8s       │
│            Trigger: 3 new verified findings                               │
│                                                                           │
│            Audit                                                          │
│            ✓ /admin endpoint confirmed                                    │
│            ✓ JWT observed in worker output                                │
│            ? JWT algorithm remains candidate                              │
│                                                                           │
│            Decisions                                                      │
│            I-14  EXPLORE /api/admin                            P0.91       │
│                  Based on facts #41, #44                                  │
│                  Surface: HTTP · Task: auth-boundary                       │
│                                                                           │
│ 14:32:13  DISPATCH                                                         │
│            I-14 → pi-worker-2                                              │
│            Profile: web-explore · Runtime: container                       │
│                                                                           │
│ 14:34:01  FACT VERIFIED                                                    │
│            /admin accepts unsigned token                                  │
│            Provenance: pi-worker-2 output #128                             │
│            Pheromone ███████░░░ 0.74  EXPERIMENTAL                        │
│                                                                           │
│ 14:34:03  WORKER OUTPUT                                      [Expand]     │
│            18 tool calls · 2 findings · 1 artifact                         │
└───────────────────────────────────────────────────────────────────────────┘
```

Timeline 事件优先级：Stage、Recon、Reason、Intent、Dispatch、Fact、Provenance、HITL、Goal/flag、异常；普通聊天文本和完整 tool output 降为次级信息。

### 5.5 Live Swarm Inspector

```text
┌─ LIVE SWARM ────────────────────────────┐
│ [Workers] [Intents] [Evidence] [Runtime]│
│                                        │
│ Workers  4 active / 2 idle             │
│                                        │
│ pi-worker-2                    RUNNING  │
│ EXPLORE · I-14                         │
│ /api/admin auth boundary               │
│ 02:31 · 8.4k tokens                    │
│ [Inspect] [Redirect] [Kill]            │
│                                        │
│ ── INTENT QUEUE ─────────────────────  │
│ I-17  Analyze JWT claims        0.82    │
│ I-19  Enumerate admin routes    0.73    │
│                                        │
│ ── ATTENTION ────────────────────────  │
│ HITL request waiting                   │
│ Worker timeout degraded                │
└────────────────────────────────────────┘
```

### 5.6 Operator Command Bar

```text
┌─ OPERATOR COMMAND ─────────────────────────────────────────────────────────┐
│ [Hint ▾] Tell the swarm to prioritize the JWT auth boundary...            │
│ Applies to: Current run · Next Reason cycle                    [Send]      │
└────────────────────────────────────────────────────────────────────────────┘
```

命令类型：Hint、Redirect、Focus、Spawn、Pause、Resume、Stop、Attach artifact、Operator note。

发送后进入 Timeline：

```text
OPERATOR DIRECTIVE
Queued → Consumed by Reason cycle #5 → Applied to Intent I-21
```

---

## 6. 关键组件详细方案

### 6.1 Reason cycle 模型

建议前端标准化模型：

```ts
interface ReasonCycleView {
  id: string;
  generation: number;
  status: "running" | "completed" | "skipped" | "failed";
  trigger: ReasonTrigger;
  startedAt: string;
  completedAt?: string;
  planner?: string;
  audits: ReasonAuditItem[];
  intents: ReasonIntentView[];
  pinnedFactIds: string[];
  fallbackReason?: string;
  stopReason?: string;
}
```

```ts
interface ReasonIntentView {
  id: string;
  cycleId: string;
  goal: string;
  mode: string;
  priority?: number;
  status:
    | "proposed"
    | "queued"
    | "claimed"
    | "running"
    | "completed"
    | "skipped"
    | "failed";
  fromFactIds: string[];
  dedupeKey?: string;
  profile?: string;
  surfaceTarget?: string;
  taskKind?: string;
  hostScan?: boolean;
  workerId?: string;
  dispatchReason?: string;
  skipReason?: string;
}
```

这是 UI 标准化模型，不要求修改现有数据库表。

### 6.2 Worker 卡片

折叠态：

```text
pi-worker-2                       RUNNING
EXPLORE · Intent I-14
Probe /api/admin authorization boundary

Latest fact: /admin responds without JWT
Provenance: execution output attached
Runtime: container · 02:31 · 8.4k tokens
```

展开态按以下层级：

1. Structured findings；
2. Intent 与 dispatch reason；
3. Provenance；
4. Reasoning；
5. Tool calls；
6. Tool results；
7. Raw terminal output；
8. Artifacts；
9. Runtime diagnostics；
10. Kill/redirect controls。

### 6.3 Evidence、Provenance 和 Pheromone

卡片示例：

```text
HTTP_ENDPOINT  /admin                           VERIFIED
Worker pi-2 · Source event #128 · 2m ago

Pheromone  ███████░░░  0.74
Base 0.80 · Half-life 6h · Experimental
```

三个维度必须独立：

```text
Truth status: verified / candidate / dead-end
Pheromone: 当前活跃程度/调度影响
Provenance: 来源是否可追溯
```

视觉建议：

- `0.75–1.00`：深绿色；
- `0.40–0.74`：灰绿色；
- `0.15–0.39`：低饱和绿色；
- `<0.15`：灰色，但不自动隐藏。

支持按 strength 排序、按 kind 筛选、Experimental 筛选、查看 base/half-life/age、跳转 source event/Worker/Intent。Replay 时按 cursor 时间计算；暂停 replay 后强度不继续随现实时间变化。

### 6.4 Pi-only 设置中心

```text
Runtime
├─ Execution mode
├─ Container/local runtime
├─ Concurrency
├─ Worker timeout
├─ Run budget
└─ Artifact/session paths

Pi
├─ Profile
├─ Provider
├─ Model
├─ Account/credential
├─ Context limits
└─ Test connection

ReasonSwarm
├─ Max active intents
├─ Reason cycle policy
├─ Recon policy
├─ Dispatch policy
└─ Experimental features

Board — Experimental
├─ MemoryBoard/PostgresBoard
├─ Connection status
├─ Pheromone defaults
└─ Projection diagnostics
```

现有 `_worker_config.json`、账号配置和 runtime 配置不强制自动迁移，但 UI 应提供当前生效值、来源、保存位置、连接验证、错误诊断、重置默认值和敏感字段遮罩。

---

## 7. 建议的兼容事件契约

### 7.1 保持外部 event type，扩展 payload

Reason cycle：

```json
{
  "delta_type": "reason_cycle_started",
  "reason_cycle_id": "reason-4",
  "generation": 4,
  "stage": "reason",
  "trigger": {
    "kind": "new_verified_facts",
    "fact_ids": ["41", "44", "45"]
  }
}
```

Dispatch：

```json
{
  "delta_type": "dispatch_decision",
  "reason_cycle_id": "reason-4",
  "intent_id": "I-14",
  "worker_id": "pi-worker-2",
  "priority": 0.91,
  "from_facts": ["41", "44"],
  "profile": "web-explore",
  "surface_target": "/api/admin",
  "task_kind": "auth-boundary",
  "dispatch_reason": "highest-priority unclaimed intent"
}
```

Pheromone：

```json
{
  "delta_type": "finding_upserted",
  "finding_id": "finding-82",
  "finding_kind": "http_endpoint",
  "target": "/admin",
  "source_seq": 128,
  "pheromone_base": 0.8,
  "pheromone_half_life_sec": 21600,
  "pheromone_created_at": "2026-08-06T14:34:01Z",
  "experimental": true
}
```

### 7.2 兼容原则

- 不修改既有 API path，除非后续发现无法扩展；
- 不重命名既有 SSE event type；
- 不要求旧客户端识别新增字段；
- 不让 UI 直接依赖 MemoryBoard/PostgresBoard；
- 不为展示方便修改 provenance gate；
- 不修改原始 session；
- 缺字段时通过 normalizer 提供兼容默认值；
- 新字段尽量使用不可变参数，保证 replay 可重建；
- UI 派生状态不反写 event log。

### 7.3 旧事件映射

```text
race_started       → legacy execution activity / started
race_concluded     → legacy execution activity / completed
旧 coordinator plan → legacy planning activity
旧 worker engine    → generic worker identity
缺少 stage          → 根据事件顺序推导 legacy stage
缺少 pheromone      → N/A
```

旧路径不重新出现在导航、设置和正式状态枚举中。

---

## 8. D-Swarm 视觉和命名规范

### 8.1 名称

唯一面向用户的正式写法：

```text
D-Swarm
```

禁止作为产品名出现：中文名、中文/英文副标题、`D Swarm`、`D-SWARM`、`DSwarm`。`DSwarm` 只用于不允许连字符的技术符号。

建议技术命名：

| 场景 | 建议写法 |
|---|---|
| UI 正式名称 | `D-Swarm` |
| Python package | `dswarm` |
| TypeScript 类型前缀 | `DSwarm` |
| 环境变量 | `DSWARM_*` |
| localStorage | `dswarm.*` |
| Skill | `dswarm-blackboard` |
| Desktop binary | `d-swarm-desktop` |
| package metadata | `d-swarm-*` |

### 8.2 Logo

Logo 使用单一深绿色字母 D，不增加蜂群、盾牌、骷髅、中文字符、副标题或霓虹渐变文字。优先用 SVG/code-native 实现，便于生成 favicon 和 Desktop 图标。

建议颜色：

```text
Brand dark green:    #0D5C45
Brand active green:  #168C67
Highlight green:     #2BBF8A
Background:          #070B0A
Panel:               #0D1311
Border:               #1A2924
Primary text:        #E4ECE8
Secondary text:      #8FA39A
Warning:             #D8A94C
Critical:            #D45D5D
```

---

## 9. 旧名称与旧内核痕迹清理范围

初步扫描（排除 `.git`、`.venv`、`node_modules`、`.next`、`references`、`sessions`）发现旧名称不区分大小写约 1626 处、174 个文件。该数字是审计时的近似基线，实施时应重新生成机器可读清单。不能简单全文替换，因为需要区分法律文本、历史数据、路径、导入、环境变量和兼容契约。

### 9.1 UI/Desktop 明显残留

包括但不限于：

- `Project Muteki — Command Deck`；
- ThreadRail 中的 `無敵 Muteki`；
- package 名 `muteki-command-deck`；
- `MutekiEvent`；
- `muteki.lang`；
- `muteki.threadRail.width`；
- `muteki.activity.compact`；
- `muteki.evidence.newestFirst`；
- `muteki.bb.layout.v3`；
- `muteki_auth_token`；
- `NEXT_PUBLIC_MUTEKI_API`；
- Desktop 标题、启动页、Wails name/output filename；
- Desktop 使用的 `MUTEKI_*` 环境变量；
- SolverRace、race 文案和旧 engine CSS；
- Claude/Codex/Cursor 注释、示例、logo 和设置。

扫描 `cursor` 时要避免误删 CSS 的 `cursor:` 属性，必须语义化处理。

### 9.2 推荐技术迁移

```text
muteki/                  → dswarm/
MUTEKI_*                 → DSWARM_*
muteki-blackboard        → dswarm-blackboard
MUTEKI_BLACKBOARD_DB     → DSWARM_BLACKBOARD_DB
.muteki_board.md         → .dswarm_board.md
MutekiEvent              → DSwarmEvent
muteki.lang               → dswarm.lang
muteki.* storage keys     → dswarm.*
```

同步更新 imports、entrypoints、pyproject/package metadata、scripts、Docker、tests、examples、config templates、skills、session tooling、Desktop service、CI 和文档。

### 9.3 允许保留的范围

仅允许在明确 allowlist 中保留旧名称：

1. 上游归属；
2. AGPL 许可证法律文本；
3. 历史鸣谢。

建议增加自动扫描测试：大小写不敏感搜索旧名称，仅允许命中指定文件和指定段落，其他命中使检查失败。

### 9.4 历史 session 的处理建议

现有 session 属于历史执行证据和 provenance 链，不应批量改写，否则可能破坏原始事件、sequence、hash、命令输出和可重放性。

建议：

- 不修改原始 session 文件；
- UI chrome 和标准视图不显示旧品牌；
- normalizer 转换旧事件语义；
- 原始历史文本只在 Raw Event/Raw Output 展开时可见；
- 缺少新字段不导致 replay 失败。

这一点需要被视为历史数据完整性例外，或由用户最终确认是否允许原始历史 payload 中保留旧文本。

---

## 10. 分阶段实施计划

### Phase 0：基线保护与契约冻结

- 记录 git working tree，不覆盖当前未提交基线；
- 生成品牌残留清单和 allowlist；
- 建立代表性旧 session fixture；
- 冻结现有 API path、SSE event type、数据库 schema 清单；
- 运行 Python、TypeScript 和 Web 当前基线检查。

交付：基线报告、兼容清单、品牌 allowlist、replay fixture 集。

### Phase 1：事件标准化与兼容层

- `MutekiEvent` 迁移为 `DSwarmEvent`；
- raw event 与 UI view model 分离；
- 建立 legacy/new event normalizer；
- 旧 race/coordinator 事件映射为通用 activity；
- 增加新旧 session reducer fixture 测试；
- 暂不改变主布局。

验收：现有 sessions 均能加载；缺字段有默认值；Worker、Evidence、Timeline 可恢复；不出现 reducer 异常。

### Phase 2：ReasonSwarm 可观测性

在现有事件 payload 中增加：

- stage changed；
- recon started/completed；
- reason cycle started/completed；
- audit item；
- intent proposed/claimed/completed/skipped；
- dispatch decision；
- fallback dispatch；
- dedupe/skip reason；
- budget/stop reason。

验收：UI 可重建 Reason cycle；Intent 可追溯事实；Dispatch 可追溯 Worker；不改变 API path 和 SSE event type。

### Phase 3：D-Swarm 品牌壳

- 深绿色 D Logo；
- 页面 title、metadata、Web 品牌；
- Desktop title、启动页和 icon；
- i18n 品牌文案；
- storage key 新命名；
- 清理用户可见旧名称。

验收：Web/Desktop 用户可见区域仅显示 D-Swarm；中英文完整；Logo 小尺寸可识别。

### Phase 4：Command Center 布局

- Run Fleet；
- Stage Rail；
- Decision Timeline；
- Live Swarm Inspector；
- Persistent Operator Command Bar；
- compact density 和响应式布局。

验收：20–100 Run 场景可筛选；Needs Attention 可快速定位；无需展开 raw output 即可理解当前决策；关键控制始终可访问。

### Phase 5：Worker、Evidence、Provenance、Pheromone

- Worker raw output 默认折叠；
- structured fact 优先；
- Intent/Worker/Fact/source event 互链；
- provenance chain；
- pheromone strength、base、half-life、age；
- replay cursor 时间计算；
- Experimental 标识。

验收：verified 与 pheromone 独立；旧 session 显示 `N/A`；raw output 可完整展开；Fact 可追溯真实 execution output。

### Phase 6：Pi-only 设置中心

- 删除 Claude/Codex/Cursor 配置和展示；
- 删除 race/coordinator UI；
- 提供 Pi profile/provider/model/runtime/account/credential；
- 支持 `_worker_config.json`、账号和 runtime 重新配置；
- connection test、secret masking、诊断。

验收：UI 中无旧 provider；用户可重新建立完整 Pi Worker 配置；配置错误有可操作的诊断。

### Phase 7：Desktop 同步

- Wails name、窗口 title、启动页、binary、icon、env、packaging；
- 验证 Desktop/Web 使用同一事件模型和功能行为。

验收：Desktop 无旧品牌；replay、HITL、Pi 设置与 Web 一致。

### Phase 8：全项目技术名称迁移

- Python package 和 imports；
- env、skill、scripts、Docker、config、tests、docs、metadata；
- Desktop service 和 examples；
- 按 allowlist 执行残留扫描。

这是风险最高阶段，应在 UI 和兼容层稳定后实施。若严格执行零旧名称，则不保留旧 Python import 和旧环境变量 alias，用户按已确认策略重新配置。

### Phase 9：最终验证

- Python 全量测试；
- TypeScript typecheck；
- Next production build；
- Desktop build；
- i18n key parity；
- 旧/新 session replay；
- 多 Run、HITL、kill/redirect；
- provenance、pheromone；
- Pi local/container runtime；
- 品牌残留扫描；
- `git diff --check`。

---

## 11. 风险与控制措施

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 大范围 package/env 重命名破坏启动路径 | 高 | 放到独立阶段；先建立测试和契约清单 |
| 修改历史 session 破坏 provenance/replay | 高 | 原始 session 只读，使用 normalizer |
| UI 直接查询实验 Board 导致 replay 不一致 | 高 | 只消费事件投影的不可变参数 |
| pheromone 被误读为置信度 | 高 | 与 truth/provenance 分栏和独立 badge |
| 旧 race 事件无法重放 | 高 | 保留 reducer 兼容读取，不保留旧正式 UI |
| 新 Reason 事件数量过多 | 中 | 周期级摘要 + 可展开详情；避免每次 decay 发事件 |
| 大量 Run 导致 UI 卡顿 | 中 | 虚拟列表、按需渲染、折叠 raw output |
| Web/Desktop 行为漂移 | 中 | 共用组件和 reducer；Desktop 只维护壳层差异 |
| 中英文新增 key 漏翻译 | 中 | 自动 key parity 和渲染测试 |
| 品牌全文替换误伤 CSS `cursor:` 等 | 中 | 语义化迁移和 allowlist 扫描 |
| 技术 alias 与“零旧名称”冲突 | 中 | 由用户确认；默认不保留旧 alias |
| 未提交有效基线被覆盖 | 高 | 每阶段先检查 status；只编辑计划内文件；禁止 reset/clean |

---

## 12. 验收清单

### 12.1 品牌

- [ ] 正式名称全部为 `D-Swarm`；
- [ ] 无中文名称和副标题；
- [ ] Logo 为深绿色 D；
- [ ] Web/Desktop 品牌一致；
- [ ] 除 allowlist/历史数据政策外无旧名称残留；
- [ ] 技术标识遵循统一规则。

### 12.2 ReasonSwarm

- [ ] ReasonSwarm 是 UI 唯一正式主路径；
- [ ] Recon/Reason/Dispatch/Execute/Review 可观察；
- [ ] Intent 显示来源事实和优先级；
- [ ] Dispatch 显示 Worker 和选择原因；
- [ ] fallback/dedupe/skip 可解释；
- [ ] race/coordinator 不出现在正式 UI。

### 12.3 Worker 与证据

- [ ] 完整 tool output 默认折叠；
- [ ] fact/intent/provenance 优先；
- [ ] 可展开完整输出；
- [ ] Worker 可 kill/redirect；
- [ ] Worker、Intent、Fact、source event 可互相跳转；
- [ ] provenance gate 不被弱化或绕过。

### 12.4 Pheromone

- [ ] UI 直接展示 strength；
- [ ] 显示 base、half-life、age；
- [ ] 标注 Experimental；
- [ ] 不与 verified/confidence 混淆；
- [ ] Replay 使用虚拟时间；
- [ ] 旧 session 缺字段时正常降级。

### 12.5 多 Run 与 HITL

- [ ] 支持高密度 Fleet；
- [ ] 支持 Needs Attention 筛选；
- [ ] 显示 stage、worker、flag、cost、elapsed；
- [ ] 支持批量 pause/resume/stop；
- [ ] 批量 stop 二次确认且不删除历史；
- [ ] HITL 显示 queued/consumed/applied/completed。

### 12.6 兼容性

- [ ] 现有 `sessions/` 可完整重放；
- [ ] API path 无不必要破坏；
- [ ] SSE event type 无不必要破坏；
- [ ] 数据库 schema 无不必要破坏；
- [ ] 当前未提交有效基线全部保留；
- [ ] 配置可由用户重新建立。

### 12.7 中英文

- [ ] 所有新文案同时提供中文和英文；
- [ ] 品牌名不翻译；
- [ ] 内核枚举不直接裸露；
- [ ] i18n key parity 测试通过；
- [ ] 中英文布局无明显溢出。

---

## 13. 实施前需要确认的决策

1. **技术命名规则**：是否采用 UI=`D-Swarm`、Python=`dswarm`、TS 类型=`DSwarm*`、环境变量=`DSWARM_*`、Skill=`dswarm-blackboard`、Desktop binary=`d-swarm-desktop`。
2. **历史 session 例外**：是否确认原始 `sessions/` 不做内容改写，旧文本只可能出现在 Raw Event/Raw Output 展开视图。
3. **完整重放语义**：建议定义为恢复 Timeline、Worker、Intent、Evidence、HITL、状态和原始输出，不代表重新执行 Worker。
4. **兼容 alias**：若要求仓库除法律/鸣谢/历史数据外零旧名称，则不保留旧 Python import 和旧环境变量 alias；用户重新配置。
5. **批量 Stop**：建议需要二次确认，停止运行但不删除 session 历史。
6. **Pheromone replay**：建议默认显示 replay cursor 时间点强度，而不是现实时间强度。

---

## 14. 推荐结论

建议批准以下总体方向：

1. 不推翻现有 SSE 和 Web/Desktop 共享架构；
2. 先建立事件 normalizer 和旧 session replay 保障；
3. 给 ReasonSwarm 补齐结构化可观测事件；
4. 将 Conversation 主轴改为 Decision Timeline；
5. 将左侧升级为高密度 Run Fleet；
6. Worker raw output 默认折叠，fact/intent/provenance 优先；
7. 通过事件投影展示 pheromone，不让 UI 直连实验 Board；
8. UI 正式路径只保留 ReasonSwarm 和 Pi；
9. Web/Desktop 同步改造；
10. 最后执行全项目技术名称迁移和严格残留扫描。

该顺序能最大限度保护现有未提交基线、历史 session、外部事件契约和 provenance 正确性，同时把 D-Swarm UI 转向真正适合 CTF、渗透测试和大规模并发 Run 管理的安全指挥台。
