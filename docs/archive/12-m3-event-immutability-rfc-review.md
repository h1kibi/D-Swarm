> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../00-architecture-spec.md) 取代；本文保留作为时代记录。

# M3 事件不可变 RFC v1 评审反馈

> 评审对象：[`docs/11-m3-event-immutability-rfc.md`](11-m3-event-immutability-rfc.md)
> 关联计划：[`docs/10-v4-kernel-improvement-implementation.md`](../10-v4-kernel-improvement-implementation.md) M3
> 评审日期：2026-08-14
> 结论：**Conditional No-Go / RFC v1 必须修订后再评审；暂不实施 M3 生产代码。**

---

## 0. 执行摘要

RFC v1 对原设计阻塞的处理方向是正确的：

- promotion 不再原位改写 `fact_added`，而是追加 canonical `fact_verified`；
- summary 明确选择 canonical `fact_summarized`，不再依赖不可重建的旁表；
- `events` 增加 UPDATE/DELETE trigger；
- 读取链路审计扩展到 blackboard skill；
- 旧库已被原位修改的内容作为 genesis 基线。

这些选择解决了“删除旁表后无法只凭 immutable events 知道 promotion/summary”的原始矛盾，事件化方向应保留。

但 RFC v1 **还不能作为实现依据**。当前至少有四个阻断级问题：

1. 文档中的 `fact_effective` SQL VIEW 无法执行；
2. effective fold 没有闭合 fact lifecycle，blackboard 会把 challenged fact 错误显示为 VERIFIED；
3. `_fact_origin_verdict()` 被漏审，`promotion → challenge → revalidate` 会恢复成 candidate；
4. promotion event 丢失触发 verified 写入的 artifact/provenance，不能满足可审计证据链。

此外，BoardProjector 仍保留两个互斥实现分支、`record_fact_summary()` 返回契约写错、回滚方案会丢失升级后状态、读取链路清单不完整。因此，本轮结论不是否决 M3，而是要求形成 **RFC v2** 后再次评审。

## 1. 核验范围与基线

本轮直接核对了以下代码路径：

- `dswarm/swarm/shared_graph.py`
  - `events` schema、`_append()`、`add_evidence()`；
  - fact challenge/revalidate/reject/merge/supersede；
  - `active_candidates()`、`verified_evidence()`、`snapshot()`；
  - `canonical_credentials()`、fact retention、Reason summary；
  - `record_fact_summary()`、raw event API。
- `dswarm/swarm/projection.py::BoardProjector`
- `dswarm/swarm/board.py::MemoryBoard`
- `dswarm/swarm/blackboard_bridge.py`
- `dswarm/swarm/review_flow.py`
- `skills/dswarm-blackboard/blackboard.py`
- `dswarm/solver/btw.py`
- `tests/test_summarizer.py` 与相关 shared-graph 测试。

启动基线：

- `./init.sh` 在 Windows checkout 中按项目保护逻辑拒绝经 WSL 执行，这是预期行为；
- 使用 AGENTS.md 指定的等价命令 `uv run pytest -q`；
- 结果：**exit 0，0 failed，4 条第三方弃用 warning**。

本评审未修改 M3 生产代码，也未弱化 provenance/flag gate。

---

## 2. 阻断级问题

### 2.1 `fact_effective` VIEW 当前 SQL 不可执行

RFC §4.1 使用：

```sql
SELECT f.seq AS fact_seq,
       (f.verified OR v.fact_seq IS NOT NULL) AS verified,
       COALESCE(v.confidence, f.confidence) AS confidence,
       s.summary AS summary
FROM events f
LEFT JOIN events v ...
LEFT JOIN events s ...
```

但 `events` schema 只有：

```text
seq, ts, challenge_id, actor, kind, payload,
artifact_id, verified, confidence, dedupe_key
```

不存在 `v.fact_seq` 与 `s.summary` 列。二者都位于 JSON payload 中。按 RFC 原 SQL 建 VIEW 后执行查询，SQLite 实际报错：

```text
sqlite3.OperationalError: no such column: v.fact_seq
```

至少必须改成 `json_extract(...)`。同时不能用未限定的普通双 LEFT JOIN 草率替换，因为未来若出现多条 promotion/summary event，会形成行乘积。RFC v2 应给出一份在真实 SQLite 上执行过的最终 SQL，并明确如何按最大 `seq` 选择最新 canonical transition。

### 2.2 VIEW 没有闭合 fact lifecycle，名称与语义不一致

RFC 的 VIEW 只折叠：

```text
fact_added.verified
OR fact_verified
+ fact_summarized
```

但当前有效事实状态还受这些 lifecycle transition 影响：

```text
fact_challenged
fact_revalidated
fact_rejected
fact_merged
fact_superseded
```

Python 路径通过 `_fact_state_map()` 继续过滤生命周期；blackboard skill 的 `read_facts()` 却只过滤 terminal/retired fact，并不会把 challenged fact 降级。若它按 RFC 直接改查当前 `fact_effective` VIEW，就会发生：

```text
candidate → fact_verified → fact_challenged
```

原事实仍被 VIEW 标成 verified，`read-facts --verified-only` 仍会向 worker 暴露它，违反当前“challenged fact 不得依赖”的行为。

RFC v2 必须选定一个闭合模型：

- **推荐：**让真正名为 `fact_effective` 的 VIEW 仅从 canonical events 折叠 promotion、summary 和最新 lifecycle event，直接输出最终 `state/retired/verified/confidence`；
- 或将当前 VIEW 政名为 `fact_event_state`/`fact_promotion_state`，并要求所有消费者再显式应用 lifecycle projection。但这种方案下 blackboard 不能只“换一处 SQL”就结束，且必须提供 `fact_states` 从 events 重建的明确协议。

当前 RFC 一边声称“events 是唯一事实源”，一边依赖未写入 VIEW 的 mutable `fact_states` 才能得到真正 effective 状态，接口尚未闭合。

### 2.3 `_fact_origin_verdict()` 漏审导致 revalidate 语义回归

现代码：

```python
def _fact_origin_verdict(self, fact_seq: int) -> tuple[bool, float]:
    SELECT verified, confidence FROM events
    WHERE seq=? AND kind='fact_added'
```

`revalidate_fact()` 用它恢复 challenged fact 的 verdict：

```python
orig_verified, orig_conf = self._fact_origin_verdict(fact_seq)
verified_effective = 1 if orig_verified else 0
```

M3 后 promotion 不再修改原 `fact_added`，所以以下链路会出错：

```text
candidate fact_added
→ fact_verified
→ fact_challenged
→ fact_revalidated
```

`_fact_origin_verdict()` 仍读到 candidate，最终把已 promotion 的事实恢复为 `verified_effective=0`。

RFC v2 必须把 `_fact_origin_verdict()` 纳入读取链路，并将其语义改为“lifecycle 之前的 canonical base verdict”，即 `fact_added + fact_verified` 的折叠值。必须增加确定性测试：

```text
candidate promotion → challenge → revalidate → verified=True
```

### 2.4 promotion event 不能丢失触发验证的 provenance

RFC 规定 `fact_verified`：

```text
artifact_id=NULL
payload={fact_seq, confidence, witness, verifier, triggered_by}
```

并声称“证据链仍挂在原始 `fact_added` 上”。这与 promotion 的真实触发场景不一致。当前 `add_evidence()` 注释明确说明：candidate marker 可能先写入，随后带 verified 状态的 skill copy 与其 dedupe collision。后一次写入可能携带新的：

```text
artifact_id, witness, verifier, source
```

原 candidate 不保证拥有该 artifact。若新 event 强制 `artifact_id=NULL`，则 effective verified 状态缺少触发验证的 artifact 归因；`snapshot()`/Board projection 若继续取原事件字段，也会保留空或旧 provenance。

RFC v2 应规定：

- `fact_verified.artifact_id` 保存触发 promotion 的 canonical artifact ID；
- payload 至少保存 `fact_seq`、`verified=true`、`confidence`、`witness`、`verifier`、`source`；
- `actor` 使用触发写入者，不再用难解析的 `triggered_by` 字符串替代结构化字段；
- effective projection 同时输出 promotion 的 artifact/witness/verifier/source；
- 若目标 fact 不存在、不是同 challenge 的 `fact_added`，拒绝追加 transition；
- 不修改、放宽或绕过 `gate.py` 与 anti-laundering 路径。

---

## 3. 高优先级设计缺口

### 3.1 VIEW JOIN 必须绑定 challenge 与目标 kind

RFC JOIN 只比较 payload `fact_seq`，没有要求：

```sql
v.challenge_id = f.challenge_id
s.challenge_id = f.challenge_id
```

也没有在写 transition 前验证目标确实是同 challenge 的 `fact_added`。虽然当前设计通常“一 challenge 一 DB”，schema 和部分 API 已明确携带 `challenge_id`，transition 仍应具备 referential guard，不能允许错误或恶意 metadata event 引用其他 challenge 的 seq 污染 projection。

### 3.2 `BoardProjector` 仍是未定稿分支

RFC §4.2 写的是：

> 投影时传入 effective 状态，**或**投影 `fact_verified` 事件为 UPDATE 语义。

这两个方案不是同一个协议。当前代码事实是：

- `BoardProjector.sync()` 只订阅 `kinds=("fact_added",)`；
- `project_event()` 只接受 `fact_added`；
- `MemoryBoard.write_finding()` 会追加 Finding；
- Board 没有“按 `source_seq` 原位更新 payload”的接口，只有 `supersede(old_id, new_id)`。

因此：

- 仅让 `fact_added` 投影时查询 VIEW，可让 cold replay 得到 effective 值，但在线 promotion 不会唤醒 projector；
- 订阅 `fact_verified` 可以唤醒在线更新，但必须定义如何找到原 Finding、生成 replacement、supersede 旧 Finding，并保证 cold replay 与在线结果等价。

RFC v2 必须选定一个方案并给出具体 Board API/状态转移。不能把该选择留到实现阶段。

### 3.3 `record_fact_summary()` 返回契约写错

现有公开方法签名为：

```python
def record_fact_summary(...) -> bool
```

现有测试断言：

```text
成功 = True
不存在/空输入 = False
```

RFC 却规定重复调用由 `_append()` 返回 `-1`。这会把内部 event seq/no-op 值泄漏为方法返回值，而且 `bool(-1) is True`，容易制造隐蔽错误。

RFC v2 应保留 bool 契约并明确重复语义，例如：

- 首次成功追加：`True`；
- 目标不存在或输入为空：`False`；
- 同一 fact 已有 summary：选择 `False` 表示未写入，或 `True` 表示幂等目标已达成，但必须定稿并测试。

测试应从“原 payload 被 patch”改为查询 canonical summary API/VIEW，同时继续验证方法返回 bool。

### 3.4 metadata event envelope 不应笼统标 `verified=1`

RFC 将 `fact_verified` 和 `fact_summarized` 都写成：

```text
verified=1, confidence=1.0
```

当前 raw event API、BTW snapshot 与 Review raw timeline 会直接展示 envelope 的 `verified/confidence`。这样会把 `fact_summarized` 这种 metadata transition 显示成“verified evidence event”，混淆跨 kind 语义。

推荐：

- metadata transition 的 event envelope 使用 `verified=0`；
- promotion verdict 放在 typed payload 与 effective fold 中；
- `events()`、`events_since()`、`recent_events()` 明确返回 append-time/raw envelope；
- 需要事实有效状态的消费者只能调用专用 fact projection/API。

若 RFC 坚持 envelope `verified=1`，则必须先定义该字段对所有 event kind 的统一语义，并审计所有泛型消费者；当前文档没有做到。

### 3.5 回滚方案不是语义安全回滚

RFC 声称：

> 移除 trigger + 恢复旧 UPDATE；新 metadata events 保留无害，旧代码不读它们。

恰恰因为旧代码不读它们，回滚后会丢状态：

- 升级后发生的 promotion 只存在于 `fact_verified`，旧代码重新看到 candidate；
- 升级后发生的 summary 只存在于 `fact_summarized`，旧代码看不到 summary。

RFC v2 必须二选一：

1. **forward-only migration**：升级后的 DB 不允许旧二进制重新打开；或
2. **downgrade materialization**：回滚前将 effective promotion/summary 安全写回旧行，再移除 trigger、切回旧代码，并有备份与测试。

当前“事件保留无害”只能说明不会崩溃，不能说明语义可回滚。

---

## 4. 读取链路审计仍不完整

RFC §4.2 标注“全部已核实行号”，但至少漏掉以下直接或间接消费者：

| 读取点 | 风险 |
|---|---|
| `_fact_origin_verdict()` | revalidate 恢复成 candidate，见 §2.3 |
| `active_candidates()` | promotion 后仍可能计入 candidate |
| `_active_fact_seqs_by_verified()` | retention/Reason 事实集合分类错误 |
| `canonical_credentials()` | 直接过滤原 `events.verified`，promotion 后漏掉已验证凭据 |
| `fact_pin_context()` | verdict/confidence 继续使用 append-time 值 |
| `_summary_for_fact_seqs()` | 直接使用原 verified/confidence，且当前不消费 fact summary |
| `review_flow.py::_candidate_fact_count()` fallback | raw event fallback 会把已 promotion fact 继续计为 candidate |
| `blackboard_bridge.py` | 只映射 `fact_added`，不传播 promotion transition |
| `BoardProjector` cold/online projection | 见 §3.2 |
| `per_flag_evidence_chains()` | 间接依赖 `verified_evidence()`，应纳入回归测试 |

此外，RFC §1/§4.2 对 blackboard skill 的行号与函数描述已经漂移：当前文件存在 `read_facts()` 的直接查询，但没有文档声称的 `facts_json()`；`:521` 当前位于 `read_review()` 查询。RFC v2 应基于符号名而不是易漂移的裸行号维护清单。

建议新增统一 API，例如：

```text
effective_fact(fact_seq)
effective_facts(...)
```

Python 路径统一经该 API；blackboard skill 使用与其同一 SQL contract 的 VIEW。避免每个调用方自行拼接 `fact_added + fact_verified + lifecycle`。

---

## 5. summary 的现状描述需要纠正

RFC 说 summary 是“UI gist 与 Reason 摘要的用户可见特征”。代码事实是：

- UI 的即时更新主要消费 EventBus `NODE_SUMMARIZED`；
- SharedGraph `record_fact_summary()` 当前把 summary patch 进 payload；
- `to_summary()` 通过 `snapshot()` 输出原 fact；
- `_summary_for_fact_seqs()` 也输出原 fact，没有读取 `payload["summary"]`；
- `to_reason_summary()` 因此没有明显消费已存 summary。

把 summary 设为 canonical 仍可成立，例如为了 restart/replay 后恢复 UI gist，但 RFC 必须准确写出目标消费 API：

- 哪个 replay/API 会读取 `fact_summarized`；
- UI 重连后如何从 SharedGraph 恢复 gist；
- Reason 是否继续只看原始事实，还是显式增加摘要字段；
- 原始 fact 必须继续保留，不能用 gist 替代证据内容。

在该消费链未定义前，不应把“Reason 摘要已依赖它”作为 canonical 选择的现状证据。

---

## 6. 对 RFC 三个未决问题的建议结论

### 6.1 promotion 唯一性：保留每事实至多一次

第一版可以保留：

```text
dedupe_key = fact_verified::<fact_seq>
```

这与当前“candidate 只提升一次”的行为接近。VIEW 仍应防御异常重复数据并按最大 seq 选择 transition；写路径必须检查目标 fact、challenge 和既有 promotion。

关键不是放开多次 promotion，而是保证：

```text
promotion → challenge → revalidate
```

恢复 promotion 后的 base verdict，而不是原始 candidate verdict。

### 6.2 VIEW 性能：暂不引入缓存表，但先提供实测

当前无需预先增加 `fact_verifications`/`fact_summaries` 缓存表。RFC v2 应先完成：

- 真实 SQLite SQL 执行测试；
- 千级与万级 event fixture 的查询微基准；
- `EXPLAIN QUERY PLAN`；
- 对 `challenge_id/kind/json_extract(payload,'$.fact_seq')/seq` 的索引策略评估。

只有基准证明 VIEW 成为热路径瓶颈时，才新增可删除、可重建的 projection cache。

### 6.3 recent_events：保持 raw append-time 语义

`events()`/`events_since()`/`recent_events()` 应继续作为 canonical raw event API，不把历史 `fact_added` 动态改写成 effective 状态。文档需明确：

- envelope 字段是 append-time；
- effective fact 状态来自专用 VIEW/API；
- BTW 与 Review raw timeline 不得把 metadata transition 的 envelope 当作事实 verdict。

---

## 7. RFC v2 必修清单

RFC v2 获批前至少完成以下内容：

1. 提供可在 SQLite 实际执行的 VIEW/查询，所有 JSON 字段使用 `json_extract()`。
2. transition 与 fact 按 `challenge_id`、`fact_seq`、`kind='fact_added'` 绑定并验证。
3. 定义 latest-by-seq 规则，避免多事件 JOIN 行乘积。
4. `fact_effective` 闭合 challenged/revalidated/terminal lifecycle，或改名并明确第二层 fold。
5. `fact_verified` 保存触发 promotion 的 artifact、witness、verifier、source。
6. `_fact_origin_verdict()` 改为读取 promotion 后、lifecycle 前的 canonical base verdict。
7. 定稿 BoardProjector 的在线 promotion 与 cold replay 协议，不再保留“或”分支。
8. 保持 `record_fact_summary() -> bool`，明确重复调用语义。
9. metadata event envelope 语义定稿；推荐 `verified=0`，verdict 进 payload。
10. 将读取链路清单补全到 §4，并以符号名为主、行号为辅。
11. 修正 summary 的真实消费链描述，定义 restart/replay 后的 UI/API 行为。
12. 回滚改为 forward-only 或提供 downgrade materialization。
13. 性能结论由 executable SQL + benchmark + query plan 支撑。
14. docs/10 的“设计阻塞已解决”应在 RFC v2 获批前改成“事件化方向已提出，设计评审仍未通过”。

---

## 8. RFC v2 最低测试矩阵

除 RFC v1 已列测试外，必须新增：

1. VIEW SQL 在真实 SQLite schema 上可创建且可查询。
2. candidate → promotion：原 `fact_added` 全字段/稳定哈希不变。
3. promotion event 保存 artifact/witness/verifier/source，effective projection 可追溯。
4. candidate → promotion → challenge：最终 `verified=False`。
5. candidate → promotion → challenge → revalidate：最终 `verified=True` 且恢复 promotion confidence/provenance。
6. verified origin → challenge → revalidate：保持现有语义。
7. reject/merge/supersede 后不出现在 blackboard verified-only、snapshot、Reason、credentials。
8. blackboard `read-facts --verified-only` 不显示 challenged fact。
9. `canonical_credentials()` 能读取经 promotion 验证的凭据。
10. `fact_pin_context()`、`active_candidates()`、`verified_evidence()` 分类一致。
11. BoardProjector 在线 promotion 与从空 Board cold replay 得到等价有效 Finding。
12. BlackboardBridge/reconcile 不丢 promotion transition。
13. `record_fact_summary()` 始终返回 bool；重复语义符合 RFC。
14. summary event replay 后可恢复指定 API/UI gist，原始 fact 不被替换。
15. raw `recent_events()` 保持 append-time 字段，effective API 返回最终状态。
16. 错误 challenge_id/不存在 fact_seq/非 fact_added 目标不能产生有效 transition。
17. downgrade 路径测试，或旧二进制打开升级 DB 被明确拒绝。
18. 全仓 `UPDATE events|DELETE FROM events` 静态扫描为 0，DB trigger 同时阻断直接 SQL。
19. provenance gate、anti-laundering、flag acceptance 原测试全部保持不变并通过。
20. 完整 `uv run pytest -q` 绿色。

---

## 9. 最终 Verdict

| 项目 | 结论 |
|---|---|
| 追加 `fact_verified` | **方向批准，协议需修订** |
| 追加 `fact_summarized` | **条件批准，需闭合真实消费链** |
| 废弃两张新旁表 | **批准** |
| SQL VIEW | **当前 No-Go：SQL 不可执行且 lifecycle 不完整** |
| UPDATE/DELETE trigger | **批准，实施时需与迁移顺序一起测试** |
| BoardProjector 改造 | **No-Go：方案未选定** |
| 迁移 genesis 边界 | **批准** |
| 回滚方案 | **No-Go：当前会丢升级后状态** |
| M3 生产实施 | **暂不批准** |

结论：**M3 的设计阻塞尚未完全解除。RFC v1 已选对事件化方向，但还没有形成可执行、生命周期完整、provenance 完整、可回滚且所有消费者一致的协议。请先按 §7 修订为 RFC v2，再进行下一轮评审；在此之前不要修改 canonical events 的生产语义。**
