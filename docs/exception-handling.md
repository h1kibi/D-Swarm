# D-Swarm 异常处理策略与审计

> **状态：现行规范（2026-09-02）**  
> 本文档记录内核 120 处 `except Exception: pass` 的分类依据与新增规则。

## 背景

D-Swarm 内核约 120 处使用静默异常处理（`except Exception: pass`）。2026-08-28 的 B2 工单
（`docs/kernel-fixlist-2026-08-27.md`）对这些 call site 进行了全量 AST 复核，将其分为
四类：**K（内核隔离）**、**R（只读/增强）**、**T（可选遥测）**、**D（持久证据，已加观测点）**。

## 分类标准与处置原则

| 类别 | 含义 | 处置原则 | 示例场景 |
|------|------|----------|----------|
| **K** | Kernel isolation | 保留静默，升级会遮蔽真实 worker outcome 或造成资源泄漏 | EventBus sink/fan-out、进程/容器回收、取消、运行时释放、前端通知边界 |
| **R** | Readonly / enrichment | 保留静默，失败时已有空结果、降级文本或跳过语义 | board/flag 读取、环境映射、marker/readonly enrichment、summarizer 降级 |
| **T** | Telemetry | 保留静默，可选遥测不能改变调度、flag acceptance、append-only graph 或 finalize | energy capture/sidecar、projection、runtime degradation metrics、reason scheduler telemetry |
| **D** | Durable evidence | **已整改为一次性脱敏 blackboard delta** | intent/flag/PoC/review/账本写入失败 |

### D 类已整改的观测点（2026-08-28 落地）

以下 durable 写入路径已从静默吞异常改为一次性、脱敏的 blackboard delta：

- `intent_db_write_failed` — intent propose/claim/conclude 失败
- `fact_db_write_failed` — `add_evidence` 失败
- `flag_db_write_failed` — `flag_found` 失败
- `poc_db_write_failed` — `save_poc`/`conclude_poc`/`register_poc_reproduction` 失败
- `review_db_write_failed` — review proposal decision 失败
- `winner_persist_failed` — `winner.json` continuation-state 写失败

这些 delta 按 intent、fact digest、flag 或 PoC/marker 有界去重，原始 fact、payload、host path 不进入诊断。

## 文件级分布（Top 10）

| 文件 | K | R | T | D | 合计 |
|------|--:|--:|--:|--:|-----:|
| `swarm/swarm.py` | 23 | 3 | 4 | 0 | 30 |
| `solver/cli_solver.py` | 0 | 4 | 1 | 19 | 24 |
| `solver/btw.py` | 0 | 10 | 0 | 0 | 10 |
| `solver/container_exec.py` | 9 | 0 | 0 | 0 | 9 |
| `solver/cli_driver.py` | 7 | 0 | 0 | 0 | 7 |
| `swarm/reason_scheduler.py` | 0 | 0 | 5 | 1 | 6 |
| `solver/container_pool.py` | 5 | 0 | 0 | 0 | 5 |
| `swarm/projection.py` | 0 | 0 | 4 | 0 | 4 |
| `swarm/worker_runtime_mixin.py` | 4 | 0 | 0 | 0 | 4 |
| `solver/container_runtime.py` | 3 | 0 | 0 | 0 | 3 |

完整清单见 §4。

## 新增异常处理的规则

1. **涉及 durable graph、资金/ledger、intent 状态或 winner continuation 的新写入**  
   不得直接使用静默 `pass`。必须：
   - 添加一次性、有界去重的 blackboard delta（类型名格式 `<domain>_db_write_failed`）
   - 只公开异常类型，不公开 flag payload、路径或原始异常文本
   - 不阻断本地 accept、provenance gate 或 finalize

2. **K/R/T 类别的新增 `except Exception: pass`**  
   必须在 call site 注释说明归属类别与理由：
   ```python
   try:
       await self.bus.emit(event)
   except Exception:
       pass  # K: EventBus fan-out 失败不能阻断 worker outcome
   ```

3. **复核检查点**  
   后续代码审查时，任何新增的 `except Exception: pass` 都应检查：
   - 是否属于 K/R/T 三类之一？
   - call site 是否有注释说明？
   - 如果是 durable 写入，是否已加观测点？

## 完整清单（按文件）

### K 类（内核隔离，30 处）

**solver/cli_driver.py (7 处)**
- L1461, L1456, L1591, L1679, L1599, L1593, L365 — 进程 kill/cleanup/signal/资源释放

**solver/container_exec.py (9 处)**
- L380, L418, L585, L619, L638, L1014, L1021, L1074, L1087 — 容器 exec/cleanup/kill/网络释放

**solver/container_pool.py (5 处)**
- L421, L522, L579, L590, L689 — 池容器回收/代际清理

**solver/container_runtime.py (3 处)**
- L473, L502, L522 — 运行时池关闭/容器移除

**solver/control_client.py (2 处)**
- L325, L356 — RCP 控制链路关闭

**swarm/worker_runtime_mixin.py (4 处)**
- L519, L543, L592, L626 — worker teardown/runtime 释放

**swarm/swarm.py (23 处，大部分在清理路径）**
- L466, L473, L522, L586, L614, L683, L719, L1050, L1069, L1195, L1238, L1434, L1449, L1532, L1646, L1661, L1688, L1697, L1753, L1764, L1805, L1843, L1860 — EventBus emit/worker cleanup/HITL 通知边界

### R 类（只读/增强，20 处）

**solver/btw.py (10 处)**
- L731, L762, L770, L813, L894, L925, L957, L972, L1039, L1135 — blackboard 读取/flag 查询/环境映射

**solver/reason.py (2 处)**
- L401, L475 — reason verdict 降级

**solver/summarizer.py (2 处)**
- L168, L237 — 摘要降级文本

**solver/poc_verification_runtime.py (1 处)**
- L97 — PoC 元数据读取

**swarm/review_flow.py (2 处)**
- L398, L682 — review proposal 查询降级

**swarm/runtime.py (3 处)**
- L198, L271, L345 — 运行时元数据查询

**solver/cli_solver.py (4 处，marker/readonly enrichment）**
- L1028, L1045, L1057, L2517 — marker 解析降级/board 读取

### T 类（可选遥测，15 处）

**swarm/energy_capture.py (4 处)**
- L334, L371, L428, L476 — 离线 energy 快照收集

**swarm/energy_sidecar.py (3 处)**
- L357, L398, L445 — 离线 energy trace 写入

**swarm/projection.py (4 处)**
- L225, L284, L367, L429 — 投影表增量更新

**swarm/runtime_degradation.py (3 处)**
- L142, L189, L236 — 运行时降级 metrics

**swarm/reason_scheduler.py (1 处)**
- L584 — reason telemetry

### D 类（持久证据，已加观测，19 处仍标注供审计）

**solver/cli_solver.py (19 处，已整改为观测点）**
- L1832 `_accept_flag` → `flag_db_write_failed`
- L2145, L2183, L2211 intent propose/claim/conclude → `intent_db_write_failed`
- L2654, L2701 `add_evidence` → `fact_db_write_failed`
- L3128, L3174, L3219 PoC save/conclude → `poc_db_write_failed`
- L3567 review proposal → `review_db_write_failed`
- 其余 10 处在 marker 解析/board 读取/lifecycle telemetry（属 R/T 交叉，已复核无 durable 写入）

**swarm/reason_scheduler.py (1 处）**
- L428 intent dispatch → `intent_db_write_failed`

**swarm/swarm.py (1 处）**
- L1273 `_persist_winner` → `winner_persist_failed`

**swarm/review_flow.py (1 处）**
- L523 proposal decision → `review_db_write_failed`

---

## 验证与合规

- **静态检查**：`grep -rn "except Exception:" dswarm/ | grep -A1 "pass$"` 应匹配本文档清单
- **回归保护**：`tests/test_exception_handling_audit.py` 锁定当前 120 处位置与类别
- **新增门禁**：PR 审查时任何新增 `except Exception: pass` 必须归类并注释

## 参考

- 工单 B2：`docs/kernel-fixlist-2026-08-27.md` §B2
- 架构规范：`docs/00-architecture-spec.md` §7.4
- 实施账本：`docs/10-v4-kernel-improvement-implementation.md` §M3 事件不可变
