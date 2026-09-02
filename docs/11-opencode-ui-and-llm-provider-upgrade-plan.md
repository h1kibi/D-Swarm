# D-Swarm UI 与自定义 LLM Provider 升级方案

- **文档状态**：提案（本文件只描述方案，不代表已经实施）
- **编制日期**：2026-09-02
- **适用版本**：D-Swarm 当前 `main`，基线提交 `3740470`
- **参考项目**：[anomalyco/opencode](https://github.com/anomalyco/opencode)
- **参考版本**：`8e0f1c253b6b7292b419505af849d06747c0e049`（`dev`，2026-09-01）

> 本方案的目标是吸收 OpenCode 在 **session-first UI、Provider registry、model catalog、能力元数据和动态 adapter** 方面的设计经验，改善 D-Swarm 的操作体验与自定义模型接入能力。D-Swarm 仍保持自己的 ReasonSwarm、共享 append-only evidence graph、事件流订阅模型、成本账本和 provenance gate；本方案不改变这些核心语义。

---

## 1. 目标、边界与设计原则

### 1.1 目标

1. 让操作员可以在一个连续的工作区中完成项目/run 选择、观察决策、查看证据、干预 swarm 和切换模型。
2. 把 Provider、Model、Secret、Worker Profile 和 Reason/Titler 绑定关系表达为稳定的控制面模型，减少当前配置分散在 profile、account、provider 三处造成的认知负担。
3. 支持 OpenAI-compatible、自定义 relay、DeepSeek、Zhipu、Qwen、Ollama 等端点，同时保留 wire API、认证方式、超时和模型验证能力。
4. 为模型发现、健康状态、能力展示和后续 fallback 提供扩展点，但不让模型元数据或 UI 指令绕过真实执行输出的验收门槛。
5. 保持历史 `sessions/` 可重放、旧配置可读取、旧 API/SSE 契约兼容，并能渐进式上线和回滚。

### 1.2 不在本次范围内

- 不复制 OpenCode 的 SolidJS、Effect runtime、目录结构或完整 UI 实现；D-Swarm 当前是 Next.js + React。
- 不重写 Worker executor、`cli_driver.py`、`cli_solver.py`、ReasonSwarm scheduler、事件 spine、cost ledger、shared graph 或 `gate.py`。
- 不把任意 JavaScript/Python 代码作为无权限 Provider 插件直接执行。
- 不把人工提示、BTW、Provider metadata、模型返回的“flag 声明”变成 flag 来源。
- 不把 capability eval 切换为在线搜索；能力评估仍必须使用 offline 模式。
- 不在文档实施阶段修改运行时行为；后续编码应按阶段单独提交、单独测试。

### 1.3 不可违反的原则

| 原则 | 具体要求 |
| --- | --- |
| provenance sacred | flag 只有在真实 worker stdout/stderr/artifact 输出中出现，并通过硬编码 gate 与 anti-laundering 检查，才能接受。 |
| 前端是 dumb subscriber | Web/TUI 订阅和投影事件；HITL 只通过既有控制通道下达，不直接调用 solver core。 |
| 事件 append-only | 新增 UI/Provider 状态必须通过事件或控制面快照表达，不覆盖事实事件；历史事件保持可重放。 |
| secret 与 public projection 分离 | API key 只写不读；公开 Provider/Model 信息不含原始 secret、header token 或可推导凭据。 |
| fail-closed | 配置错误、Provider 不存在、模型不支持、健康探测失败时，默认拒绝派发或明确降级，不静默换用另一个 Provider。 |
| 单一绑定来源 | Worker、Reason、Titler 的有效 Provider 绑定必须经过同一解析器，避免 UI 显示的 Provider 与实际请求上游不一致。 |

---

## 2. OpenCode 学习结论与 D-Swarm 映射

### 2.1 参考源码

本次按固定 commit 阅读以下实现：

| 主题 | OpenCode 源码 | 可借鉴点 |
| --- | --- | --- |
| Provider registry | `packages/opencode/src/provider/provider.ts` | 内置 Provider 动态加载、配置合并、Provider-specific loader、模型发现、公开投影。 |
| Provider 配置契约 | `packages/core/src/v1/config/provider.ts` | Provider/Model 分层；模型能力、成本、上下限、状态、headers/options、variants。 |
| 请求适配 | `packages/core/src/v1/config/provider-options.ts` | 不同 SDK/wire API 的 headers、URL、body 和请求参数 lowerer。 |
| Provider 认证 | `packages/opencode/src/provider/auth.ts` | API key 与 OAuth/auth store 分离；运行时使用凭据，公开信息只保留连接状态。 |
| 模型状态 | `packages/app/src/context/models.tsx` | provider+model 联合 key；recent、favorite、show/hide、variant 的用户状态。 |
| 应用上下文 | `packages/app/src/app.tsx` | command/dialog/layout/settings/models/tabs/session/server sync 分层。 |
| 新布局 | `packages/app/src/pages/layout-new.tsx` | 顶层 shell、toast、debug、标题栏、响应式容器和页面内容隔离。 |
| Session composer | `packages/app/src/pages/session/composer/session-composer-controls.ts` | 在当前 session composer 内暴露 agent、model、session tabs 和 project controls。 |

### 2.2 应当借鉴的模式

1. **Provider registry 而不是散落的 if/else**：Provider 配置由统一注册表管理，运行时根据 provider id 找 adapter、认证规则和模型目录。
2. **Model catalog 独立于 secret**：模型资料可公开投影，密钥只用于连接和调用；前端无需获取原始 key。
3. **能力元数据先于 UI 决策**：UI 可以根据 reasoning、tool call、attachment、temperature、modalities、上下文/输出上限来提示和过滤模型，但不能把这些字段当作调用成功或 flag 有效性的证据。
4. **Provider-specific loader**：特殊供应商可以实现自定义 `autoload`、`getModel`、`vars`、`options`、`discoverModels`，但 D-Swarm 应把它落在受控的 Python adapter 接口中。
5. **用户模型状态独立持久化**：recent、favorite、可见性和 variant 是用户偏好，不应污染 Provider 基础配置。
6. **公开投影**：把内部运行时对象转换成只包含 UI 所需字段的 DTO，避免函数、symbol、undefined 或凭据进入 API payload。

### 2.3 不应直接照搬的部分

- OpenCode 面向通用编码助手；D-Swarm 的主界面是 swarm run 的观测与控制台，中心信息仍应是 Reason intent、worker lane、evidence 和 provenance，而不是普通聊天记录。
- OpenCode 的 Provider SDK 插件体系假设本地应用可以加载 npm 包；D-Swarm 的 Worker 处于沙箱/容器边界，Provider 上游 key 不能进入 Worker 容器。
- OpenCode 的模型自动发现和在线 catalog 不能影响 D-Swarm 的黑盒评估隔离，也不能将网络 writeup/知识库结果送入 capability eval。
- D-Swarm 现有事件契约、旧 session replay 和 Pi-only/ReasonSwarm 约束优先于参考项目的命名或状态模型。

---

## 3. D-Swarm 当前基线与主要差距

### 3.1 已有能力

当前 Provider 控制面已经具备较好的安全基础：

- `dswarm/solver/llm_providers.py` 已提供 Provider 模板、Provider secret store、端点校验、解析和 probe；`apps/web/llm_providers.py` 是兼容性 re-export。
- `apps/web/routes/llm_settings.py` 已有 Provider probe 与 LLM endpoint test。
- `apps/web/worker_settings.py` 已有配置 draft/apply、revision conflict、Provider 引用检查和 secret redaction。
- `apps/web/ui/components/WorkerSettingsWorkspace.tsx`、`workerSettingsDraft.ts` 和 `workerSettingsText.ts` 已支持 Worker/Reason/Titler、模型、端点、wire API、诊断和批量配置。
- Worker runtime 使用 task-token/gateway 方案，Worker 不应持有 upstream API key。

当前 UI 也已经具备 command deck 基础：

- `ThreadRail` 管理 run fleet；
- `Conversation` 负责草稿启动和操作员交互；
- `DecisionTimeline`、`SwarmInspector`、`WorkerLanes`、`ArtifactPanel`、`Blackboard` 展示 swarm 状态；
- `CommandPalette`、BTW、toast、键盘快捷键、可调整 rail/inspector 宽度已经存在；
- `lib/normalize.ts` 和事件投影支持 live SSE 与旧 session replay。

### 3.2 主要差距

| 领域 | 现状 | 差距/影响 |
| --- | --- | --- |
| UI 信息架构 | 运行监控能力强，页面逐步从 conversation-first 转向 command deck | 缺少统一的 `Home → Run → Evidence/Inspector → Settings` 导航语义；新用户不易理解当前 run、目标和模型绑定。 |
| Session 状态 | 有 run route、rail、replay 和 draft | session tabs、最近使用模型、跨 run 的用户模型偏好尚未形成统一状态层。 |
| Model selector | Settings 中可填 model，部分 Provider 有 models 列表 | 运行中的当前模型、Provider 连接状态、能力、variant、recent/favorite 没有统一投影和选择器。 |
| Provider registry | 有模板和配置清洗 | 缺少明确的 adapter/catalog/capability 分层；特殊 wire API 的扩展容易回到条件分支。 |
| Model discovery | 已有 fetch/probe 方向 | 还需要标准化发现结果、缓存、失败隔离、模型去重和静态目录与动态目录的合并策略。 |
| 健康与 fallback | 有 endpoint/profile health | Provider 健康、模型可用性、最近失败原因和绑定 profile 的影响范围需要成为可观察状态；fallback 不能静默发生。 |
| 配置体验 | Worker settings 已有分栏编辑和 revision | Provider 管理、secret 写入、模型 catalog、profile 绑定尚未拆成清晰的工作流。 |

结论：本项目不需要“重做 UI”或“重做 Provider”；应在已有控制面之上补齐统一的状态投影、Provider registry 契约和渐进式工作区。

---

## 4. 目标架构

### 4.1 分层

```text
Next.js / React UI
  ├─ AppShell / Navigation / Run workspace
  ├─ Model & Provider contexts (public DTO only)
  ├─ Session/run event projection + replay
  └─ Settings forms / diagnostics
             │ REST + SSE (existing contracts, additive fields only)
FastAPI control plane
  ├─ Provider registry + public catalog projection
  ├─ Secret resolver (write-only API, filesystem/env-backed)
  ├─ Adapter registry + probe/discovery/health
  ├─ Profile binding validator / revision apply
  └─ Existing run control and event projection
             │ task token / gateway
Worker and Reason runtime
  ├─ resolve provider binding
  ├─ call gateway/allowed upstream
  ├─ emit execution output and status events
  └─ existing provenance / cost / graph gates remain authoritative
```

### 4.2 生命周期

1. 操作员在 Settings 创建或选择 Provider，填写非敏感连接信息并通过 secret write-only API 写入 key。
2. 后端将配置规范化，校验 provider id、URL、wire API、认证字段、模型和 profile 引用，生成 revision。
3. Registry 选择对应 adapter，执行连接 probe；动态模型发现是可选操作，失败只影响 catalog 状态，不破坏已保存的静态模型配置。
4. 操作员把 Provider + Model 绑定到 `WorkerProfile`、Reason profile 或 Titler profile；应用前执行引用完整性和能力约束检查。
5. 创建 run 时，后端生成本 run 使用的 **公开 binding snapshot**（provider id、model id、endpoint host、能力摘要、配置 revision），secret 仍只在运行时解析。
6. Worker 通过现有 gateway/task token 调用上游；事件中只出现脱敏的 Provider/model 标识与健康/耗时信息。
7. UI 由 SSE 事件和 snapshot 投影当前状态；run 完成后通过 session JSONL 重放相同 projection。

---

## 5. UI 升级方案

### 5.1 新的信息架构

目标布局不是普通聊天应用，而是可解释的 swarm command workspace：

```text
┌──────────────────────────────────────────────────────────────┐
│ AppBar: 项目 / 当前 Run / Stage / flags / cost / connection   │
├──────────────┬──────────────────────────────┬────────────────┤
│ Run Rail     │ Run Workspace                │ Inspector      │
│              │ ┌─ Conversation / Composer  │ ├ Evidence      │
│ folders      │ ├─ Decision Timeline        │ ├ Workers       │
│ recent runs  │ ├─ Intent / review cards    │ ├ Blackboard     │
│ status       │ └─ operator directives       │ └ Provider      │
├──────────────┴──────────────────────────────┴────────────────┤
│ Command bar: pause / focus / hint / submit / open palette    │
└──────────────────────────────────────────────────────────────┘
```

建议把现有组件迁移为职责明确的 shell，而不是一次性重写：

- `AppShell`：承载语言、主题、登录、toast、快捷键和全局 dialog。
- `RunRail`：由现有 `ThreadRail` 演进，增加 folder、最近 run、活动状态、未读事件和固定 run。
- `RunWorkspace`：根据 run 状态切换 draft、live、finished、replay 四种视图；主视图默认是 `DecisionTimeline`，Conversation 作为启动/补充干预面板。
- `InspectorDock`：由 `SwarmInspector`/`ArtifactPanel` 演进，支持 evidence、workers、blackboard、runtime、provider binding 五个稳定 tab。
- `SettingsWorkspace`：保持当前 Worker Settings 的 split-pane 模式，增加 Provider Catalog 与 Profiles 两级导航。
- `CommandPalette`：扩展到 run、panel、provider/model、HITL 命令，但所有改变仍调用既有后端控制 API。

### 5.2 React context/store 分层

参照 OpenCode 的 context 分层，D-Swarm 建议建立以下 React context；每层只负责一种状态：

| Context | 内容 | 持久化 |
| --- | --- | --- |
| `AppShellContext` | theme、locale、toast、dialog、快捷键 | localStorage（不含 secret） |
| `RunNavigationContext` | 当前 project、folder、run、draft、deep link | URL + localStorage 的非敏感偏好 |
| `RunStreamContext` | SSE connection、last event id、reconnect、replay cursor | 仅内存；cursor 可用于恢复 |
| `RunProjectionContext` | timeline、stage/status、flags、workers、evidence、budget | 从事件重建；不直接写业务状态 |
| `ProviderCatalogContext` | public providers、models、connected/health、catalog revision | 可缓存 public DTO |
| `ModelPreferencesContext` | recent、favorite、visibility、variant | localStorage/用户配置，不进 secret store |
| `SettingsDraftContext` | 配置草稿、dirty paths、revision、diagnostics | 仅内存，明确 Apply 后提交 |

Provider/model 的联合 key 必须稳定，例如 `provider_id:model_id`；不能只用 model id，否则不同 Provider 的同名模型会互相覆盖。

### 5.3 Model/Provider selector

在 composer、Worker Settings 和 run inspector 中复用同一个 `ModelSelector`：

- 分组显示 Provider → Model；Provider 显示连接状态和最近 probe 时间。
- 支持 recent、favorite、show/hide；默认只显示 active/最近使用模型，提供“显示全部”。
- 展示 reasoning、tool call、attachment、temperature、输入/输出 modality、context/output limit 等能力摘要。
- 对 `variant` 使用 Provider/model 联合 key；variant 仅是请求参数预设，不得修改 provenance 或成本判定。
- 选择器只允许选择当前 profile 能力约束内的模型；不支持 tool call 的模型不能被静默绑定到需要工具的 Worker profile。
- run 已启动后，显示不可变的 binding snapshot；切换模型属于下一次 run 或显式的 operator action，不改变历史事件含义。

建议组件接口：

```ts
export type ModelKey = { providerId: string; modelId: string };
export type ModelSelection = ModelKey & { variant?: string };

<ModelSelector
  value={selection}
  providers={catalog.providers}
  recent={preferences.recent}
  favorites={preferences.favorites}
  onChange={setSelection}
  capabilityFilter={{ toolCall: true }}
/>
```

### 5.4 Run workspace 与事件投影

- 保持现有 `normalize.ts` 作为唯一入口：live SSE、历史 JSONL 和 API snapshot 都先 normalize，再交给 projection reducer。
- 新增 Provider 相关事件时，优先使用 additive payload：`provider_binding_resolved`、`provider_health_changed`、`model_catalog_refreshed`、`provider_dispatch_paused` 等；不改已有事件字段含义。
- stable kind 必须先写入 `docs/ui-event-contract-v1.md`，再实现 typed reducer 和 deterministic Vitest replay test；未升级的 kind 只能 generic timeline 展示。
- UI 要区分：配置健康（control plane）、运行健康（run）、模型调用结果（execution）和 flag/provenance 结果（correctness）。不能因“Provider probe 成功”显示“solve 成功”。
- SSE 重连使用 `Last-Event-ID`/已有 cursor 语义；重连后按 event sequence 去重。事件重复、乱序和 replay 必须有测试。
- 长输出默认摘要显示，点击后展开 artifact/tool output；避免把完整模型响应和凭据放进初始 payload。

### 5.5 Settings 工作流

Provider 管理页建议采用三栏/两栏模式：

1. **Provider 列表**：内置/自定义、连接状态、绑定数量、最近探测结果。
2. **Provider 编辑器**：label、base URL、wire API、auth mode/header/prefix、timeout、静态 models、发现模型、headers/options（严格白名单）。
3. **诊断面板**：URL 规范化、DNS/网络/HTTP/auth/model-call 分层结果、最近错误、影响的 profiles。

Secret 操作单独放在“凭据”区域：

- 输入框永远为空或显示 `已配置`，绝不回填原值。
- `保存/替换` 和 `删除` 是明确动作，替换前可显示影响范围。
- probe 默认使用已保存 secret，但响应只返回 `present: true/false` 和脱敏诊断。
- UI 状态机区分 `未配置`、`已配置未验证`、`验证成功`、`验证失败`、`过期/需重新认证`。

### 5.6 响应式和可访问性

- 桌面端保留可调整 Rail/Inspector 宽度；窄屏时 Inspector 变为 drawer，Rail 变为 overlay。
- 所有关键操作均有按钮入口和键盘入口，不能只依赖单键快捷键。
- 运行中布局不能因 SSE 高频更新自动跳动；只在结构变化时刷新 graph/layout。
- 面板 tab、dialog、toast、combobox 提供明确焦点管理、ARIA label 和 Esc 关闭语义。
- 中英文文案同步维护，Provider 错误保留可诊断的 layer/code，不只显示自然语言。

---

## 6. LLM Provider 配置升级方案

### 6.1 Provider Registry

建议在 `dswarm/solver/llm_providers.py` 现有基础上拆出明确的领域对象（可先用 dataclass/TypedDict，避免一次引入复杂框架）：

```python
@dataclass(frozen=True)
class LLMProviderSpec:
    id: str
    label: str
    adapter_id: str
    base_url: str
    wire_api: str                 # auto/openai/openai-chat/openai-responses
    auth_mode: str                # bearer/x-api-key/custom
    auth_header: str
    auth_prefix: str
    env_key_names: tuple[str, ...] = ()
    timeout_ms: int | None = None
    header_timeout_ms: int | None = None
    chunk_timeout_ms: int | None = None
    static_models: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)
```

Registry 负责：

- provider id 规范化和唯一性；
- 模板与用户配置合并；
- adapter 查找；
- 静态 catalog 与动态发现结果合并；
- profile 引用完整性；
- 对外 public projection；
- 不负责读取 secret 明文到 API 响应。

Provider 配置建议保留 `llm_providers` 兼容字段，逐步增加：

```json
{
  "id": "corp-relay",
  "label": "Corporate OpenAI Relay",
  "adapter_id": "openai-compatible",
  "base_url": "https://llm-relay.example.invalid/v1",
  "wire_api": "openai-chat",
  "auth_mode": "bearer",
  "auth_header": "Authorization",
  "auth_prefix": "Bearer",
  "models": ["reasoner-main", "coder-main"],
  "default_model": "reasoner-main",
  "timeouts": {
    "request_ms": 180000,
    "header_ms": 30000,
    "chunk_ms": 45000
  },
  "options": {
    "organization": "team-a"
  }
}
```

`options` 和自定义 headers 不应成为任意 header 注入通道。第一阶段只开放明确白名单；需要新字段时增加 adapter schema 和测试。

### 6.2 Adapter Registry

Adapter 是 Provider 与上游协议之间的受控翻译层，至少提供：

```python
class LLMProviderAdapter(Protocol):
    adapter_id: str

    def normalize(self, provider: Mapping[str, Any]) -> NormalizedProvider: ...
    def build_request(self, provider: NormalizedProvider, *, model: str, messages: list[dict[str, Any]], ...) -> RequestSpec: ...
    def parse_response(self, response: Response) -> ParsedResponse: ...
    def probe(self, provider: NormalizedProvider, *, secret: str, model: str | None) -> ProbeResult: ...
    def discover_models(self, provider: NormalizedProvider, *, secret: str) -> list[ModelRecord]: ...
```

首批 adapter：

1. `openai-compatible`：覆盖 DeepSeek、OpenRouter、Moonshot、Zhipu/Qwen compatible mode、Groq、Together、SiliconFlow 和自定义 relay。
2. `openai-responses`：仅在实际调用链需要时启用；不可因字段名称相似就强行发送 Responses payload。
3. `ollama`：默认本地 endpoint，可无 key 或配置本地 gateway；仍需 URL/网络策略校验。
4. 后续特殊 Provider：以独立 adapter 加入，不在 `resolve_llm_provider` 中堆叠供应商分支。

Adapter 的职责边界：只构造和解析请求，不决定 flag 是否有效，不写 shared graph，不修改成本账本，不决定 run 是否完成。

### 6.3 Model Catalog 与能力元数据

引入静态 + 动态双来源目录：

```python
@dataclass(frozen=True)
class LLMModel:
    provider_id: str
    id: str
    name: str
    family: str | None
    status: Literal["active", "alpha", "beta", "deprecated", "unknown"]
    capabilities: ModelCapabilities
    cost: ModelCost | None
    limits: ModelLimits | None
    release_date: str | None
    source: Literal["template", "config", "discovery", "env"]
    variants: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
```

`ModelCapabilities` 至少包含 `reasoning`、`tool_call`、`attachment`、`temperature`、`input_modalities`、`output_modalities`；`ModelLimits` 包含 context/input/output；成本字段可为空，未知时 UI 显示 `N/A`，不得猜测。

合并规则：

1. `config` 明确配置优先于 template；
2. 动态发现只能补充不存在的 model，不能静默覆盖 operator 明确写入的能力、成本和限制；
3. discovery 失败保留上一次成功的缓存并标记 stale，首次失败只返回错误状态；
4. model id 按 `provider_id + model_id` 去重；
5. deprecated 模型不能成为默认推荐，但历史 binding 仍可 replay；
6. model metadata 不属于执行证据，不进入 flag gate。

### 6.4 Secret Resolver

建议保持当前 write-only filesystem store，并统一解析顺序：

```text
env override (explicit deployment policy)
  → provider secret store (sessions/_secrets/llm_providers/<id>/API_KEY)
  → gateway/task-token projection (worker runtime)
```

具体要求：

- API/UI 只能 `PUT /secret` 或 `DELETE /secret`，不提供 `GET secret`。
- `public_provider_secrets` 只返回 provider id、present、updated_at、has_secret 等 metadata。
- endpoint、model、provider id 和 profile ref 的 revision 不应包含 secret 内容；revision 只哈希 public config + secret metadata。
- worker 容器继续只拿 task token/受限 credential projection；不能把 upstream key 写入环境变量、镜像、事件 payload 或日志。
- 删除 Provider 前先返回绑定 profiles；默认拒绝删除仍被绑定的 Provider，除非显式执行解绑/迁移。

### 6.5 Profile binding

将 Provider + Model 作为一个不可拆散的 binding：

```json
{
  "profile_id": "pi-web",
  "provider_ref": "corp-relay",
  "model": "coder-main",
  "variant": "balanced",
  "required_capabilities": {
    "tool_call": true,
    "input_text": true
  },
  "fallback_policy": "disabled"
}
```

解析时必须返回：

- 规范化后的 provider id/model id；
- 实际 adapter、wire API、endpoint host；
- secret 是否存在（布尔值，不是 secret）；
- capability mismatch；
- health state；
- config revision。

如果 `provider_ref` 存在，profile 不得静默使用自己的 `base_url`、另一个 credential account 或默认 DeepSeek key。已有的“bound provider 与 gateway upstream key 必须一致”约束继续保留，并在 apply 和 dispatch 两处双重校验。

### 6.6 Probe、discovery 与 health

探测分层并标准化结果：

```text
config → endpoint → network → auth → model-list → model-call → stream
```

每层返回 `ok`、`code`、`detail`、`elapsed_ms`、`retryable`、`redacted_target`；禁止返回 Authorization、完整 request body 或原始异常中可能含 key 的部分。

- `probe` 是显式 operator action，不因打开 Settings 自动调用。
- `/models` 成功不等于模型可调用；“验证模型”仍进行最小真实 chat/completion 调用，并明确可能消耗额度。
- discovery 应有超时、单 Provider 隔离、缓存 TTL 和手动刷新；一个 Provider 失败不得使整个 Settings 页面失败。
- health 只影响 UI 状态和派发前 fail-closed 检查；不能修改 evidence graph 中已经产生的 execution evidence。
- 健康状态建议：`unknown`、`configured`、`healthy`、`degraded`、`auth_failed`、`model_unavailable`、`paused`。

### 6.7 Fallback 策略（后续阶段）

第一阶段默认 `fallback_policy = disabled`，避免隐式换模型造成结果不可解释。后续如需要 fallback：

- 只能使用 profile 明确列出的候选 binding；
- 切换前发出 `provider_dispatch_paused`/`provider_recovery_scheduled` 等审计事件；
- 记录原 binding、候选 binding、原因和 operator policy；
- 切换不能跨越不兼容 capability；
- 成本 ledger 按实际 Provider 分开记账；
- run inspector 明确显示“已 fallback”，不能只显示一个最终模型名。

---

## 7. 建议 API 与 DTO

### 7.1 Provider catalog（公开投影）

```http
GET /api/settings/llm-providers
```

返回示意：

```json
{
  "revision": "public-config-rev",
  "providers": [
    {
      "id": "corp-relay",
      "label": "Corporate OpenAI Relay",
      "adapter_id": "openai-compatible",
      "base_url": "https://llm-relay.example.invalid/v1",
      "endpoint_host": "llm-relay.example.invalid",
      "wire_api": "openai-chat",
      "auth_mode": "bearer",
      "secret": {"present": true, "updated_at": "2026-09-02T00:00:00Z"},
      "health": {"state": "healthy", "checked_at": "2026-09-02T00:01:00Z"},
      "models": [
        {
          "id": "reasoner-main",
          "name": "reasoner-main",
          "status": "active",
          "capabilities": {"reasoning": true, "tool_call": true, "attachment": false},
          "limits": {"context": 128000, "output": 8192}
        }
      ]
    }
  ]
}
```

### 7.2 控制面操作

建议新增或统一以下 additive endpoints（最终路径以现有路由规范为准）：

| API | 作用 | 安全规则 |
| --- | --- | --- |
| `GET /api/settings/llm-providers` | Provider/model public catalog | 只返回 public projection。 |
| `PUT /api/settings/llm-providers` | 新增/更新非 secret 配置 | 校验 id、URL、adapter、wire API、options 白名单和引用。 |
| `DELETE /api/settings/llm-providers/{id}` | 删除 Provider | 有绑定时默认拒绝；不删除事件历史。 |
| `PUT /api/settings/llm-providers/{id}/secret` | 写入/替换 key | 请求可含 key，响应只返回 metadata。 |
| `DELETE /api/settings/llm-providers/{id}/secret` | 删除 key | 幂等，响应不含 key。 |
| `POST /api/settings/llm-providers/{id}/probe` | 连接/模型调用探测 | 分层错误、脱敏、超时；不写 run evidence。 |
| `POST /api/settings/llm-providers/{id}/discover` | 发现模型 | 缓存、隔离失败、只返回模型 metadata。 |
| `GET /api/settings/llm-providers/{id}/health` | 查看最近健康状态 | 只返回状态和脱敏详情。 |
| `PUT /api/settings/profiles/bindings` | 批量应用 profile binding | revision conflict、引用完整性、能力校验。 |
| `GET /api/runs/{id}/provider-binding` | 查看 run binding snapshot | 只返回 id/model/adapter/host/capability，不返回 secret。 |

已有 `/api/settings/llm-providers/probe`、`/api/settings/llm/test` 和 Worker Settings API 应通过内部 registry 复用相同解析与 adapter，避免两个探测实现产生不同结论。

### 7.3 DTO 分离

至少维护三种视图：

1. `LLMProviderInternal`：仅运行时 resolver 使用，可包含 secret handle，不可直接序列化。
2. `LLMProviderPublic`：UI 使用，包含 catalog、secret metadata、health，不含 raw secret。
3. `RunProviderBindingSnapshot`：run 使用的不可变公开快照，包含 provider/model/adapter/wire API/host/revision/capabilities，不包含 raw secret。

所有响应出口统一经过 `sanitize_for_api`/显式 serializer；不能依赖“调用方记得删字段”。

---

## 8. 配置示例

以下示例均为假地址和占位符，不能填入真实 key，也不应提交真实 `.env`。

### 8.1 OpenAI-compatible 自定义 relay

```json
{
  "id": "team-relay",
  "label": "Team LLM Relay",
  "adapter_id": "openai-compatible",
  "base_url": "https://relay.example.invalid/v1",
  "wire_api": "openai-chat",
  "auth_mode": "bearer",
  "auth_header": "Authorization",
  "auth_prefix": "Bearer",
  "models": ["reasoner-main", "coder-main"],
  "default_model": "reasoner-main"
}
```

凭据通过控制面写入，或按部署策略从环境变量提供：

```text
DSWARM_PROVIDER_TEAM_RELAY_API_KEY=<set-outside-repository>
```

实际环境变量命名应由 secret resolver 的显式映射决定，不要让用户输入任意环境变量名后在 Worker 中执行。

### 8.2 本地 Ollama

```json
{
  "id": "ollama-local",
  "label": "Local Ollama",
  "adapter_id": "ollama",
  "base_url": "http://127.0.0.1:11434/v1",
  "wire_api": "openai-chat",
  "auth_mode": "custom",
  "auth_header": "",
  "auth_prefix": "",
  "models": ["qwen3:latest"]
}
```

容器部署时 `127.0.0.1` 的含义与宿主机不同；应在部署文档中明确 gateway/host alias，不要在 UI 中自动猜测网络拓扑。

### 8.3 Profile 绑定

```json
{
  "profile_id": "pi-crypto",
  "provider_ref": "team-relay",
  "model": "reasoner-main",
  "variant": "balanced",
  "required_capabilities": {
    "tool_call": true,
    "input_text": true
  },
  "fallback_policy": "disabled"
}
```

---

## 9. 安全回归要求

Provider/UI 升级必须把以下项目加入安全回归，而不是只测“请求能否成功”：

### 9.1 凭据与输出

- API key 不出现在 GET 响应、SSE、session JSONL、run snapshot、浏览器 localStorage、React error、toast、日志和 exception detail。
- secret store 写入使用私有目录、原子写入和既有权限策略；删除路径必须通过 validated provider id，不能接受 `..`、绝对路径或路径分隔符。
- public projection 进行递归 redaction，并增加针对嵌套 `headers.Authorization`、`options.apiKey`、`access_token` 的测试。
- probe/discovery 的错误文本经过脱敏；上游返回的 body 不能原样回传。

### 9.2 Endpoint 与 Provider 边界

- URL 只允许部署策略认可的 `http/https`；生产环境应提供 endpoint allowlist 或明确的 egress/SSRF 防护策略。
- 防止请求内网 metadata、Docker socket、控制面、loopback 或不在授权范围内的地址；若本地 Ollama 必须允许，应使用显式部署开关和网络隔离。
- `provider_id`、`model_id`、`adapter_id` 和 profile ref 均需长度、字符集和最大数量限制。
- 自定义 headers/options 采用白名单 schema；禁止任意 header 注入、代理配置注入、命令/代码执行字段。
- Provider 绑定存在时，实际 gateway upstream key、endpoint 和 provider_ref 必须一致；错误应 fail-closed。

### 9.3 运行与正确性

- Provider 健康、模型发现、UI guidance 和 operator hint 永远不能进入 provenance evidence 或直接接受 flag。
- 任何 Provider fallback 必须有明确事件和成本记录；不得覆盖原始 execution output。
- UI 的“验证成功”标签只表示探测请求成功，不表示 challenge 成功。
- capability eval 继续 offline，禁止 Provider discovery/online writeup 污染 solve-rate。
- 不改变 `_flag_ok`、anti-laundering、first-valid-flag/multi-flag completion 语义。

### 9.4 Web 安全

- 继续使用现有认证、CSRF/CORS 和 same-origin 代理策略；新增 settings endpoint 不能绕过登录。
- secret 写入接口禁止 GET、缓存和重放；考虑 body size/rate limit 和审计事件。
- 公开 catalog 可以缓存，但必须按配置 revision 失效，不能缓存 secret-bearing response。
- UI 的 model preferences 只保存 id/variant 等非敏感值，禁止保存 endpoint credential 或完整请求 headers。

---

## 10. 分阶段实施计划

### Phase 0：契约与安全基线（P0）

**产出**：Provider/Model/Public DTO、redaction 规则、事件命名和兼容策略。

- 为现有 `llm_providers.py` 编写 schema/normalizer，不改变旧配置读取。
- 统一 `probe_llm_provider`、`llm_test`、profile health 的解析路径。
- 补齐 secret/public projection、Provider 引用一致性和 URL 安全测试。
- 在 `docs/ui-event-contract-v1.md` 添加 Provider 事件候选表；只有实现后才升级 stable。

**验收**：现有 Python/UI 测试全绿；旧配置 replay 不变；故意注入 key 的 API/SSE/JSONL 测试失败即阻止合并。

### Phase 1：Registry + Catalog（P0）

**产出**：受控 adapter registry、静态 catalog、动态 discovery cache、public catalog API。

- 先实现 `openai-compatible` 和现有 Provider templates 的映射。
- adapter 仅封装 endpoint/wire/auth/request/response；不触及 solver gate。
- discovery 采用显式刷新和失败隔离；添加 stale 状态。
- 将 Provider 列表、model 列表、secret metadata、health 投影为一个版本化 DTO。

**验收**：自定义 OpenAI-compatible endpoint 可保存、probe、发现/手工添加模型并被 profile 引用；无 key 泄露。

### Phase 2：Model Selector + Profile Binding（P0/P1）

**产出**：统一 `ModelSelector`、Provider/model 联合 key、binding snapshot。

- 在 Worker Settings 中复用 selector，支持 recent/favorite/visibility/variant。
- 增加能力过滤和 profile-specific validation。
- run 启动时固定 binding snapshot，并在 inspector 展示实际 provider/model/adapter/host。

**验收**：同名 model 的不同 Provider 不冲突；错误 capability、缺 key、未知 Provider 均 fail-closed；运行中历史 binding 不因配置修改而变化。

### Phase 3：UI Shell/Session Workspace（P1）

**产出**：AppShell、RunRail、RunWorkspace、InspectorDock、Settings navigation。

- 从现有 `page.tsx`/`ThreadRail`/`DecisionTimeline`/`SwarmInspector` 渐进迁移。
- 新增 context/store，但 projection 仍复用既有 normalize/replay。
- 优先改善 deep link、panel persistence、reconnect 和键盘可达性。

**验收**：桌面/窄屏布局可用；live/replay/draft/finished 四种 run 状态一致；SSE 重连和旧 session replay 通过测试。

### Phase 4：Probe/Discovery/Health（P1）

**产出**：诊断面板、分层错误、健康事件和手动恢复。

- Provider Settings 提供 config/endpoint/network/auth/model-call 分层诊断。
- health 不自动切换 Provider；`paused` 和 recovery 状态可见。
- 所有诊断结果脱敏并限制大小。

**验收**：模拟 401、404、超时、流中断、模型不存在、relay `/models` 不可用时，UI 给出可操作且不泄露信息的状态。

### Phase 5：Explicit fallback、variants 与观测（P2）

**产出**：显式 fallback policy、variants、Provider 级 cost/latency/error dashboard。

- 仅对明确配置的候选 binding 启用 fallback。
- 对每次切换发审计事件并按 Provider 记账。
- variants 只影响请求参数；记录有效 variant。

**验收**：fallback 可回放、可审计、可关闭；无 provenance/成本/历史重放回归。

---

## 11. 测试与验收矩阵

### 11.1 Python / API

- Provider schema：合法/非法 id、URL、wire API、auth mode、模型数量上限、重复模型。
- Registry：模板 + config + discovery 的优先级、去重、stale 缓存和 adapter 选择。
- Secret：写入/替换/删除、路径穿越、原子写、权限、只写 API、递归 redaction。
- Binding：未知 provider、缺 key、模型不存在、capability mismatch、revision conflict、绑定 Provider 与 gateway key 不一致。
- Probe：每一层错误、超时、响应 body 截断和脱敏；`/models` 成功但 chat 失败时不能报告 fully healthy。
- API：认证/CSRF/CORS、body size/rate limit、公开 DTO 不含 secret、删除被引用 Provider 默认拒绝。
- Runtime：Worker 只得到 task token；secret 不出现在 container env、事件、日志和 session JSONL。

### 11.2 UI Vitest

- Provider/model 联合 key、recent/favorite/visibility/variant。
- catalog loading、partial failure、stale discovery、health 状态转换。
- ModelSelector capability filter 和 keyboard navigation。
- Settings draft/apply/revision conflict、secret replacement 的空值语义。
- SSE replay、断线重连、重复/乱序事件、binding snapshot 固定。
- run 状态 projection 与旧 fixture replay；新 Provider kind 未注册时 generic fallback。
- 检查 key、Authorization、options.apiKey 等敏感字段不会渲染或持久化。

### 11.3 构建与黑盒回归

每个阶段至少运行：

```text
uv run pytest -q
apps/web/ui: npm test / npm run lint / npm run build
git diff --check
```

涉及 Worker/容器时，再运行对应 Docker 集成测试；Windows 本地无法运行的 Linux syscall/runtime-agent 检查必须记录为平台限制，并在 Linux CI 验证。

必须保留至少一条真实黑盒 trace：flag 出现在 worker 的实际 stdout/stderr/artifact 输出，经 gate 接受；Provider probe 的返回、模型名称或 operator guidance 不得计为 solve。

---

## 12. 风险、回滚与运维

| 风险 | 缓解/回滚 |
| --- | --- |
| 新 catalog 与旧 `models` 配置不一致 | 保留旧字段读路径；生成 public catalog 时可回退到静态 models；按 revision 回滚。 |
| adapter 解析差异导致调用失败 | 先只接 OpenAI-compatible；每个 adapter 有 golden request/response fixture；按 adapter id feature flag 启用。 |
| 自动发现拖慢 Settings 或泄露上游错误 | 手动触发、严格超时、结果缓存、错误脱敏和单 Provider 隔离。 |
| fallback 使结果不可解释 | 初始关闭；后续显式候选、审计事件、binding snapshot 和成本分账。 |
| UI 状态与实际调用 Provider 不一致 | run 启动固化 snapshot；事件中携带非敏感 binding id；后端 resolver 是唯一事实来源。 |
| 新 reducer 破坏历史 replay | additive event payload、generic fallback、fixture replay、旧事件 contract test。 |
| endpoint 被利用为 SSRF | allowlist/egress policy、DNS/IP 校验、容器网络隔离、本地 Provider 显式开关。 |
| 配置升级误触及 solver substrate | 分阶段独立 commit；每个阶段只改 control plane/UI；gate、event spine、ledger 和 graph 改动必须另行 RFC。 |

回滚策略：

1. 通过 feature flag 关闭新 catalog、selector、discovery 和 fallback；旧 settings API 继续可用。
2. 保留旧配置字段和向后兼容读取，不做破坏性迁移。
3. Provider adapter 按 `adapter_id` 独立禁用；已有 run 使用其 immutable binding snapshot 完成 replay。
4. 任何发现 provenance、secret redaction、成本账本或事件 immutability 回归时，立即回滚该阶段，不以“solve rate 提升”换取安全退化。

---

## 13. 推荐优先级、依赖和拆分

```text
P0  DTO/redaction/schema/统一 resolver
        ↓
P0  adapter registry + OpenAI-compatible catalog/probe
        ↓
P0  model selector + profile binding snapshot
        ↓
P1  Run workspace / context 分层 / replay hardening
        ↓
P1  discovery + health diagnostics
        ↓
P2  explicit fallback + variants + provider observability
```

建议按以下小步提交，避免一个大 PR 同时改 UI、Provider 和 runtime：

1. `provider: add normalized public catalog contract`
2. `provider: unify adapter probe and redaction`
3. `provider: add openai-compatible discovery`
4. `ui: add provider model context and selector`
5. `ui: show immutable run provider binding`
6. `ui: migrate settings to provider catalog workspace`
7. `provider: add explicit fallback audit`

每个提交都必须有 deterministic tests；不以文档、模型自述或在线 writeup 作为 solve-rate 证据。

---

## 14. 最终验收标准

方案完成实施后，至少应满足：

- 操作员能在 UI 中创建自定义 Provider、写入 secret、探测 endpoint、发现或手工配置 model，并将 Provider+Model 绑定到 Worker/Reason/Titler。
- UI 能在 composer、settings、run inspector 中一致显示当前 binding；run 历史显示不可变 snapshot。
- Provider/model 目录支持能力、成本、限制、状态、recent/favorite/visibility/variant，未知值明确显示未知。
- 一个 Provider 的发现或健康失败不会拖垮其他 Provider 或已有静态配置。
- API key 不出现在 API、SSE、session、浏览器存储、日志、错误和容器 Worker 中。
- Provider 配置错误、secret 缺失、模型不支持、绑定不一致和 SSRF 风险均 fail-closed。
- 旧配置、旧 session 和旧 SSE 事件仍可读取/重放。
- `uv run pytest -q`、UI Vitest、lint、build、diff check 全绿；Linux runtime-agent/Docker 测试在对应 CI 环境通过。
- provenance gate、first-valid-flag/multi-flag completion、cost ledger、shared graph append-only 语义没有任何变化。

**结论**：优先建设“统一 Provider public catalog + 安全 binding + 复用 selector”的 P0 垂直切片，再升级 Run workspace。这样既能快速改善自定义 LLM 接入，又不会把 UI 视觉重构与 solver 核心风险耦合在一次变更中。
