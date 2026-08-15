# M3 严格事件行不可变 — 事件协议 RFC v3（sticky 生命周期 + challenge 绑定 + 完整有效模型）

> 状态：**RFC v3 已按批准计划实施（2026-08-14）**。v1 经 [docs/12](12-m3-event-immutability-rfc-review.md)
> 判 Conditional No-Go；v2 经 [docs/13](13-m3-event-immutability-rfc-v2-review.md) 第三轮复评
> 判 Conditional No-Go。本版逐条修订并作为当前实现的事件协议基线，附 **13 场景真实 schema
> 执行证据**。实现遵守 provenance gate 不变、events 只追加、派生投影可删除重建。

## Implementation status（2026-08-14）

已落地：

- `dswarm/swarm/fact_events.py`：`fact_effective` VIEW、immutable triggers、transition
  guards、唯一性约束、migration preflight、version check 和 backup API；
- `dswarm/swarm/shared_graph.py`：canonical promotion/summary/lifecycle event 写路径，
  effective projection 读取链，显式迁移；旧 materialized lifecycle 表保留为兼容 schema，
  但不再由 M3 写入或读取；
- `dswarm/swarm/projection.py`、`board.py`、`postgres_board.py`：projection identity、
  replacement/幂等、partial replay 和失败 cursor 语义；
- `skills/dswarm-blackboard/blackboard.py`、`review_flow.py`：challenge scope 与有效事实
  读取链统一，禁止 raw genesis `verified` 作为 lifecycle fallback。

本次实施补充了 side-table 删除后 projection 等价、promotion 无 prior 时不伪造
`supersedes_source_seq`、projection 失败 cursor 保持和 effective-only candidate fallback
回归测试。真实 Postgres 集成测试尚未执行；其余专项与全量测试在最终验证阶段复跑。

---

## 0. v2 → v3 变更摘要（docs/13 逐条）

| docs/13 条目 | v2 问题 | v3 修订 |
|---|---|---|
| 阻断 A：terminal 被复活 | 只取最新 lifecycle 事件；`reject→revalidate` 等恢复 verified | §3 **sticky retired**：`retired = EXISTS(任意 terminal 事件)`，与 `_upsert_fact_state` 的 `retired_seq=COALESCE` 语义逐点对齐；场景 J/K/L 验证 |
| 阻断 B：challenge_id 未绑定 | 子查询只按 fact_seq 分组/JOIN | §2 全部 transition 子查询 `GROUP BY challenge_id, fact_seq`、JOIN 双键绑定；写路径 guard + `BEFORE INSERT` trigger 兜底；场景 M 验证 |
| 阻断 C：VIEW 列不足 | 缺 fact 文本/actor/ts/route/finding/promotion 身份 | §2 扩展为完整 typed effective model（26 列），§9 消费者映射逐列 |
| 阻断 D：user_version 无法约束旧二进制 | forward-only 承诺不可实现 | §6 改为**两阶段发布**（phase1 只加 guard 不 bump；phase2 再 bump）+ backup-only 如实表述 |
| Board `replace_by_source` 未定稿 | 契约空 | §4 完整契约（source_seq 语义/幂等/原子性/缺失行为/游标/delta 形态） |
| summary 恢复路径违背前端边界 | UI→graph 旁路 | §5 UI 保持纯 bus subscriber（SessionStore 重放已满足）；canonical summary 仅 BTW/审计 |
| malformed JSON 击穿 VIEW | 无防护 | §7 写路径 `json_valid` + key 类型校验 + 触发器兜底 |
| summary 不同值重试未定义 | — | §8 **first-write-wins**：同值 True、异值 False（不覆盖，审计 delta） |

---

## 1. 事件协议（v2 基础上修订）

### 1.1 `fact_verified` / `fact_summarized`（不变，见 v2 §1.2/§1.3）

envelope 一律 `verified=0`、`confidence=1.0`；verdict/provenance 只在 typed payload 与
effective fold；`fact_verified` 保存触发写入的 `artifact_id` + payload
`{fact_seq, confidence, witness, verifier, source}`，actor = 触发者；
`dedupe_key = fact_verified::{fact_seq}` / `fact_summarized::{fact_seq}`。

### 1.2 写路径 guard（docs/13 阻断 B/§3 强化）

追加任一 transition 前（`shared_graph` Python 层）校验，任一失败即拒绝且不产生事件：

1. `payload` 是合法 JSON 且 `fact_seq` 是整数（`json_valid` + 类型检查，docs/13 malformed JSON）；
2. 目标 `fact_seq` 存在、`kind='fact_added'`、`challenge_id` == 当前 challenge；
3. `fact_verified`：目标尚无 promotion（dedupe 已保证，guard 给出明确错误）；
4. `fact_summarized`：summary 非空且 ≤ 现有长度限制；**first-write-wins**（§8）。

DB 层兜底（blackboard skill 等直连方）：

```sql
CREATE TRIGGER IF NOT EXISTS transition_json_guard
BEFORE INSERT ON events
WHEN NEW.kind IN ('fact_verified','fact_summarized')
     AND (json_valid(NEW.payload) = 0
          OR json_type(NEW.payload,'$.fact_seq') NOT IN ('integer'))
BEGIN SELECT RAISE(ABORT, 'transition payload must be valid JSON with integer fact_seq'); END;
```

（跨 challenge 的目标存在性无法在 trigger 内便宜表达，由 Python guard 承担主责；
trigger 只兜 JSON 形状。）

---

## 2. `fact_effective` VIEW v3（可执行，13 场景验证）

### 2.1 语义规则（与生产 `fact_states`/`_fact_state_map` 逐点对齐）

```
base_verified   = fact_added.verified OR EXISTS(fact_verified)          # lifecycle 前 base
base_confidence = COALESCE(最新 fact_verified.confidence, fact_added.confidence)
state           = 最新 lifecycle 事件 kind 的映射                         # 与 fact_states.state 相同
                  （challenged/revalidated/rejected/merged/superseded；无 → base 语义 verified/candidate）
retired         = EXISTS(任意 terminal 事件)  ← STICKY，等价 retired_seq COALESCE
verified        = retired ? 0 : (state==challenged ? 0 : base_verified)
confidence      = retired ? 0.0 : (state==challenged ? 0.4 : base_confidence)
```

关键点：`reject/supersede/merge → revalidate` 时 `state` 为 `revalidated`（与
`fact_states` 一致），但 `retired=1` 且 `verified=0`——消费方按生产语义先看 `retired`，
terminal 事实永不复活（场景 J/K/L 锁定）。

### 2.2 最终 SQL（本 RFC 规范实现，已执行；完整 26 列）

```sql
CREATE VIEW IF NOT EXISTS fact_effective AS
SELECT
    f.seq                                  AS fact_seq,
    f.challenge_id                         AS challenge_id,
    json_extract(f.payload,'$.fact')       AS fact_text,
    json_extract(f.payload,'$.source')     AS fact_source,
    f.actor                                AS fact_actor,
    f.ts                                   AS fact_ts,
    f.verified                             AS base_verified,
    f.confidence                           AS base_confidence,
    json_extract(f.payload,'$.route_hash') AS route_hash,
    json_extract(f.payload,'$.finding.kind')   AS finding_kind,
    json_extract(f.payload,'$.finding.target') AS finding_target,
    json_extract(f.payload,'$.finding.data')   AS finding_data,
    p.seq                                  AS promotion_seq,
    pv.actor                               AS promotion_actor,
    pv.artifact_id                         AS promotion_artifact_id,
    COALESCE(pv.artifact_id, f.artifact_id)              AS artifact_id,
    COALESCE(json_extract(pv.payload,'$.witness'), json_extract(f.payload,'$.witness'))  AS witness,
    COALESCE(json_extract(pv.payload,'$.verifier'), json_extract(f.payload,'$.verifier')) AS verifier,
    COALESCE(json_extract(pv.payload,'$.source'),  json_extract(f.payload,'$.source'))    AS source,
    s.seq                                  AS summary_seq,
    json_extract(s.payload,'$.summary')    AS summary,
    CASE
        WHEN lce.kind IS NULL THEN
            CASE WHEN (f.verified OR p.seq IS NOT NULL) THEN 'verified' ELSE 'candidate' END
        WHEN lce.kind = 'fact_challenged' THEN 'challenged'
        WHEN lce.kind = 'fact_revalidated' THEN 'revalidated'
        WHEN lce.kind = 'fact_rejected' THEN 'rejected'
        WHEN lce.kind = 'fact_merged' THEN 'merged'
        WHEN lce.kind = 'fact_superseded' THEN 'superseded'
        ELSE 'candidate'
    END                                    AS state,
    CASE WHEN term.seq IS NOT NULL THEN 1 ELSE 0 END AS retired,
    CASE
        WHEN term.seq IS NOT NULL THEN 0
        WHEN lce.kind = 'fact_challenged' THEN 0
        ELSE (f.verified OR p.seq IS NOT NULL)
    END                                    AS verified,
    CASE
        WHEN term.seq IS NOT NULL THEN 0.0
        WHEN lce.kind = 'fact_challenged' THEN 0.4
        ELSE COALESCE(json_extract(pv.payload,'$.confidence'), f.confidence)
    END                                    AS confidence
FROM events f
LEFT JOIN (
    SELECT challenge_id,
           CAST(json_extract(payload,'$.fact_seq') AS INTEGER) AS fact_seq,
           MAX(seq) AS seq
    FROM events WHERE kind = 'fact_verified'
    GROUP BY challenge_id, CAST(json_extract(payload,'$.fact_seq') AS INTEGER)
) p ON p.challenge_id = f.challenge_id AND p.fact_seq = f.seq
LEFT JOIN events pv ON pv.seq = p.seq
LEFT JOIN (
    SELECT challenge_id,
           CAST(json_extract(payload,'$.fact_seq') AS INTEGER) AS fact_seq,
           MAX(seq) AS seq
    FROM events WHERE kind = 'fact_summarized'
    GROUP BY challenge_id, CAST(json_extract(payload,'$.fact_seq') AS INTEGER)
) s2 ON s2.challenge_id = f.challenge_id AND s2.fact_seq = f.seq
LEFT JOIN events s ON s.seq = s2.seq
LEFT JOIN (
    SELECT challenge_id, fact_seq, MAX(seq) AS seq
    FROM (
        SELECT challenge_id,
               CAST(json_extract(payload,'$.fact_seq') AS INTEGER) AS fact_seq, seq
        FROM events
        WHERE kind IN ('fact_challenged','fact_revalidated','fact_rejected','fact_superseded')
        UNION ALL
        SELECT challenge_id,
               CAST(json_extract(payload,'$.from_fact_seq') AS INTEGER) AS fact_seq, seq
        FROM events WHERE kind = 'fact_merged'
    )
    GROUP BY challenge_id, fact_seq
) lc2 ON lc2.challenge_id = f.challenge_id AND lc2.fact_seq = f.seq
LEFT JOIN events lce ON lce.seq = lc2.seq
LEFT JOIN (
    SELECT challenge_id, fact_seq, MAX(seq) AS seq
    FROM (
        SELECT challenge_id,
               CAST(json_extract(payload,'$.fact_seq') AS INTEGER) AS fact_seq, seq
        FROM events
        WHERE kind IN ('fact_rejected','fact_superseded')
        UNION ALL
        SELECT challenge_id,
               CAST(json_extract(payload,'$.from_fact_seq') AS INTEGER) AS fact_seq, seq
        FROM events WHERE kind = 'fact_merged'
    )
    GROUP BY challenge_id, fact_seq
) term ON term.challenge_id = f.challenge_id AND term.fact_seq = f.seq
WHERE f.kind = 'fact_added';
```

### 2.3 执行验证证据（真实 events schema，内存 SQLite）

13 场景全部通过（v2 的 A-I 重新在 v3 SQL 上验证 + 4 个新场景）：

| 场景 | 断言 |
|---|---|
| A promote / B +challenge / C +challenge+revalidate / D origin-verified 链 / E reject / F merge / G summary / H candidate+challenge / I 双 promotion latest-wins | 与 docs/12 §8 对应项一致（C/D 的 state 名随 v3 语义为 `revalidated`） |
| **J reject→revalidate** | state=revalidated, **retired=1, verified=0, conf=0.0**（复活阻断） |
| **K supersede→revalidate** | 同上 |
| **L merge(from)→revalidate** | from 事实 retired=1；to 事实不受影响 |
| **M 跨 challenge 污染** | c2 的 transition 不影响 c1 事实（challenge 双键绑定生效） |

列完整性在 A/G 场景断言：`fact_text/fact_actor/promotion_seq/promotion_actor/
promotion_artifact_id/summary_seq` 全部有值。

### 2.4 性能与索引

实施 PR 附：千级/万级事件 fixture 微基准 + `EXPLAIN QUERY PLAN` + 索引策略评估
（候选：`events(kind, seq)`、按 `json_extract` 表达式索引评估）。基准证明 VIEW 为热路径
瓶颈**之前**不引入缓存表（docs/12 §6.2 结论保留）。

---

## 3. sticky 生命周期语义（阻断 A 的定稿）

- `retired` 是**单调**的：一旦 rejected/merged/superseded 出现，任何后续 lifecycle 事件
  不改变 `retired=1`。这与 `_upsert_fact_state:1308` 的
  `retired_seq=COALESCE(excluded.retired_seq, fact_states.retired_seq)` 和
  `_fact_state_map:1440` 的 `retired = retired_seq is not None` 逐点等价（已读代码核对）。
- `state` 列保留"最新 lifecycle 事件"映射（与 `fact_states.state` 的 latest-write-wins
  一致），因此 `retired=1, state='revalidated'` 是合法且符合生产的形态；
  消费方契约：**先 retired 过滤，再 state 语义**（与现有 `snapshot`/
  `verified_evidence`/`active_candidates` 的 `retired or terminal-state` 判断一致）。
- Python 消费方改造后仍允许保留 `_fact_state_map` 作为加速缓存，但正确性只依赖 VIEW
  （一致性由测试矩阵 §10 锁定）。

## 4. Board `replace_by_source` 完整契约（docs/13 高优先级缺口定稿）

```python
# Board Protocol 增
def replace_by_source(self, source_seq: int, finding: Finding) -> bool:
    """source_seq 指原 fact_added 事件 seq（稳定身份，与 cold replay 一致）。
    把该 source 下所有未 superseded 的 Finding 标记 superseded_by 新 finding.finding_id，
    再追加 replacement。找不到旧 Finding 时仍追加 replacement 并返回 False（部分同步安全）。"""
```

- **replacement 内容**：`source_seq` 保持原 fact seq；promotion 身份放
  `payload["promotion"] = {seq, actor, witness, verifier, source, artifact_id}`；
  `Finding.kind/target/data` 沿用原结构化 finding（VIEW 的 finding_kind/target/data 列），
  缺失时回退 `TEXT_FACT`。
- **幂等与游标**：projector 以事件 seq 游标推进；`sync()` 现有语义（异常传播则
  `after_seq` 不推进，重试重投影）保持不变并加测试锁定；同一 promotion 事件被重放时，
  旧 Finding 已 superseded → 直接追加等价 replacement（Board 端最终视图唯一，因旧者
  均带 superseded 标记被 query 过滤）。
- **原子性**：`MemoryBoard` 单事件循环内完成标记+追加（无并发写者）；`PostgresBoard`
  在同一事务内完成；契约注释写明。
- **事件形态**：复用 `finding_upserted` BLACKBOARD_DELTA，增 `supersedes_source_seq` 字段；
  UI 据此做替换动画。
- **订阅集**：projector `kinds=("fact_added","fact_verified")`；`fact_summarized` 不投影
  （Board 是注意力视图，summary 走 §5 恢复路径）。

## 5. summary 消费链（docs/13 纠正采纳）

- UI 保持**纯 bus subscriber**：gist 即时更新走 `NODE_SUMMARIZED`；该事件已由 EventBus
  durable sink 写入 SessionStore，SSE 重连全量重放历史——**现有机制已满足 gist 恢复**。
- RFC v3 **删除** v2 的"UI 调用 `fact_summaries()` 恢复"旁路；canonical
  `fact_summarized` 事件的消费方仅为：BTW fact 预览、审计、replay QA（读 VIEW）。
- `record_fact_summary` 的 canonical 化价值陈述改为："重放/审计可完整恢复摘要记录"，
  不再声称"Reason 或 UI 依赖它"。

## 6. 回滚：两阶段发布 + backup-only（docs/13 阻断 D 定稿）

1. **Phase 1（随 M3 同一发布）**：在 `SQLiteSharedGraph.open()`/`open_readonly()` 增加
   `PRAGMA user_version` 检查——`user_version > SUPPORTED(1)` 时拒绝打开；**本次不 bump**。
   既有 DB（user_version=0）不受影响。
2. **Phase 2（下一发布）**：`user_version = 2` + 写入 M3 schema（VIEW/trigger）。
   Phase-1 二进制（SUPPORTED=1）自动拒绝打开 2；**早于 Phase-1 的旧二进制没有任何
   版本检查，无法拦截**——如实声明。
3. **backup-only rollback**：升级前 `VACUUM INTO` 快照；回滚 = 恢复代码 + 恢复快照。
   删除 v2"旧二进制自动拒开"的过度承诺；文档明示唯一安全回滚路径是快照。

## 7. malformed JSON 防护（docs/13）

- 写路径：§1.2 guard（`json_valid` + `fact_seq` 整数类型）。
- DB 层：§1.2 `transition_json_guard` 触发器（仅新 kind）。
- VIEW 不防御（canonical 写者保证）；历史数据中 malformed payload 由测试确认不存在
  （若有，实施时作为数据迁移异常显式报告，不静默）。

## 8. `record_fact_summary` first-write-wins（docs/13）

`-> bool`：目标存在且 summary 非空时——无既有 summary → 追加并返回 `True`；已有**同值**
summary → `True`（幂等达成）；已有**不同值** → `False`（不覆盖，发一条审计 delta）。
目标不存在/空输入 → `False`。现有 `tests/test_summarizer.py:58-64` 断言保持通过；
:61 的"读 payload.summary"断言改为查 `fact_summarized` 事件/VIEW。

## 9. 读取链路清单（v3 全列，符号名；VIEW 26 列覆盖）

统一入口：`effective_fact(fact_seq)` / `effective_facts(*, verified_only, active_only)`。

| 消费方 | 使用列 |
|---|---|
| `verified_evidence` / `active_candidates` / `_active_fact_seqs_by_verified` | state/retired/verified/confidence |
| `snapshot` / `to_summary` / `_summary_for_fact_seqs` | fact_text/fact_source/verified/confidence/artifact_id/witness/verifier/source |
| `_reason_relevant_fact_seqs` | verified + retired |
| `canonical_credentials` | verified（promotion 后凭据可识别） |
| `fact_pin_context` | fact_text/verified/confidence |
| `_fact_base_verdict`（原 `_fact_origin_verdict`） | base_verified/base_confidence |
| `review_flow._candidate_fact_count` fallback | state='candidate' |
| `blackboard_bridge` / `BoardProjector` | finding_kind/target/data + §4 协议 |
| `per_flag_evidence_chains` | 经 `verified_evidence`（回归锁定） |
| blackboard skill `read_facts`/`read_review` | VIEW（challenged 不暴露、retired 过滤） |
| btw.py / raw `events()`/`recent_events()` | **保持 append-time raw**（docs/12 §6.3） |

## 10. 测试矩阵（docs/12 §8 的 20 项 + docs/13 新增）

docs/12 的 20 项全部保留并对应 v3 语义（第 4/5 项 state 名为 `revalidated`）。新增：

21. reject/supersede/merge → revalidate 均保持 `retired=1`（场景 J/K/L 固化）；
22. 跨 challenge transition 不污染他题 fact（场景 M）；
23. VIEW 26 列全可用（A/G 列断言固化）；
24. malformed JSON / 非整数 fact_seq 的 transition 被 Python guard 与 DB trigger 双重拒绝；
25. `record_fact_summary` 同值重试 True、异值 False（first-write-wins）；
26. `replace_by_source` 契约 7 项（source_seq 身份、部分同步、幂等重放、两后端原子性、
    缺失旧 Finding、游标失败不推进、supersedes delta 形态）；
27. Phase-1 二进制拒绝 `user_version > 1`；Phase-2 bump 后 Phase-1 拒绝、早于 Phase-1 不拦（如实）；
28. UI gist 恢复仅经 SessionStore 重放（无 UI→graph 旁路）。

## 11. Verdict 对照（docs/13 最终结论）

| 项目 | v3 状态 |
|---|---|
| 事件化方向 / genesis | 保留 |
| VIEW | **可执行 + 13 场景 + sticky + challenge 绑定 + 26 列** |
| BoardProjector | 单协议完整契约（§4） |
| rollback | 两阶段 + backup-only（§6） |
| summary 消费链 | 如实（§5/§8） |
| M3 生产实施 | **待本版评审通过后放行** |
