# 协作状态与共享图

## 目标

定义共享图的 schema、事件类型、状态机和 Reason 调度协议，确保多 worker 之间可协作、可审计、可回放。

## 数据模型

### tasks

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  parent_task_id TEXT,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  description TEXT,
  target TEXT,
  flag_format TEXT,
  model_profile TEXT,
  model_id TEXT,
  status TEXT NOT NULL,
  image TEXT,
  runtime TEXT,
  container_id TEXT,
  workspace TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### task_events

```sql
CREATE TABLE task_events (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(task_id, sequence)
);
```

### graph_events

共享图是 append-only 事件源：

```sql
CREATE TABLE graph_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

### graph_facts / graph_intents / graph_routes

从 `graph_events` 派生，不直接覆盖：

```sql
CREATE VIEW graph_facts AS
SELECT
  e.seq,
  e.run_id,
  json_extract(e.payload, '$.fact') AS fact,
  json_extract(e.payload, '$.verified') AS verified,
  json_extract(e.payload, '$.confidence') AS confidence,
  json_extract(e.payload, '$.source') AS source,
  e.actor,
  e.created_at
FROM graph_events e
WHERE e.kind = 'fact_added';

CREATE VIEW graph_open_intents AS
SELECT
  json_extract(e.payload, '$.intent_id') AS intent_id,
  json_extract(e.payload, '$.goal') AS goal,
  json_extract(e.payload, '$.worker_class') AS worker_class,
  json_extract(e.payload, '$.status') AS status,
  json_extract(e.payload, '$.worker') AS worker
FROM graph_events e
WHERE e.kind IN ('intent_proposed', 'intent_claimed', 'intent_concluded');
```

## 事件类型

### 事实

| 事件 | 含义 |
|---|---|
| `fact_added` | 新增事实，verified 或 candidate |
| `fact_challenged` | review 质疑事实 |
| `fact_revalidated` | 重新验证成功 |
| `fact_rejected` | 事实被拒绝 |
| `fact_merged` | 合并重复事实 |
| `fact_superseded` | 被新事实取代 |

### 方向

| 事件 | 含义 |
|---|---|
| `intent_proposed` | Reason 提出新方向 |
| `intent_claimed` | worker 原子认领 |
| `intent_concluded` | 完成 / 失败 / dead-end |
| `dead_end` | 方向已排除 |

### Flag

| 事件 | 含义 |
|---|---|
| `flag_found` | flag 过 provenance gate |
| `flag_invalidated` | 多 flag 场景撤回假阳性 |

### 协作控制

| 事件 | 含义 |
|---|---|
| `poc_saved` | 保存可复用 PoC |
| `poc_claimed` | worker 认领 PoC |
| `poc_concluded` | PoC 使用结束 |
| `review_finding` | review 发现 |
| `route_suppressed` | 路由被压制 |
| `route_reopened` | 路由因新证据重开 |
| `branch_split` | 竞争假设分叉 |
| `resource_locked` | 独占资源锁 |
| `resource_released` | 释放资源锁 |
| `operator_directive` | 操作员指令 |

## Intent 状态机

```text
proposed -> active -> claimed -> concluded
                          |
                          +-> lease expired -> active again
```

### claim 语义

- `claim_intent` 必须是原子操作。
- 只允许 claim 一个 active intent。
- lease 过期后重新可 claim。
- worker 结束时会释放 claim。

```sql
UPDATE intents
SET status = 'claimed',
    worker = :worker,
    claimed_at = :now,
    lease_expires_at = :lease_expires_at
WHERE intent_id = :intent_id
  AND status = 'active'
  AND dispatch_state = 'active';
```

如果 `changes() = 0`，返回 LOST。

## Reason Prompt

Reason 输入：

```text
Goal:
<flag format or engagement goal>

Shared graph:
<verified facts>
<candidate facts>
<open intents>
<attempted intents>
<dead-ends>
<flags>
<directives>

Fact retention index:
<pinned facts>
```

Reason 输出：

```json
{
  "verdict": "explore",
  "goal_met": false,
  "complete_why": "",
  "drift": "",
  "intents": [
    {
      "id": "I1",
      "goal": "Probe JWT alg confusion on /login",
      "worker_class": "explore",
      "category": "web",
      "from": [3, 7],
      "route_hash": "web:login:jwt",
      "lane_key": "",
      "risk_class": "",
      "dup_of": "",
      "rationale": "Verified token claims indicate HS256/RS256 confusion."
    }
  ],
  "pinned_facts": [3, 7],
  "audit": ["Candidate fact #5 must be verified before use."]
}
```

## Reason 触发条件

不要每轮都调用 Reason，只有 graph 变化时才触发：

- 新增 verified fact。
- 新增 candidate fact。
- 新增 dead-end。
- flag 被 invalidated。
- review finding 产生。
- operator directive 写入。

## Scheduler 主循环

```text
for {
  if flags complete:
    finalize

  if graph seq changed:
    reason_result = Reason(graph_summary)

  for intent in dispatchable_open_intents:
    if capacity available:
      worker = pick_worker(intent.category, healthy_engines)
      spawn Pi worker
      worker claims intent

  reap finished workers
  drain operator commands
  drain resource locks
  drain review proposals

  sleep 2s
}
```

## Worker 角色

| 角色 | 行为 |
|---|---|
| race | 多个引擎并行打整题，抢简单题 |
| bootstrap | 单 worker 整题冲一次 |
| explore | 只执行一个 intent |
| review | 审计事实、压制重复路线 |
| coordinator | 可选的父 Agent，负责最终整合 |

## Provenance Gate

Gate 输入：

```text
flag
flag_format
raw_output
artifacts
```

Gate 规则：

1. 格式必须匹配。
2. 不能是占位符。
3. flag 必须逐字出现在 raw_output 或 artifact 中。
4. 只出现在 `FOUND_FLAG=` marker 或 final-result 中但无真实输出的，不接受。

Gate 输出：

```text
verified flag
candidate flag
rejected flag
```

## Review 机制

Review worker 不执行 exploit，只做：

- 检查 candidate fact 是否被错误地当 verified。
- 检查 worker 是否重复同一 route。
- 检查 fact 是否被挑战。
- 对重复 intent 进行压制。
- 对冲突假设拆分 branch。

Review 结果写回 graph，Reason 下一次读取时会遵守。

## 多 Flag

任务可设置：

```json
{
  "expected_flags": 3
}
```

只有当 `flags_complete()` 为 true 时才 finalize。

## HITL

操作员指令写入 graph：

```json
{
  "kind": "operator_directive",
  "payload": {
    "directive": "Focus on RCE path, not further port enumeration.",
    "priority": 1
  }
}
```

worker 启动时必须读取 directives。
