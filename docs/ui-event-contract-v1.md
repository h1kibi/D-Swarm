# UI 事件契约 v1

> 状态：冻结（2026-08-28）  
> 适用范围：`apps/web/ui/` 对 D-Swarm 事件流的消费与 session replay。  
> 原则：前端是 dumb subscriber；事件只提供观测输入，不能成为 flag 来源，也不能绕过
> `dswarm/solver/gate.py` 的 provenance gate。

## 1. Event envelope

每条 SSE/回放事件使用如下 envelope：

| 字段 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `event_type` | string | 是 | 顶层事件类型，例如 `blackboard.delta`。必须属于 Python `EventType` 的公共词汇。 |
| `seq` | integer | 是 | 事件流中的单调序号；回放时按事件流顺序消费。 |
| `ts` | number | 是 | Unix 时间戳（秒）。 |
| `run_id` | string | 是 | 所属运行实例。 |
| `challenge_id` | string \| null | 否 | 挑战标识。 |
| `solver_id` | string \| null | 否 | 产生该事件的 solver/worker 标识。 |
| `payload` | object | 是 | 事件类型对应的 payload。未知额外字段允许存在。 |

消费者必须忽略自己不认识的 envelope/payload 字段，不能根据未声明字段推断 flag、
验证结果或调度状态。

## 2. `blackboard.delta` 最低契约

`event_type == "blackboard.delta"` 时：

- `payload.kind` 是**非空字符串**；推荐使用小写字母、数字和下划线（`[a-z0-9_]+`）。
- `payload.actor` 是字符串；没有 actor 的系统汇总事件可以省略它。
- 其余字段由 kind 的定义决定；新增字段向后兼容，消费者不得依赖契约外字段。
- 事件仍然是 append-only event stream 的一部分。生产者不能通过修改旧事件来“升级”
  已发出的 kind。

### 未知 kind 的处理策略

UI 收到未知、缺失或非字符串 kind 时必须：

1. 保留原始事件流，并在 blackboard timeline 增加一条 generic activity；
2. 不修改 typed intents/facts/POCs/flags、worker 调度视图或证据图；
3. 不抛异常、不阻塞后续事件、不影响 replay；
4. 不把未知事件内容当作 flag、验证或 provenance 证据。

当前 reducer 的显示格式为 `unrecognized blackboard event: <kind>`；缺失值显示
`(missing kind)`。generic timeline 只用于可见性，不代表 UI 已理解该事件的语义。

## 3. Kind 分类

分类描述的是**UI 消费契约**，不是 solver 调度模式。特别是出现
`coordinator_directive` 或历史 `race_*` 文本，不表示恢复 Coordinator/Race 运行模式。

### Stable：已有 typed reducer

当前 UI 有明确 fold 分支的 kind：

`metrics_summary`、`intent_proposed`、`intent_claimed`、`intent_concluded`、
`fact_added`、`dead_end`、`poc_saved`、`poc_claimed`、`poc_concluded`、
`runtime_degraded`、`worker_backend_degraded`、`engine_degraded`、
`phase_transition`、`worker_budget_exhausted`、`cost_budget_exhausted`、
`intent_reopened`、`fact_rejected`、`fact_superseded`、`fact_merged`、
`intent_state_changed`、`operator_directive_changed`、`hitl_classified`、
`resource_lock_changed`、`flag_invalidated`、`flag_found`、`flag_unverified`、
`flag_audit`、`review_proposal`、`need_input`、`awaiting_operator`、
`operator_paused`、`operator_resumed`、`worker_spawned`、`review_started`、
`review_finished`、`review_finding`、`fact_challenged`、`fact_revalidated`、
`route_suppressed`、`route_reopened`、`branch_split`、`branch_resolved`、
`provider_recovery_directive`、`worker_recovery_scheduled`、
`provider_dispatch_paused`、`worker_recovery_exhausted`、`provider_batch_alert`、
`coordinator_directive`、`worker_killed`、`worker_spawn_rejected`、
`worker_finished`、`goal_complete`、`budget_exhausted`、`reason_start`、`reason_done`。

`finding_upserted` 由 pheromone finding fold 消费；`recon_*`、`reason_cycle_*`、
`intent_skipped`、`dispatch_decision`、`fallback_dispatch`、`intent_completed` 和
`intent_failed` 由 reason-loop fold 消费。

`coordinator_directive` 在这里仅表示一个已知的事件形状；它不注册、不启动、也不
兼容 Coordinator 调度器。

### Experimental：允许发射，但暂时只进 generic timeline

以下 kind 在内核或运维路径中可能出现，但没有稳定的 typed UI 投影。它们可以继续
作为诊断事件发射；在契约升级前，UI 只做 generic timeline 展示：

| kind | 约定 payload 字段 |
|---|---|
| `flag_reaccept_blocked` | `flag`、`reason`；可选 `intent_id` |
| `ready_to_submit` | `note` |
| `claim_solved_rejected` | `intent_id`、`reason`；可选 `flag` |
| `worker_runtime_error` | `worker`、`reason` 或 `category` |
| `lane_locked` / `lane_released` / `lane_revived` | `lane`、`owner`、`status` 等 lane 字段 |
| `review_proposal_decision` | `proposal`、`decision`、`reason` |
| `system_notice` | `message`、`code` |
| `direction_override` | `direction`、`source`、`reason` |
| `help_dismissed` | help id、`action` |
| `worker_health_check` | `worker`、`status`、`detail` |

字段名以生产者实际 payload 为准；上表是最低语义约束，不授权消费者读取其他未声明
字段。`reason_planner_unavailable` 等未列入 stable 的 kind 同样遵循该 generic fallback
规则，直到它们获得明确的 typed UI 分支；`finding_upserted` 与 reason-loop kinds
虽然不一定有主 reducer 分支，但已由专用 typed fold 消费，见 stable 列表。

### Retired / replay-only

`race_started`、`race_concluded` 仅为历史 session replay 保留。它们可被旧事件流
渲染为 legacy generic activity，但新生产路径不得发射，不能恢复 Race 模式或建立
兼容注册表。历史 `coordinator` 文本同理只作为事故/回放上下文处理。

## 4. 变更规则

新增 blackboard kind 必须按以下顺序完成：

1. 先更新本文档，明确 stable/experimental/replay-only 分类与 payload 最低字段；
2. 再实现 reducer 分支（若需要 typed state）；
3. 增加 deterministic Vitest replay/contract test；
4. 由事件生产者和 UI 一起验证后再发布。

没有 typed UI 分支的新 kind 不得静默丢弃，也不得通过 reducer 默认分支改变 typed state。
