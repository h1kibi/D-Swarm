# M3 事件不可变 RFC v2 第三轮复评

> 评审对象：[`docs/11-m3-event-immutability-rfc.md`](11-m3-event-immutability-rfc.md)（RFC v2）
> 上一轮评审：[`docs/12-m3-event-immutability-rfc-review.md`](12-m3-event-immutability-rfc-review.md)
> 评审日期：2026-08-14
> 结论：**Conditional No-Go / RFC v2 仍不能直接进入 M3 生产实施。事件化方向继续批准，但需形成 RFC v3 并复评。**

---

## 0. 执行摘要

RFC v2 确实解决了 v1 的多项硬问题：规范 VIEW SQL 可以在真实 `events` DDL 上创建和查询；文档附带的 9 个场景可复现为 9/9 通过；promotion 的 artifact/witness/verifier/source 被放回 canonical event；`_fact_base_verdict` 的方向能够修复 promote→challenge→revalidate；metadata envelope、`record_fact_summary() -> bool`、读取链路清单也比 v1 明确。

但独立扩展验证发现，RFC v2 仍有四个阻断级问题：

1. **terminal lifecycle 会被后续 revalidate/challenge 复活**，与当前 `fact_states.retired_seq` 的粘性退役语义不等价；
2. **VIEW 没有按 `challenge_id` 绑定 transition**，跨 challenge 的错误事件会污染另一个 challenge 的事实；
3. **`fact_effective` 的规范列不足以承载 §4/§9 承诺的消费者改造与完整 provenance**；
4. **`PRAGMA user_version` 不能让已经发布的旧二进制自动拒绝升级库**，RFC 当前的 rollback 验收陈述不可实现。

此外，Board replacement 的幂等/原子契约与 UI summary 恢复路径仍未闭合。因此本轮不是否决 M3，而是要求 RFC v3 收紧状态机、投影身份、迁移边界和前端事件流契约；在此之前不要修改 canonical `events` 生产语义。

本轮只评审文档与执行临时验证，**未修改 M3 生产代码、provenance gate 或 anti-laundering 路径**。

---

## 1. 已独立确认通过的修订

### 1.1 RFC 的 9 场景 SQL 证据可复现

执行 `%TEMP%/rfc_v2_view_check.py`，结果为：

```text
[A promote] PASS
[B promote+challenge] PASS
[C promote+challenge+revalidate] PASS
[D origin-verified+challenge+revalidate] PASS
[E reject] PASS
[F merge(from)] PASS
[G summary] PASS
[H candidate+challenge] PASS
[I dup promotions latest-wins] PASS
done
```

因此，以下声明成立：

- SQL 已修复 v1 的不存在列问题，使用 `json_extract()`；
- promotion、summary、lifecycle 各自按 `MAX(seq)` 取最新记录，已避免普通多行 LEFT JOIN 的行乘积；
- promote→challenge→revalidate 在**没有 terminal transition**的受测路径中恢复 promotion base；
- promotion 的 artifact/witness/verifier/source 可由当前 VIEW 取回；
- merge 使用 `from_fact_seq` 退役被合并方。

### 1.2 promotion canonical event 的方向可实施

RFC v2 §1.2 把 promotion 的触发 provenance 放入 `fact_verified`：

- envelope `artifact_id` 保存触发写入的 artifact；
- payload 保存 `confidence/witness/verifier/source`；
- actor 保存触发者身份；
- `fact_added` 保持不变。

这与当前 `add_evidence()` 在 dedupe collision 后原位执行 `UPDATE events SET verified=1, confidence=?` 的真实触发点相符，能够替代现有两列原位改写。不过 effective API 还必须暴露 promotion actor/seq，见 §2.3。

### 1.3 `_fact_base_verdict` 修订方向正确

当前 `revalidate_fact()` 调用 `_fact_origin_verdict()`，后者只读原始 `fact_added.verified/confidence`。RFC v2 改为读取 `fact_added + fact_verified` 的 base fold，能够修复：

```text
candidate → promote → challenge → revalidate
```

恢复成 candidate 的缺陷。该项设计方向批准。

### 1.4 其他可保留决策

以下决策可直接带入 RFC v3：

- metadata transition envelope 统一 `verified=0`，有效 verdict 只由 fold 产生；
- raw `events()` / `events_since()` / `recent_events()` 保持 append-time 语义；
- `fact_summarized` 作为 canonical transition，而不是可丢旁表；
- `record_fact_summary()` 保持 `-> bool`，同值重试按幂等达成处理；
- `events` 增加 UPDATE/DELETE 拒绝触发器；
- 暂不引入缓存投影表，先做千级/万级 benchmark 与 query plan；
- provenance gate、anti-laundering、flag acceptance 不变。

---

## 2. 阻断级问题

### 2.1 terminal lifecycle 不是 latest-event-wins；RFC VIEW 会错误复活退役事实

RFC v2 的 VIEW 只取最新 lifecycle event：

```sql
SELECT fact_seq, MAX(seq) AS seq
...
GROUP BY fact_seq
```

并按最新 kind 计算 `retired`。因此下面三条序列都会被 VIEW 恢复为 active verified：

```text
reject     → revalidate
supersede  → revalidate
merge(from)→ revalidate
```

独立执行 RFC SQL 得到：

```text
terminal_then_revalidate fact_rejected   ('verified', 0, 1, 0.9)
terminal_then_revalidate fact_superseded ('verified', 0, 1, 0.9)
terminal_then_revalidate fact_merged     ('verified', 0, 1, 0.9)
```

但当前生产语义不是这样。`_upsert_fact_state()` 使用 `COALESCE` 保留已写入的 `retired_seq`，`_fact_state_map()` 以 `retired_seq IS NOT NULL` 判断退役；即使后续错误调用 revalidate，事实仍不会进入 `verified_evidence()` 或 `active_candidates()`。对真实 `SQLiteSharedGraph` 执行同样序列得到：

```text
reject     ('revalidated', 1, 0.9, retired_seq=2, updated_seq=3) → verified_evidence=[]
supersede  ('revalidated', 1, 0.9, retired_seq=2, updated_seq=3) → verified_evidence=[]
merge      ('revalidated', 1, 0.9, retired_seq=3, updated_seq=4) → 原 from fact 不再出现
```

这说明 RFC §2.2/§3 所称“完整折叠 lifecycle”与“`fact_states.updated_seq` 的等价事件表达”仍不成立。

**RFC v3 必修：**

1. 正式定义状态机：`rejected/merged/superseded` 为 absorbing terminal state；
2. 写路径拒绝 terminal 后的 challenge/revalidate/promotion/summary（summary 是否允许需单独定稿）；
3. VIEW 对历史/异常日志采用 terminal-sticky fold：只要存在合法 terminal event，`retired=1`，后续非 terminal 事件不得复活；
4. 增加至少 6 个测试：三种 terminal 各自接 revalidate 与 challenge；
5. 明确多个 terminal event 时选择 first-terminal 还是 latest-terminal 作为显示 state/reason，但两者都必须保持 retired。

在该状态机定稿前，M3 不能实施。

### 2.2 VIEW 没有按 challenge 绑定 transition

RFC v2 §1.4 要求 transition 目标与当前 challenge 相同，但规范 SQL 的 promotion、summary、lifecycle 子查询都只按 `fact_seq` 分组，并只用：

```sql
... ON p.fact_seq = f.seq
... ON s2.fact_seq = f.seq
... ON lc.fact_seq = f.seq
```

没有携带或比较 transition 的 `challenge_id`。独立插入：

```text
c1: fact_added seq=1, verified
c2: fact_challenged payload.fact_seq=1
```

RFC VIEW 返回：

```text
('c1', 'challenged', 0)
```

即 c2 的错误 transition 污染了 c1 的事实。虽然设计假设通常“一 challenge 一 DB”，但 schema、只读打开测试和直接 SQLite 写入方都允许库内出现其他 challenge_id；RFC 自己也把“同 challenge referential guard”列为安全条件，因此不能只依赖调用方纪律。

**RFC v3 必修：**

- 所有 transition CTE/子查询携带 `challenge_id`；
- `GROUP BY challenge_id, fact_seq`；
- JOIN 同时绑定 `transition.challenge_id = f.challenge_id` 与 `fact_seq = f.seq`；
- 对所有 transition 增加事务内 referential guard；
- 因 blackboard skill 等组件可直接连接 SQLite，建议增加 `BEFORE INSERT ON events` transition guard，至少校验 `json_valid(payload)`、目标 `fact_added` 存在且 challenge 相同；
- 新增跨 challenge、目标不存在、目标非 fact_added、malformed payload 测试。

### 2.3 `fact_effective` 列集合不足，无法兑现 §4/§9 的统一消费者契约

规范 VIEW 当前只有：

```text
fact_seq, challenge_id, state, retired, verified, confidence,
artifact_id, witness, verifier, source, summary
```

但 RFC §4/§9 要求它支撑：

- BoardProjector 重建原 Finding kind/target/data；
- snapshot/to_summary/Reason/blackboard 输出原 fact；
- route lineage 与 `route_hash`；
- 原始 actor、时间与结构化 `finding`；
- promotion 的触发 actor/事件 seq；
- summary 的事件 seq/版本身份；
- credentials、pin context、审计 provenance。

当前 VIEW 没有 `fact`、原始 payload、原 actor/ts、structured finding、route_hash，也没有 promotion actor/seq。尤其 RFC 强调“actor 即触发者”，但 effective projection 根本没有该列，promotion 触发身份会在统一 API 中再次丢失。

这不是实现时的小细节，因为它决定 `effective_fact()` 的稳定返回 schema、Board replacement 的身份、blackboard skill 的 SQL 以及审计 API 的可追溯性。

**RFC v3 必修：**

给出 `effective_fact()` / `effective_facts()` 的完整 typed schema，并二选一：

1. 扩充 VIEW，至少暴露 `fact_text/fact_payload/fact_actor/fact_ts`、`promotion_seq/promotion_actor`、`summary_seq`；或
2. 明确 VIEW 只负责 verdict fold，公开 API 再以 `fact_seq`/transition seq 精确 JOIN 原事件，并给出最终 SQL 与返回模型。

无论选哪一种，BoardProjector、blackboard skill、snapshot、Reason、credentials 必须共享同一 effective model，不能各自重新拼一套 fold。

### 2.4 `user_version` 不能兑现“旧二进制拒绝升级库”

RFC v2 §7 写道：升级设置 `PRAGMA user_version=N+1`，旧二进制打开升级库会被明确拒绝。但当前已发布代码没有任何 `user_version` 检查：

- `SQLiteSharedGraph.__init__()` 直接连接并执行 `_SCHEMA`；
- `SQLiteSharedGraph.open()` 只是调用构造器；
- `open_readonly()` 同样不检查；
- 多条真实调用路径直接调用构造器而非 `open()`。

把一个临时 DB 设置为 `PRAGMA user_version=999` 后，用当前二进制实测：

```text
rw_opened_user_version 999
ro_opened_user_version 999
```

已经发布的旧二进制不可能因为未来新版本写入 `user_version` 就自动获得拒开逻辑。它可能继续插入旧语义事件，或在触发器拦截原位 UPDATE 时中途报错，而不是在 open 阶段安全失败。

**RFC v3 必须从以下方案中选定一个：**

- **两阶段发布**：先发布只增加 `MAX_SUPPORTED_USER_VERSION` guard、但不升级 DB 的兼容版本；确认升级路径后，下一版本才 bump schema 并切 M3；
- **诚实的 backup-only rollback**：删除“旧二进制拒绝升级库”的不可实现承诺，明确回滚必须同时恢复升级前 DB 快照；测试改为“新二进制拒绝未来版本 DB”，而不是“既有旧二进制拒绝升级 DB”；
- 若要支持 rolling/mixed-version 进程，还需单独设计 capability handshake，不能仅靠 `user_version`。

同时必须定义原子迁移顺序、失败恢复和所有构造入口的统一检查位置。仅在 `SQLiteSharedGraph.open()` 检查不够，因为项目存在直接构造与 `open_readonly()` 路径。

---

## 3. 高优先级设计缺口

### 3.1 `replace_by_source` 尚未形成可实现的幂等/原子协议

RFC v2 已从“二选一”收敛为单一 BoardProjector 方案，这是进步；但方法签名仍不足：

```python
def replace_by_source(self, source_seq: int, finding: Finding) -> bool:
```

当前两个实现都没有 source identity 唯一约束：

- `MemoryBoard.write_finding()` 每次直接追加，除 flag 外不去重；
- `PostgresBoard.swarm_findings.source_seq` 没有 UNIQUE 约束；
- projector 的 `after_seq` 是内存游标，投影成功后才在循环末尾推进；中途异常重试可能重复追加；
- `supersede()` 与 append replacement 在 Postgres 中目前是两个独立事务语义。

RFC v3 至少要定稿：

1. replacement 的 `source_seq` 表示原 `fact_seq`，还是 `fact_verified` transition seq；建议拆成 `fact_seq` 与 `source_event_seq`；
2. 同一 promotion 重放时不得生成第二个 active Finding；
3. 找不到原 Finding 时的行为：补建 effective Finding，还是返回失败并保持 projector cursor；
4. append replacement + supersede old 必须原子化；
5. Memory/Postgres 两实现都要满足同一返回值和通知语义；
6. cursor 仅在 replacement 完整成功后推进；
7. BLACKBOARD_DELTA 是发 `finding_upserted`、`finding_superseded` 两条，还是一个 replacement 事件，需固定并测试。

这部分可与 RFC v3 同轮修订，不必推翻事件协议。

### 3.2 summary 的“UI 恢复 API”与前端 dumb bus subscriber 不变式冲突

RFC v2 §8 提议 UI 恢复路径调用 `fact_summaries()`，而不是 EventBus 重放。但项目不变式是前端只订阅事件总线；并且当前 EventBus 的 durable sink 已把 `NODE_SUMMARIZED` 写入 SessionStore，SSE reconnect 会先重放完整 JSONL 历史。

因此需要先回答：SharedGraph summary 要修复的是哪一种 SessionStore 无法覆盖的恢复场景？如果没有新的缺口，直接增加 UI→SharedGraph 恢复旁路会制造第二状态源。

建议 RFC v3 采用其一：

- 保持 UI 纯事件订阅，后端 rehydrate 时从 `fact_summarized` 合成缺失的 `NODE_SUMMARIZED` 事件；或
- 明确 SharedGraph 只是 SessionStore 损坏/缺失时的后端修复源，前端仍不直接查询 solver core；或
- 若 SessionStore 已完整满足重连/重启恢复，则保留 `fact_summarized` 仅用于 canonical audit，并删除不必要的 UI API。

### 3.3 malformed JSON 会使整个 VIEW 查询失败

在 lifecycle event 中插入一个 malformed payload 后，查询 RFC VIEW 实测报错：

```text
sqlite3.OperationalError: malformed JSON
```

正常 `_append()` 使用 `json.dumps`，所以这不是日常路径；但 blackboard skill 和人工运维会直接接触 DB，M3 又把 VIEW 变成所有读路径的单点。建议 transition INSERT trigger 使用 `json_valid(payload)` 并验证目标 key 类型；VIEW 可再用 `CASE WHEN json_valid(payload)` 做防御性隔离或明确把 DB corruption 视为 fail-fast。

### 3.4 summary 的不同值重试语义未定义

RFC 只定义“首次 True、重复同值 True”，但 `dedupe_key=fact_summarized::{fact_seq}` 会使第二个不同 summary 无法写入。现状确实倾向“写回一次”，因此建议明确：

```text
同值重试 → True
不同值重试 → False（immutable first-write-wins）
```

若希望允许更好的后续 gist，则必须改为版本化 summary event，而不能沿用每 fact 唯一 dedupe key。

---

## 4. RFC v3 最低补充验证矩阵

在 RFC v2 的 20 项基础上，至少增加：

1. reject→revalidate 不复活；
2. reject→challenge 不复活；
3. supersede→revalidate/challenge 不复活；
4. merge(from)→revalidate/challenge 不复活；
5. transition challenge_id 与目标 fact 不同：写入被拒绝，VIEW 不受污染；
6. malformed JSON / 非整数 fact_seq：写入被拒绝或查询按定稿策略 fail-fast；
7. effective API 返回原 fact、原 actor/ts、structured finding、route_hash、promotion actor/seq；
8. promotion replay 两次只存在一个 active Board Finding；
9. replacement 在 append 与 supersede 中间故障后重试，最终仍只有一个 active Finding；
10. PostgresBoard 与 MemoryBoard 满足同一 replacement contract；
11. summary 同值重试 True、不同值重试按定稿语义返回；
12. 新二进制拒绝未来 user_version；
13. 若选择两阶段迁移，兼容版本与升级版本的矩阵测试；
14. 若选择 backup-only rollback，恢复旧二进制+旧 DB 快照的演练测试；
15. UI 重连/后端重启只通过事件流恢复 gist，不引入前端直连 graph 的旁路。

---

## 5. 对 RFC v2 声明的逐项 Verdict

| 项目 | 复评结论 |
|---|---|
| 规范 SQL 可创建、文档 9 场景可运行 | **通过** |
| latest-by-seq 避免普通 JOIN 行乘积 | **通过** |
| promotion provenance 写入 canonical event | **方向通过；effective API 仍缺 actor/seq** |
| `_fact_base_verdict` | **通过** |
| challenged/revalidated 普通循环 | **通过** |
| reject/merge/supersede lifecycle 闭合 | **No-Go：terminal 可被后续事件复活** |
| same-challenge referential binding | **No-Go：规范 VIEW 未绑定 challenge_id** |
| `fact_effective` 作为统一读取模型 | **No-Go：列集合不足** |
| BoardProjector 单协议 | **Conditional：方向选定，幂等/原子身份未定稿** |
| `record_fact_summary() -> bool` | **Conditional：需补不同值重试语义** |
| metadata envelope raw/effective 边界 | **通过** |
| UI gist restart/replay | **Conditional：必须保持 bus-only 前端契约** |
| forward-only / `user_version` | **No-Go：旧二进制拒开承诺不可实现** |
| UPDATE/DELETE trigger | **通过，需与迁移顺序和 direct writer 测试一起实施** |
| M3 生产实施 | **暂不批准** |

---

## 6. 最终结论与下一步

**最终 Verdict：Conditional No-Go。**

RFC v2 已把 v1 从“方向正确但 SQL/生命周期明显不完整”推进到“核心 SQL 可执行、主路径能跑”的阶段，四个 v1 原始阻断中有两个已实质解决、两个只解决了常规路径。但新增扩展场景证明，terminal 状态、challenge 绑定、effective schema 和 rollback 保证仍未达到实现级设计标准。

下一步应形成 **RFC v3**，仅修改设计文档，不写 M3 生产代码。RFC v3 通过以下门槛后可进入 TDD 实施：

1. terminal-sticky 状态机与 SQL 通过扩展场景；
2. transition 按 challenge+fact 双键绑定，并有 direct-SQL guard；
3. effective typed model 足够支撑全部消费者和 promotion provenance；
4. rollback 改为两阶段发布或诚实的 backup-only；
5. Board replacement 幂等、原子、cursor、通知语义闭合；
6. summary 恢复保持前端 dumb bus subscriber 不变式。

以上六项完成并附可执行 SQL/迁移/Board fault-injection 证据后，建议下一轮给出 **Conditional Go（先测试后实现）**；在此之前继续保持 `docs/10` 的“M3 设计评审未通过”状态。
