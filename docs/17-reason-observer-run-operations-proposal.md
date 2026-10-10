# Reason / Observer / Worker Run 工作台方案

## 1. 文档目的

本文整理 Target、Run、LLM 配置、Reason 调度、Worker 执行、Observer 问答、Reviewer 审计和操作员指令栏的统一方案，先作为评审稿，不直接改变既有实现。

目标：

1. 新建 Run 能稳定进入真实 Worker 执行，真实 stdout/stderr、runtime failure 和 provenance 链路可见；
2. 设置中的 Worker/LLM 配置成为默认配置，新建 Run 只在确有需要时覆盖；
3. Provider、Worker、Reason 出现问题时能及时反馈，但不会因一次瞬时网络抖动过早切换；
4. 主 UI 只展示 Target/Run 摘要；Run 选中后才显示 Run 级操作栏；
5. 操作员可以向当前 Run 的 Reason 和指定 Worker 单播或组播；
6. 已终止 Run 的信息可以由 Observer 整理，经人工审计后才写入 Target 长期记录；
7. 路线、Worker 详情、绑定、账本和证据链都可读、可审计，不把 UI 推断当作执行事实。

> 不改变：provenance gate、反洗白规则、SharedGraph append-only、首个有效 flag 完成规则、Target/Run 历史事件。任何 flag 仍必须来自真实 stdout/stderr/artifact 输出。

---

## 2. 当前问题与现状基线

### 2.1 新 Run 为什么没有成功跑出 Target flag

当前链路已经有 Target → Run → Reason → Worker，但需要把“启动失败”和“解题失败”严格分开：

- 新 Run 向 `/start` 同时传入 Reason、Worker、Review、Titler 的 LLM 绑定；空值处理不清晰时会把“没有显式 Run 配置”误当成一个不完整覆盖；
- Worker 首轮执行如果只有 CLI 协议 envelope、没有真实 worker 文本或 token 使用，不能建立可恢复 session；否则 conclude fallback 会恢复 ghost session，出现 `No session found matching ...`；
- Worker runtime/provider 错误必须保持为 runtime failure，不能变成普通 dead-end，也不能成为 evidence；
- Reason 当前已有同一配置的有限重试，Worker 当前已有 retryable provider error 的恢复和 profile pause 逻辑，但 fallback 还没有形成清晰的“Run override → Settings default”角色级链路；
- Titler 只影响标题，不应阻塞解题；Observer 也不应占用 Worker 槽位或成为 flag 来源。

### 2.2 当前已有能力

- 设置页已有 Worker Profile、LLM provider registry、planner/Reason、Titler、Reviewer policy 与连接测试；
- Run 启动已有 binding snapshot，保存脱敏后的 provider/model/endpoint 元数据；
- Reason 已产出 typed Intent、执行 evidence audit，并有 planner retry、worker recovery、provider error 分类和批量告警；
- Worker 已有 intent claim lease、conclude、worker lifecycle、runtime status；
- Observer 当前以 BTW 只读旁路存在：读取 bounded evidence pack，回答 Run 状态，不写 graph、不进 swarm、不参与 flag 判定；
- 当前 Worker 详情使用折叠 lane 展示 facts、tool count、tokens、online、runtime、reasoning 和 tools；
- 当前路线页已有 Reason cycle → Intent → Worker/Evidence 关系，但视觉上是横向周期/列表布局，不是目标所需的居中向下树。

---

## 3. 总体角色模型

```text
Settings defaults
  ├─ Reason default
  ├─ Worker defaults / profile roster
  ├─ Reviewer default
  └─ Titler default
          │ copied as effective fallback candidates
          ▼
Run snapshot
  ├─ optional per-run override: Reason
  ├─ optional per-run override: Worker
  ├─ optional per-run override: Reviewer
  ├─ no per-run Titler override
  └─ fallback chain recorded per role
          │
          ▼
Reason ──dispatches──> Worker(s)
  │                         │
  ├─ observes worker/provider failures
  ├─ decides retry/fallback/pause
  └─ emits operator-visible diagnostics

Observer = read-only Run analyst
Reviewer = evidence/provenance/route auditor
Titler = small settings-only display assistant
```

### 3.1 角色职责

| 角色 | 职责 | 是否参与解题 | 是否写 Evidence/Target |
|---|---|---:|---:|
| Reason | 读取 Reviewer 发布的 reviewed context、Target/Operator context，以及 runtime/lifecycle 诊断；规划、恢复和降级 | 是，负责调度决策 | 只能基于审阅后的业务信息产生 Intent，不绕过 gate |
| Worker | 对指定 Intent 进行真实目标操作，产出 stdout/stderr/artifact 和执行报告 | 是 | 通过 SharedGraph/事件写入原始执行产物；报告先进入 Reviewer 隔离区 |
| Reviewer | 严格审视 Worker 的真实输出/报告，并评估 Reason Intent 的合理性、重复性、价值和风险 | 是，质量闸门 | 发布 reviewed context、revalidation、rejection、dedupe 和 route audit；不替代 provenance gate |
| Observer | 只读汇报实时进展；回答基于 reviewed context、事实、Intent、日志、路线、账本、证据链的问题 | 否 | 不写 graph、不改变调度、不成为 flag 来源 |
| Titler | 生成 Run 标题/短摘要等展示文本 | 否 | 不影响解题、不影响 flag、不参与 Run LLM override |

### 3.2 UI 名称

- 设置中的 `ReasonSwarm` 改为四个配置区：`Reason`、`Observer`、`Reviewer`、`Titler`；
- 当前 `Planner` 面向用户统一显示为 `Reason`，内部 `planner` 兼容别名可以保留在 API/配置迁移层；
- 新建 Run 不再显示 Titler 独立配置；
- 当前“询问 Reason”改为“询问进展”，回答者显示为 Observer；
- Run 绑定详情显示四个角色的 `Settings default / Run override / Effective / Fallback state`，而不是只显示一个冻结字符串。

### 3.3 Reviewer 质量闸门

Reviewer 不是把 Worker 的说法换一种措辞转给 Reason 的摘要器，而是独立、怀疑式的质量闸门。Worker 的自然语言报告、模型声称、猜测的 flag、未经复现的“已验证”都先进入隔离区；只有 Reviewer 明确给出审阅结论后，业务信息才进入 Reason 的规划上下文。

Reviewer 有两道闸门：

1. **证据闸门（Worker → Reviewer → Reason）**：逐项检查 Worker 报告是否由真实 stdout/stderr、tool result、terminal output、artifact 或可重复的目标响应支持；检查报告中的因果关系、目标状态、flag claim 和 negative result 是否与原始输出一致。
2. **Intent 闸门（Reason → Reviewer → dispatch）**：逐项检查 Reason 提出的 Intent 是否有 reviewed context 依据、是否与已验证方向/事实/已完成或进行中的 Intent 重复、是否只是低价值改写、是否值得消耗 Worker/token、是否需要改成更窄的 verifier 任务。

Reviewer 的结论至少包括：

```text
evidence: verified | candidate | rejected | needs_retest | runtime_only
intent: approved | revise | duplicate | blocked | defer
```

每条结论都必须携带 subject id、source event/artifact refs、理由、审阅者、时间、相关 route/Intent，以及下一步动作。`verified` 只表示 Reviewer 认可报告与证据一致，不能代替硬编码 provenance gate 接受 flag。

### 3.4 Reviewed context 发布契约

```text
Worker raw stdout/stderr/report
          │ append-only, quarantined
          ▼
Reviewer evidence review
          │ verified/candidate/rejected/needs_retest
          ▼
Reviewed context + Intent quality decisions
          │
          ▼
Reason planning context
          │ approved Intent only
          ▼
Worker dispatch
```

- 原始 Worker 输出必须保留，Reviewer 可以读取完整内容，Observer 可以在只读审计视图中引用；
- Reason 可以看到 Worker/runtime 的生命周期、退出码、provider error 和“等待 Reviewer”状态，用于恢复和降级判断；这些是运行诊断，不是业务事实；
- Reason 不得把 `candidate`、`rejected`、未审阅的 Worker prose 或未通过 Intent 闸门的建议当成已知事实或派发依据；
- Reviewer 不可用时，原始报告继续保存，但业务信息不得晋升到 Reason，新的业务 Intent 不得绕过 Reviewer 直接派发；仅允许必要的 runtime recovery/health probe；
- Reviewer 的否决、去重、要求重测和 Intent 改写都进入 append-only 审计链，不能静默删除 Reason 的原始提案。
 

---

## 4. LLM 配置模型：设置默认，Run 可选覆盖

### 4.1 配置原则

设置页是默认配置的唯一来源。新建 Run 可以完全不配置 LLM：

```text
Settings defaults
  reason_default
  worker_default / worker_profile roster
  reviewer_default
  observer_default
  titler_default

New Run
  optional reason_override
  optional worker_override / selected profile
  optional reviewer_override
  // no titler override
```

规则：

1. 新建 Run 默认只选择 Target 方向、Run instruction、Target hints、继承 facts 和 Worker Profile；不需要展开 LLM 配置；
2. “使用默认配置”是显式可见状态，不是空白或未配置；
3. 用户选择“为本 Run 覆盖 LLM”后，才显示 Reason、Worker、Reviewer 三个角色的 provider/model/account 选择；
4. Titler 永远使用设置中的 Titler 配置；新建 Run 不再允许单独覆盖；
5. 每个 Run 在启动时冻结脱敏 binding snapshot，同时保存 role-level source：`settings_default` 或 `run_override`；
6. 每个 role 保存 fallback candidate 的稳定引用，不复制 secret；
7. 已启动 Run 不因设置修改而静默改变 effective provider。只有用户明确执行“恢复/重试并切换到默认”或新建 Run 才使用新的设置。

### 4.2 推荐请求形状

```json
{
  "target_id": "target-...",
  "direction_id": "direction-...",
  "prompt": "本次 Run 指令",
  "worker_profile_id": "pi-web",
  "llm_mode": "settings_default",
  "llm_overrides": {},
  "review_policy": {},
  "include_target_hints": true,
  "target_hint_ids": []
}
```

覆盖时：

```json
{
  "llm_mode": "per_run_override",
  "llm_overrides": {
    "reason": {"provider_ref": "relay-a", "model": "reason-model"},
    "worker": {"provider_ref": "relay-b", "model": "worker-model"},
    "reviewer": {"provider_ref": "relay-a", "model": "review-model"}
  }
}
```

后端不接受客户端传入 secret。`llm_overrides` 只包含 provider/model/protocol/account reference 等非 secret 元数据；凭据仍从服务器账号/Provider registry 解析。

### 4.3 fallback 链

每个角色独立处理：

```text
Run override (如果存在)
  ↓ 不可用/被 Reason 判定持续失败
Settings default
  ↓ 仍不可用
Role blocked / operator attention
```

- Worker 的 fallback 是角色级和 profile 级的，不是把一个 Worker 的失败直接改成另一个不相关的 Worker；
- Reason 的 fallback 只替换 Reason provider/model，不改变 worker 的 evidence 或 flag 规则；
- Reviewer fallback 不能把未经审计的 candidate 升格为 verified；切换期间仍必须保持证据隔离和 Intent dispatch 闸门，default 也不可用时暂停业务信息晋升与新业务派发；
- Observer/Titler 失败只影响汇报或标题，不得将 Run 标记为 challenge 失败；Reviewer 失败则必须明确显示质量闸门阻塞，不能降级为“自动相信 Worker”；
- 每次切换发出可追溯事件：`provider_recovery_directive`、`provider_recovery_activated`、`binding_effective_changed`，并在 binding inspector 中显示切换前后角色和原因。

### 4.4 设置页说明文案

设置页顶部和每个角色卡片增加明确说明：

> **这里配置的是默认配置。** 新建 Run 默认使用这里的 Reason、Worker、Reviewer、Observer 和 Titler 配置。只有在新建 Run 中显式选择“覆盖默认”时，Run 才使用单独绑定。Run 运行期间如覆盖配置持续失败，Reason 可按恢复策略切回设置默认配置；切换会记录在 Run 审计链中。

Run 创建向导显示：

> 默认使用设置中的 LLM 配置。你可以在“本 Run 覆盖”中临时替换 Reason、Worker 或 Reviewer；Titler 始终使用设置配置。

---

## 5. Fallback 与健康判断方案

### 5.1 判定责任边界

**Worker 出问题是否回退：由 Reason 判断。** 但 Reason 不读取模糊的 UI 状态，而是读取结构化的 worker outcome、provider diagnostic、runtime status、intent claim/conclude 和 evidence 结果。

**Reason 自身出问题是否回退：由宿主调度器的健康探针 + 有限重试/熔断判断。** Reason 不能在自身 LLM 已不可用时负责给自己下判断。

这避免两个风险：

- 把一次普通业务失败误判成 provider 故障；
- 让已经失联的 Reason 无限自我重试，既不反馈也不切换。

Reason 不直接相信 Worker 的报告。Reviewer 先对每个 Worker/Intent 维护并发布审阅结论，Reason 再结合 reviewed context 与运行诊断做恢复和规划：

1. **执行层**：是否启动、是否有真实 stdout/stderr、runtime rc、timeout、OOM、cancelled；
2. **Provider 层**：`insufficient_quota`、`auth_invalid`、`model_not_found`、`rate_limited`、`transient_network`、`timeout`、`provider_down` 等分类；
3. **任务层**：Worker 是否 claim 了 Intent，是否返回实际尝试和反馈，是否产生 candidate/verified fact，是否只留下空 envelope。

### 5.2.1 Worker 报告隔离与 Reason 输入契约

- Worker 的原始反馈先写入 `worker_report_pending_review` / 等价的待审阅状态；“我发现了”“已验证”“flag 是……”等模型自报不能直接写入 Reason 的业务事实上下文；
- Reviewer 对照真实执行输出审查：报告引用的 URL/命令/响应是否存在，响应是否支持结论，是否遗漏反例，是否把计划或猜测写成结果；必要时提出独立 verifier/retest；
- Reviewer 发布 `reviewed_context` 后，Reason 才能使用其中的 verified facts、confirmed dead-ends、route constraints、rejected claims 和 approved Intent decisions；
- Reason 仍可即时读取 runtime/lifecycle/provider diagnostics，以便判断重试、fallback、暂停和 Intent 恢复，但这类诊断不得被包装成 Target 事实；
- 若 Reviewer 判定 `candidate` 或 `needs_retest`，Reason 只能得到“待验证任务/不确定性”，不能沿用该 claim 继续扩展同一路线；若判定 `rejected`，Reason 必须将其视为否定信号，除非有新的 reviewed evidence；
- Reviewer 的输出应短而结构化，原始报告和审阅理由通过引用保留，避免把整段 Worker firehose 注入 Reason，降低 token 消耗和误导风险。
 
只有执行层和 Provider 层指向 LLM/runtime 问题时才考虑 fallback；以下不 fallback：

- Worker 有真实工具输出但目标路线失败；
- Target 返回 401/403/404 等业务结果；
- Reason 判定路线 dead-end；
- Worker 明确报告目标没有该 surface；
- provenance gate 拒绝了 flag claim。

### 5.3 推荐阈值，避免过于敏感

**单 Worker：**

- 明确 fatal：额度不足、鉴权失败、模型不存在/无权访问 —— 立即标记当前 override 不可用，暂停该 override 的后续派发，通知 Reason；如果 settings default 健康，允许下一次同类 Intent 使用 default；
- retryable：网络、timeout、provider 5xx、429 —— 同一 Intent 最多自动重试 2 次，采用退避；一次失败不切换；
- unknown/runtime 空转 —— 不自动切换；先保留 runtime failure，交给 Reason 在下一 cycle 判断。

**滑动窗口：**

- 60 秒窗口内同一 provider/account/role 发生至少 3 次同类 fatal，或至少 2 个不同 Worker 受影响且占当前活动 Worker 的 ≥50%，触发 `provider_batch_alert`；
- retryable 只有在同一 override 连续失败 3 次，或影响 ≥50% 活动 Worker 时才激活 default fallback；
- fallback 激活后至少冷却 60 秒，避免 override/default 之间来回抖动；
- 切换前进行一次不消耗业务额度的配置/凭据/endpoint probe；探针失败才进入 fallback，探针成功则保留当前配置并等待退避；
- fallback 失败不能静默循环：最多一次 default activation；default 也失败则 role blocked，要求操作员处理。

阈值可放在设置的“高级恢复策略”中，但默认值必须保持上述保守策略，并在 Run binding/Health 中显示当前计数和阈值。

> **实现范围说明（与代码一致）：** “≥50% 活动 Worker 受影响”这一条只适用于 **Worker 角色的聚合统计**（同一 provider/account 在 60 秒窗口内影响多个活动 Worker 时）。Reason 与 Reviewer 是按角色单例，不存在“受影响 Worker 比例”，因此它们的恢复只走 `连续失败次数` 与 `fatal 立即熔断` 两条路径；调度器在调用角色 fallback 时显式传 `affected_workers=0`，不会伪造比例。

### 5.4 Reason 自身不可用的判断

Reason 使用专用状态机，不让模型自行决定是否切换：

```text
healthy
  ├─ 一次 timeout/429/5xx → degraded + retry backoff
  ├─ 连续 3 次同 cycle 请求失败 → retrying / operator-visible warning
  ├─ 连续 6 次 planner cycle unavailable → circuit_open
  └─ circuit_open + settings default probe ok → activate settings default
                                      └─ probe fail → reason_blocked
```

细则：

- 同一 Reason cycle 内最多 3 次请求，沿用当前 backoff；
- 失败计数只统计真正的 provider/runtime failure，不统计合法 JSON 但 `intents=[]`；空 Intent 是 Reason 的规划结果，应交由正常 course-correct/fallback bootstrap 逻辑处理；
- `insufficient_quota`、`auth_invalid`、`model_not_found` 不等待 6 次，第一次即可打开该 override 的 circuit，但仍要先做一次 default probe；
- 网络波动、timeout、429 默认累计，不因一次失败切换；
- default probe 也失败时，Run 状态为 `reason_blocked`/`waiting_for_operator`，明确提示“解题证据未失败，Reason 调度暂不可用”，不把 Run 标成 solved/普通 challenge failed；
- 若 Reason 暂不可用但当前 Worker 仍在运行，保留 Worker 执行和真实输出，暂停新 Intent 派发，避免继续盲目 spawn；
- 恢复成功后由 Reason 读取当前 graph 重新规划，不能从旧模型回答中直接恢复 Intent 状态。

**状态投影契约（`reason_health_changed`，append-only，`actor=reason`）：**

```text
health_state       healthy | degraded | retrying | circuit_open | waiting_for_operator | reason_blocked
failures           连续不可用 planner cycle 数
max_failures       熔断阈值（DSWARM_REASON_MAX_PLANNER_FAILURES，默认 6）
request_failures   当前 cycle 内已失败的请求数
request_limit      单 cycle 请求上限（固定 3）
retry_window_s     重试窗口秒数（默认 60）
retry_not_before   由 Run Health 投影计算（ts + retry_window_s），仅 degraded/retrying 出现
immediate          true = fatal 首次即熔断（quota/auth/model_not_found/invalid_base_url）
fallback_active    已切到设置默认并恢复
blocked_reason     公开的原因码，不含 provider 异常原文或凭据
```

- 只有真正的 provider/runtime failure 进入 `failures`；合法 JSON 但 `intents=[]` 不计数；
- 健康状态由宿主调度器产生，模型无法自行决定切换；
- Run Health / Binding Inspector 只显示计数、阈值和窗口，不显示原始异常文本。

### 5.5 反馈层级

1. **实时 timeline**：一条短消息，标明角色、provider、分类、当前动作；
2. **Run Health**：显示 `degraded / fallback active / blocked`、失败计数、窗口、下一次重试时间；
3. **Binding Inspector**：显示 `configured`、`frozen`、`effective`、`source` 和 fallback reason；
4. **Observer**：回答“为什么回退/是否已恢复/哪些 Intent 受影响”，引用 event、Intent 和 runtime；
5. **操作员动作**：`恢复`、`停止`、切换到设置默认、重新探测；不自动改写历史。

---

## 6. 新建 Run 向导调整

### 6.1 保留在向导中的内容

第一步：

- Target 预览；
- 本次 Run 指令；
- 主方向；
- Target hints 是否注入；
- 是否继承已验证 facts（只读）。

第二步：

- Worker Profile / 方向选择；
- 显示默认配置摘要：Reason、Worker、Reviewer、Observer、Titler 的 provider/model；
- 显式按钮或开关：`使用设置默认（推荐）` / `本 Run 覆盖 LLM`。

第三步仅在选择覆盖时出现：

- Reason provider/model；
- Worker provider/model；
- Reviewer provider/model；
- 覆盖范围和 fallback 会回到设置默认的说明。

### 6.2 从向导删除

- 删除 Titler 独立配置卡；
- 不允许新建 Run 单独配置 Observer；Observer 是只读观察角色，固定读取设置中的 Observer 配置；
- 不把“是否测试 provider”做成启动必经步骤；可提供启动前非额度探针，但不能因为可选诊断失败隐藏启动错误；
- 不把 default binding 显示成“Configured default”这种模糊空值，要具体显示“设置默认：provider/model”。

### 6.3 Run snapshot

Run 启动后的绑定详情至少包含：

```json
{
  "roles": {
    "reason": {
      "configured_source": "settings_default|run_override",
      "configured": {"provider_ref": "...", "model": "..."},
      "fallback": {"provider_ref": "...", "model": "..."},
      "effective_source": "configured|settings_fallback",
      "state": "ready|degraded|fallback|blocked"
    }
  }
}
```

所有字段脱敏；secret 不进入 Target、Run summary 或事件 public payload。

---

## 7. Run 指令栏：只属于选中的 Run

### 7.1 主 UI 行为

当前底部指令栏不作为主页固定主 UI：

- 未选择 Run：完全不渲染指令栏；主页只显示 Target Library/Target Workspace；
- 选择一个 Run：在该 Run 的 selected workspace、Run detail 或 Run drawer 内显示；
- 指令栏作用域始终是当前 selected Run，不能从全局主页向多个 Run 广播；
- Run 切换时清空未发送的草稿，重新加载当前 Run 的 target roster；
- 终止 Run 仍可显示“回顾”和“恢复”，但不显示对已结束状态无意义的即时动作。

### 7.2 只保留四类指令

| 指令 | 语义 | 输入 | 默认动作 |
|---|---|---|---|
| 提示 | 将用户文本作为 operator guidance 注入当前 Run；不伪造 evidence | 必填文本 | 发给选定对象；Reason 决定何时形成新 Intent |
| 停止 | 停止当前 Run 的 Reason 接续和活动 Worker | 无 | 二次确认后 graceful stop，并记录 operator event |
| 恢复 | 让 Reason 接续当前 Run；Reason 再检查 Worker/Intent 并决定恢复/重开/新建 Worker | 可选短说明 | `/resolve` 或 live resume；不是直接恢复某个 ghost session |
| 回顾 | 对已终止 Run 做 Observer 汇总 | 无或可选问题 | 打开 Review/Audit modal，不改变 Run |

删除现有 `redirect`、`focus`、`pause` 的用户级动作入口。内部调度仍可保留必要的 pause/retry 状态，但不向用户暴露为四类之外的主指令。

### 7.3 对象选择器

对象集合只来自当前 Run：

```text
广播（当前 Run 的 Reason + 所有 Worker）
Reason
worker-1
worker-2
reviewer（如当前 Run 有审计 Worker）
```

交互要求：

- 支持多选；可单选实现单播，可选 Reason + 多个 Worker 实现组播；
- 广播是一个明确的特殊选择，不与具体对象同时产生歧义：选择广播时清空其他对象；
- 没有 Worker 时仍可单选 Reason；
- 对象名称、在线状态、角色、当前 Intent 和 runtime health 一起显示；
- 指令 payload 带 `run_id`、目标对象列表、action、text、client request id；后端校验对象确实属于该 Run；
- Worker 是 single-shot 时，“提示”不强行中断正在运行的进程，交给 Reason 形成下一 Intent/下一 Worker guidance；
- “停止”必须作用于当前 Run，不能只停止一个 lane；若需要单 worker kill，保留在 Worker 详情的局部控制，不作为主指令栏动作。

### 7.4 提示注入边界

提示进入 operator guidance / Intent 链路：

```text
operator directive received
  → selected recipients recorded
  → Reason reads directive
  → Reason decides whether to create/reopen an Intent
  → Worker receives guidance in next eligible execution
```

用户提示不是 Evidence、Fact 或 Flag 来源；不能直接写入 Target verified facts。

---

## 8. 回顾与 Target 审计持久化

### 8.1 回顾流程

仅对已终止 Run 启用：

1. 点击“回顾”；
2. 后端/Observer 构建只读 snapshot：
   - verified/candidate facts；
   - Intent 提出、认领、执行、失败、恢复记录；
   - Worker stdout/stderr/artifact 摘要；
   - Reason cycle、路线分支、dead-end、suppressed/reopened route；
   - provider/runtime health；
   - usage ledger；
   - provenance/evidence chain；
3. Observer 输出结构化总结，并为每个条目标注 `confirmed / candidate / failed / operator guidance / runtime-only`；
4. 弹窗分为“总结”“已知事实”“尝试过的路线”“失败与运行时问题”“证据引用”；
5. 用户逐条勾选或全部审计通过；
6. 只有点击“写入 Target 记录”后，选中的**已审计内容**才写入 Target durable record；
7. 写入对象是 Target knowledge/history projection，不是直接写 accepted flag；每项保留 source Run、event/artifact/evidence refs、审计者、时间和审计决定；
8. 取消或关闭弹窗不产生 Target mutation。

### 8.2 可写入 Target 的信息

默认允许写入：

- 经用户确认的事实摘要及 provenance 引用；
- 已验证的路线约束、服务特征、有效凭据线索（按现有 secret policy 脱敏）；
- 已确认的 dead-end/路线压制原因；
- Operator-approved Run summary。

默认禁止直接写入：

- 未验证 flag claim；
- 仅来自模型 prose 的事实；
- 未审计的候选 evidence；
- raw secret、API key、session token；
- 把 runtime failure 当成目标业务事实。

---

## 9. Reason 恢复与 Intent 生命周期

### 9.1 中断 Intent 的规则

用户要求的语义：如果认领 Intent 的 Worker 意外中断，没有返回实际尝试和反馈，则 Intent 恢复为“待验证”，再由新 Worker 认领。

建议采用明确状态：

```text
proposed → claimed → running
                     ├─ completed (有实际反馈)
                     ├─ failed_runtime (有 runtime 诊断，无业务反馈)
                     ├─ interrupted_no_feedback → awaiting_validation
                     └─ stopped_by_operator → awaiting_validation
awaiting_validation → Reason 新 cycle 审计 → reopen/redispatch 或 retire
```

判断“没有实际尝试和反馈”的最低条件：

- 有 `intent_claimed`，但没有对应 worker 的 `intent_concluded`；或 conclude 为 runtime failure；
- 没有真实 tool call / terminal output / stdout/stderr artifact；
- 没有 candidate/verified fact、dead-end、PoC 或结构化 worker result；
- claim lease 已过期或 worker lifecycle 为 exited/stalled/oom/cancelled。

若 Worker 已产生真实执行输出，即使没有 flag，也不应简单恢复为待验证；应结论为“已尝试，结果为 dead-end/failed”，由 Reason 决定是否 reopen。

### 9.2 设计实现建议

- SharedGraph 保持 append-only：新增 `intent_interrupted` / `intent_recovery_required` 事件，不覆盖历史 claim/conclude；
- intents projection 增加 `dispatch_state=resume` 或 `awaiting_validation`；
- lease reaper 在发现过期 claim 时发出恢复事件，携带旧 worker、最后 lifecycle、是否有 output 的摘要；
- Reason 下一 cycle 必须先审计这些恢复项，再产生普通新 Intent；
- 新 Worker 认领时使用新 worker id，新 claim event 与旧 claim 链接；禁止旧 Worker 的迟到 conclude 覆盖新 owner；
- 恢复次数有限。连续多次无输出应标记 provider/runtime degraded 或 operator attention，不无限 spawn；
- UI 显示“待验证 / 原 Worker 中断 / 尚无实际反馈 / 已重新派发”，不要显示成普通“失败”。

### 9.3 恢复优先级

Reason 每轮按以下顺序处理：

1. provenance/reviewer 需要的验证任务；
2. `awaiting_validation` 的中断 Intent；
3. operator 提示生成的高优先级 Intent；
4. 新证据驱动的探索 Intent；
5. fallback bootstrap。

这样不会因为 provider/runtime 空转而让新的探索任务掩盖未完成的认领。

---

## 10. Worker 详情可读性改造

当前 Worker lane 信息密度高，但标题、状态、runtime、Reasoning、Tools 和错误原因缺少层级。建议每个 Worker 详情使用四层：

```text
┌ Worker identity                         状态 / role / online
│ intent-7 · 当前任务                      runtime health
├──────────────────────────────────────────────────────────────
│ 关键指标：Verified facts | Tool calls | Tokens | Duration | Cost
├───────────────────────┬──────────────────────────────────────
│ 当前任务              │ Runtime / Provider
│ Intent goal           │ effective provider/model
│ dispatch reason       │ backend / rc / timeout / oom
│ claim lease           │ retry / fallback / next action
├───────────────────────┴──────────────────────────────────────
│ 实际执行摘要
│ stdout/stderr 与 tool output 按时间排列，可展开原文
├──────────────────────────────────────────────────────────────
│ 生命周期：spawned → phase → stalled/exited
│ Intent 结果：completed / dead-end / runtime failure / awaiting validation
└──────────────────────────────────────────────────────────────
```

具体规则：

- 第一屏优先显示：Worker ID、角色、当前 Intent、状态、是否在线、runtime/provider health；
- 将 session id、cwd、argv、raw rc 放进“运行时诊断”折叠区，不与自然语言 reasoning 混排；
- stdout、stderr、tool result 使用等宽字体、行号/时间、复制按钮和折叠；stderr 与 runtime error 使用 amber/red，但不隐藏；
- Reasoning 只显示最后摘要，完整原文进入可展开区；
- 每个状态 chip 同时给出用户语义和机器状态，例如“运行中 · explore”“运行时失败 · rc=1”“待验证 · 无实际输出”；
- Worker 操作按钮只放在 header/action 区：查看 Intent、复制诊断、停止该 Worker、重新请求验证；不挤压正文；
- 长 goal、stderr、tool output 在内容列内部换行，不能撑宽详情面板；
- reviewer Worker 使用单独 role chip；Observer 不作为普通 Worker lane 展示。

---

## 11. 路线分支：居中向下延伸树

### 11.1 目标结构

```text
                         [RUN / Target]
                                ↓
                         [Reason Cycle 1]
                         ↙       ↓       ↘
                 [Intent A]  [Intent B]  [Intent C]
                    ↓           ↓           ↓
                [Worker]     [Worker]     [Reviewer]
                    ↓           ↓           ↓
              [Evidence]   [Dead-end]  [Reopen / next]
                                ↓
                         [Reason Cycle 2]
                         ↙       ↓       ↘
```

### 11.2 视觉与交互

- Run 根节点在内容区域水平居中；
- 主连接线从根节点向下；每个 cycle 是横向分叉层，Intent 从 cycle 下方分叉；
- 使用 CSS grid/flex 或专用 SVG connector，箭头方向始终向下；不要用当前横向 rail 作为主结构；
- 每个节点显示：短标题、状态 chip、时间/耗时、owner；长 goal 进入详情；
- 节点点击打开对应 Worker、Intent、Evidence 或审计详情；
- 失败路线用红/琥珀色旁支但仍保留在树上；压制路线用虚线；重开路线用回环箭头或“reopened from”链接；
- 屏幕窄时保持纵向树，Intent 子节点横向滚动仅限单层，不让整个页面横向溢出；
- 仍保留表格/列表作为“审计列表”辅助视图，树是默认视图；
- 没有真实 lineage 时显示 `legacy / lineage unavailable`，不能按 UI 顺序推断不存在的父子关系。

### 11.3 数据要求

路线树不能只靠数组顺序。每条边至少需要：

```json
{
  "from": "reason-2|intent-I1|worker-1|fact:42",
  "to": "intent-I1|worker-1|fact:42|dead-end:7",
  "kind": "planned|claimed|produced|failed|reopened|suppressed",
  "source_event_seq": 123
}
```

现有 `reason_cycle_id`、`intent_id`、`worker_id`、`intent_products`、`route_hash`、`branch_id` 和 event seq 可以组成第一版；缺失边必须标注 unknown，而不是猜测。

---

## 12. Observer：询问进展与实时汇报

### 12.1 替换当前 Ask Reason

当前 BTW 已经符合只读旁路的大部分边界，产品命名和数据包需要升级：

- “询问 Reason” → “询问进展”；
- 角色名称 → `Observer / 观察员`；
- Observer 读取：facts、Intents、活动日志、路线分支、账本、证据链、Worker/runtime/provider health、Target 已确认记录；
- Observer 不能执行 shell、不能 spawn/pause/redirect/stop、不能写 graph/Target、不能产生 flag；
- “汇报实时进展”使用同一 Observer snapshot，按当前 event seq 增量刷新；
- Observer 失败显示观察通道失败，不改变 Run 状态。

### 12.2 Observer 配置

Observer 使用设置中的独立配置，默认选择低成本、低延迟模型；新建 Run 不允许覆盖。它可以有自己的 fallback：

```text
Observer default → lightweight configured fallback → unavailable
```

Observer 回答必须引用 snapshot 的来源范围和 event seq/事实 id；回答“当前正在做什么”时区分：计划、已认领、真实尝试、已产出、失败、待验证。

**归因契约：** 每次 Observer 回答都带机器可读归因，前端据此显示标签：

```text
observer_source = default | fallback | unavailable
fallback_used   = true 仅当配置的轻量 Observer fallback 实际作答
```

- `default`：主 Observer 作答；
- `fallback`：主 Observer 失败后，**仅** 配置的 `observer_fallback` 作答（同时显示“configured lightweight fallback”）；
- `unavailable`：主 Observer 与配置 fallback 均不可用，**以及**客户端侧任何失败（HTTP 非 2xx、流异常、空流）——统一显示“观察员暂时不可用”，绝不显示为成功回答；
- 任何情况下都不会回退到 Titler 或 Reason；Observer 失败不改变 Run 状态。

### 12.3 实时进展卡片

Run 选中后可在侧栏看到 Observer summary：

- 当前 Reason 状态/最近 cycle；
- active Intent 与 Worker；
- 最近真实执行输出；
- 当前 provider/runtime health；
- 已验证事实、candidate、证据缺口；
- 下一步由谁决定；
- 需要操作员处理的项。

这是摘要，不替代完整 Timeline/Worker/Routes/Evidence 页面。

---

## 13. 设置页重构：Reason、Observer、Reviewer、Titler

设置左侧或角色区改为四张配置卡：

### Reason

- planner model/provider/account；
- retry/backoff/circuit threshold；
- settings default fallback；
- probe status；
- 说明：负责规划、判断 Worker/provider 问题和恢复，不直接执行 target 操作。

### Observer

- cheap read-only model/provider/account；
- max transcript/context；
- answer timeout；
- fallback model；
- 说明：只读汇报，不影响调度、证据和 flag。

### Reviewer

- reviewer model/provider/account；
- review Worker profile；
- max concurrency；
- candidate spike / fruitless worker / cooldown policy；
- evidence review strictness：必须引用真实 stdout/stderr/tool/artifact，禁止仅凭 Worker prose 放行；
- intent quality gate：重复路线检测、已验证方向冲突、低价值改写、优先级和 token 预算评估；
- revalidation/retest policy：何时要求独立 verifier，何时拒绝或延后 Intent；
- 说明：只负责复核、重验证、Intent 质量闸门和路线审计；Reviewer 不可用时不得绕过它直接把 Worker 信息交给 Reason。

### Titler

- model/provider/account；
- timeout；
- 说明：只生成标题/摘要，失败不影响 Run。

顶部统一文案：

> 以上是所有新 Run 的默认角色配置。Run 可以临时覆盖 Reason、Worker、Reviewer，但 Titler 和 Observer 使用这里的设置。任何运行时切换都会在 Run 绑定与事件中留下记录。

兼容迁移：

- `planner` 迁移为产品名 `reason`，保留读取别名；
- `titler` 保留但从 Run wizard 移除；
- `review_policy` 中的 provider/model 迁移为 Reviewer role binding；
- 旧 Run 没有 binding snapshot 时显示 legacy，不补造历史配置。

---

## 14. 首要修复顺序

### P0：先让新 Run 真实执行可诊断

1. 验证新 Run 首次 `build_execute` 不带 ghost session；
2. 修复/验证 Worker root、Pi session dir、RCP supervisor HOME 映射一致性；
3. 让首次 Worker 的真实 stdout/stderr、runtime status、worker lifecycle 和 Intent result 全部进入事件链；
4. 运行时失败必须清晰显示为 runtime failure；
5. 用新的 Bugku-like Run 验证真实 target 访问和 flag provenance；不重写 `run-6508`。

### P1：配置继承与角色拆分

1. `llm_mode=settings_default` 默认路径；
2. Run override 显式开关和非 secret snapshot；
3. Titler 从 Run wizard 删除；
4. Observer 配置加入设置并替换 Ask Reason 命名；
5. binding inspector 展示 configured/effective/fallback。

### P2：运行控制与生命周期

1. selected Run 才渲染 command bar；
2. 四类指令和多选对象；
3. Intent interrupted/no-feedback → awaiting_validation → Reason recovery；
4. 回顾弹窗和 Target audit write；
5. provider fallback 状态机和阈值配置。

### P3：可读性与路线视觉

1. Worker detail 信息层级和 raw output 折叠；
2. 居中向下路线树与箭头；
3. Observer 实时进展卡片；
4. 移动端单列、无横向溢出。

---

## 15. 测试与验收标准

### 15.1 新 Run 成功路径

- 创建 Run 不填写 LLM override 也能启动；
- Run binding 显示来源为 settings default；
- Worker 首轮执行真实 target，不读取 challenge source/solution；
- 真实 stdout/stderr、tool output、worker lifecycle、runtime status 和 Intent conclude 可回放；
- flag 只有在真实执行输出通过 provenance gate 后才 accepted；
- `run-6508` 历史 JSONL、workspace、arts 和 graph 不被修改。

### 15.2 LLM 继承/覆盖/fallback

- 默认 Run 的 Reason/Worker/Reviewer/Observer/Titler 均从设置解析；
- 显式 Run override 只替换选择的角色；未覆盖角色继续使用 settings default；
- Titler 不出现在 Run override 表单；
- Worker 报告先进入 pending review；未经 Reviewer 审阅的 prose、candidate 和 flag claim 不得进入 Reason 的业务上下文；
- Reviewer 只有在真实输出/证据支持时才发布 verified；verified 仍不替代硬编码 provenance gate；
- Reason 提出的每个 Intent 必须经过 Reviewer 的 approved/revise/duplicate/blocked/defer 结论后才能 dispatch；
- Reviewer 必须识别与已验证方向、事实、已完成/进行中 Intent 的重复，低价值重复必须阻断或改成 verifier/retest；
- override 一次 timeout 不切换；连续达到阈值后切换到 settings default；
- quota/auth/model-not-found 首次可暂停 override 并探测 default；
- default 也失败时 Run health 为 blocked/degraded，反馈明确，不伪装成普通解题失败；
- fallback 切换事件包含 role、from、to、reason、attempt/window，且不包含 secret；
- Reason 不可用时由宿主有限重试/熔断和 probe 判断，不依赖 Reason 自己回答。

### 15.3 指令栏

- 未选 Run 时不存在主 UI 指令栏；
- 选择 Run 后只显示提示、停止、恢复、回顾；
- 对象可选广播、Reason、多个 worker；单选可单播，多选可组播；
- 提示进入 operator guidance，不直接成为 evidence；
- 恢复由 Reason 接续，Reason 检查 Intent/Worker 后再恢复或新建 Worker；
- 回顾只对已终止 Run 开启，并在人工审计确认后才能写 Target。

### 15.4 中断 Intent

- Worker claim 后异常退出、无真实输出且无有效 conclude 时，Intent 显示 awaiting_validation；
- Reason 下一 cycle 优先审计并重新派发；新 Worker 使用新 claim；
- 有真实尝试但无 flag 的 Worker 不被误判为空中断；
- 旧 Worker 迟到 conclude 不得覆盖新 owner；
- 无限无输出失败达到上限后进入 operator attention，而非无限 spawn。

### 15.5 UI 可读性

- Worker 详情第一屏可读出 identity、Intent、status、runtime/provider health；
- raw stdout/stderr 可展开、复制、换行，无横向撑宽；
- 路线默认是居中根节点、向下箭头、分叉树；失败/压制/重开关系可审计；
- Observer 回答显示为观察员并引用事实/事件；
- 设置明确说明“默认配置”；新建 Run 明确显示 default/override 状态；
- 桌面和 390px 移动视口均无时间列、按钮、goal、stderr 横向溢出。

后端：

- provider error classifier/aggregator；
- Reason planner retry/circuit/fallback；
- Reviewer evidence verification：Worker 报告与真实 stdout/stderr/tool/artifact 的一致性、反例和 flag claim 审计；
- Reviewer Intent quality gate：重复路线/已验证方向冲突/低价值 Intent 阻断或改写；
- reviewed context 隔离：未审阅业务信息不得进入 Reason；Reviewer 故障不得自动降级为信任 Worker；
- Worker no-output interruption/reopen；
- intent lease expiration and owner fencing；
- default/override binding snapshot；
- Target review audit write；
- existing provenance, append-only, flag completion and manifest tests。

前端：

- Run wizard default/override conditional rendering；
- settings role cards and wording；
- Reviewer 状态、待审阅报告、Intent review decision 和阻断原因可读；
- selected-run command bar target multi-select；
- Observer label/context/realtime summary；
- Worker detail layout；
- route tree lineage and fallback legacy display；
- desktop/mobile visual smoke；
- full Vitest + TypeScript。

---

## 16. 待评审决策

本方案建议直接采用以下默认选择：

1. **默认继承设置配置**，Run override 采用显式开关；
2. **fallback 角色级隔离**，Worker 的业务 dead-end 不触发 LLM fallback；
3. **Reason 自身由宿主探针/有限重试/熔断判断**，Reason 只负责 Worker 的恢复决策；
4. **60 秒窗口、3 次失败或 50% Worker 受影响**作为 retryable fallback 的默认阈值；fatal quota/auth/model 错误立即暂停并探测默认；
5. **Observer 只读、独立配置、设置默认、无 Run override**；
6. **Titler 只保留设置配置**，从 Run wizard 删除；
7. **command bar 只属于 selected Run**，只保留提示/停止/恢复/回顾；
8. **回顾内容必须人工审计后才能进入 Target**；
9. **Intent 认领无实际反馈时进入 awaiting_validation**，由 Reason 优先恢复，而不是静默标记完成或无限重试；
10. **路线树默认居中向下**，列表作为辅助审计视图。

这些选择保留了当前系统最重要的边界：Target 是长期记录，Run 是独立尝试，Reason 是决策者，Worker 是真实执行者，Observer 是只读解释器，Reviewer 是审计者，Titler 是展示辅助；任何模型输出都不能替代真实执行 provenance。

Reviewer 质量原则：Worker 可以提供线索，但不能自行证明；Reason 可以提出方向，但不能自行决定重复工作值得执行；Reviewer 必须同时对证据真实性和 Intent 质量负责，最终把“可信且值得做的内容”发布给 Reason/Worker 调度链。
