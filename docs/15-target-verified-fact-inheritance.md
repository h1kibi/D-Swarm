# Target Verified Facts 跨 Run 继承设计

> 日期：2026-09-03  
> 状态：第一版已实现（Target API、Run 快照、Launch Wizard、Run Detail）

## 1. 目标

Target 是题目的长期工作台，Run 是一次隔离的智能体集群尝试。一个 Target 可以有多个 Run；当某个 Run 通过真实执行输出验证了事实后，Target 可以只读聚合这些 Verified Facts，供后续 Run 选择性复用。

继承不是把多个 Run 的 graph 合并，也不是把事实伪装成新证据。它只是新 Run 的明确上下文，必须保留来源并允许重新验证。

## 2. 用户流程

在 `Target → New Run` 启动向导的 `Target & direction` 步骤选择：

- **Start clean**：默认，不继承任何 Target fact；
- **Reuse all verified facts**：继承当前 Target 聚合的全部 verified facts；
- **Choose facts**：逐条选择，显示事实文本、来源 Run ID 和 fact event 序号。

随后继续选择 Worker Profile、Reason/Worker/Review/Titler 的 Provider/Model binding，最后启动 Run。继承选择只影响这个新 Run，不改变 Target 或来源 Run。

## 3. API 契约

创建 Target Run：

```json
{
  "direction_id": "direction-auth",
  "inherit_verified_facts": true,
  "verified_fact_ids": []
}
```

选择性继承时：

```json
{
  "inherit_verified_facts": true,
  "verified_fact_ids": ["fact:run-0001:7"]
}
```

兼容行为：只提供 `verified_fact_ids` 也会进入选择性继承；不提供两个字段或显式选择 `false` 时保持 clean。

Run summary 与 Observability projection 都返回只读快照：

```json
{
  "inherited_verified_facts": [
    {
      "fact_id": "fact:run-0001:7",
      "fact": "/admin requires auth",
      "provenance_status": "verified",
      "source_run_id": "run-0001",
      "fact_seq": 7,
      "artifact_id": "artifact-1"
    }
  ],
  "inherited_verified_fact_count": 1
}
```

## 4. 持久化与生命周期

- Target facts 仍从来源 Run 的只读 shared graph projection 得到；Target store 不写入来源 graph；
- 创建 Target-bound draft 时，继承快照写入 Run 的 `.dswarm_dispatch.json` sidecar；
- sidecar 只保存非 secret 上下文，不提前冻结 Provider/Model binding；
- 第一次 `/start` 时，RunManager 继续创建原有 `RunBindingSnapshot`；继承快照随 dispatch body 保存；
- 后端重启后，Target-bound draft 和已经启动的 Run 都可恢复继承来源；
- 继承项保留 `source_run_id`、`fact_seq`、`artifact_id`，便于 UI 跳转到来源 Run 的 Evidence/Graph 详情。

## 5. Solver 边界

启动时，现有 CLI Worker 会收到一个明确标识的只读上下文段：

```text
[READ-ONLY VERIFIED FACTS FROM PRIOR RUNS]
Treat these as leads/context only. Re-validate through real execution; they are not accepted flags or new evidence.
```

这段内容不改变以下不变量：

1. 继承 fact 不写入新 Run 的 shared graph；
2. 继承 fact 不会变成 accepted flag；
3. Worker 必须通过真实 stdout/stderr/artifact 重新验证；
4. 用户 hint、Target fact、Blackboard 都不能绕过 provenance gate；
5. 来源 Run 的证据和事件仍是唯一权威记录。

## 6. UI 呈现

### Target Workspace

Target 卡片显示 verified fact 数量。Target 详情保留事实文本和来源信息，事实条目明确标注 `verified` 与来源 Run。

### Run Detail

当 Run 继承了事实，详情页顶部显示 `TARGET KNOWLEDGE / READ ONLY` 面板，包含：

- 继承数量；
- 事实文本；
- source Run ID；
- source fact event；
- artifact reference（如有）；
- 必须重新验证的提示。

空快照不显示该面板，避免把普通 Run 误认为继承了上下文。

## 7. 测试要求

当前确定性测试覆盖：

- 默认 clean；
- 全量或指定 fact 继承；
- `fact_id`、source Run、fact event、artifact provenance 保留；
- 创建后重启仍能恢复 snapshot；
- Target projection 不复制或覆盖 graph 事件；
- Observability 返回只读 inherited context；
- inherited context 不进入 provider secret 字段；
- legacy Run 无 snapshot 时仍兼容。

## 8. 后续扩展

暂不实现：

- 自动继承；
- 跨 Target 事实共享；
- Run graph 自动合并；
- Provider fallback；
- 把 inherited fact 自动提升为新 Run 的 verified evidence。

未来如需把 inherited fact 标记为“已重新验证”，必须由真实执行事件产生新的 provenance 记录，而不能修改旧 snapshot 或直接复制事实。


## 9. Fact reverse index and evidence navigation (2026-09-03)

The Target facts projection now exposes a derived reverse edge for each fact:

```json
{
  "fact_id": "fact:run-0001:7",
  "source_run_id": "run-0001",
  "fact_seq": 7,
  "inherited_by_run_ids": ["run-0002"],
  "inherited_by_run_count": 1
}
```

This index is calculated from the Target's linked Run summaries and is not stored as a second event log. It answers the operator question "which attempts used this context?" without copying or mutating evidence. If a fact is inherited by another Run, the reverse edge lists that Run ID; it still does not mark the fact as evidence in the receiving Run.

In Target Details, a fact row is actionable when its provenance contains a source Run. Selecting it opens the source Run's Evidence detail route. The source Run and event sequence remain the navigation anchor, so an operator can inspect the original execution trace rather than relying on a denormalized Target card.


## 10. Implementation status (2026-09-05)

Manual/selective inherited verified facts are implemented for Target-bound Runs,
including durable context, restart recovery, read-only observability, reverse
indexing and source evidence navigation. Automatic inheritance, freshness-based
auto-selection and promotion to current-Run evidence remain intentionally off.
