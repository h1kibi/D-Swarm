# D-Swarm 内核问题工单（外部交接版）

> **日期**：2026-08-27 · **来源**：全项目质检 + M9 审查 + 完成判定接线工程的真实发现
> **接收人须知**：每条问题含【证据】【影响】【修复方向】【验收标准】。行号为快照参考，
> 以符号名检索为准。**一次只做一个 Issue，单独提交、独立通过全量测试。**
> 入职阅读顺序：`AGENTS.md` → `docs/00-architecture-spec.md` → 本文件对应条目。

## ⛔ 全局红线（开工前必读，违反即返工）

1. **永不修改 `dswarm/solver/gate.py` 的判定逻辑**，以及 `cli_solver` 反洗白检查。
   flag 接受权是硬编码门，不是可插拔 verifier。
2. **共享图事件流 append-only**：任何新信息走新增事件种类 + 可重建投影，
   禁止原地覆写或让投影成为事实源。
3. **黑盒纪律**：solver 只见活靶/题面/玩家文件；不得把 solution/writeup 送进语料。
4. **测试定义完成**：`uv run pytest -q` 全绿 + 新行为有确定性测试。
   涉及解题率主张的改动必须有真实黑盒 trace 支撑（见 model claim 无效）。
5. 建议（非强制）先读 `docs/archive/11…19` 的 RFC 链感受评审深度——重大语义变更请走 RFC。

---

## A 类 · 功能收口（P1，阻碍 pentest 线交付）

### A1. scope_audit 已实现但从未接线（零生产调用方）

- **现象**：`dswarm/swarm/scope_audit.py` 是纯函数模块（解析
  `Challenge.scope` 白名单 → 扫 provenance 语料 → 返回越界 host violation）。
  全仓生产代码无一处调用，唯一 importer 是它自己的测试。
- **证据**：`grep -rln scope_audit dswarm/ apps/` 仅命中该文件本身；
  `docs/10 §M9` 表格宣称已交付该项（账本状态行也只保 Verified-PoC 子句）。
- **影响**：pentest 模式承诺的"越界资产事后审计"实际不存在——UI 若对接会画空饼。
- **修复方向**：
  1. 在 review 流程周期（`review_flow.py` 的 drain 循环或一个低频定时钩子）以当前
     provenance corpus 调用 `scope_violations()`；
  2. 命中后经 `shared_graph.add_review_finding(kind="scope_violation",
     severity="high", summary=..., poc_id="")` 走既有去重键（kind::summary::route）
     防止重复刷屏；
  3. 无 scope 定义时不扫描（避免把私有地址规则外的公网 IP 全打成违规——
     见 `scope_audit.scope_violations` 文档字符串约定）。
- **验收标准**：新增确定性测试覆盖三态（白名单命中 / 越界 / 无 scope 不扫描）；
  violation finding 进图且被 bridge 正常转发为 `review_finding` 公开增量；
  全量回归绿。

### A2. cleanup registry 未落地（M9 四件套最后一项）

- **现象**：`docs/10 §M9` 计划了"`CLEANUP=<cmd>` 标记 → `cleanup_actions`
  表 → wind-down（`_finalize_coordinator_run`）逆序执行 + 报告清单"，但
  `skills/dswarm-blackboard/blackboard.py` 与 `dswarm/swarm/blackboard_skill.py`
  中 `CLEANUP` 命中数为 **0**，无对应表、无执行器。
- **影响**：pentest 收尾阶段无法自动还原现场（掉落的隧道/监听器/临时凭据），
  报告也无法给出"已清理动作"清单。
- **决策请求**：若决定**不做**，请在 `docs/10 §M9` 状态行显式降级并从
  README 能力表中移除相应措辞（二选一，不许保持半悬空）。
- **若实施的修复方向**：
  1. blackboard skill 增解析 `CLEANUP=<command>`（worker 输出侧已有
     `POC_SAVE=` 同型正则先例，`cli_solver.py` `_EXTRACT_POC_SAVES` 一族）；
  2. 新增 events 种类 + `cleanup_actions` 投影表（复用 `poc_reproductions` 的
     fold-rebuild 模式），记录 actor/poc/intent 关联与 command（命令文本仅存图内，
     公开增量只给 digest/截断——遵循 Verified-PoC 的脱敏边界）;
  3. `_finalize_coordinator_run` 在 release_claims_for_finalize 前逆序执行
     （失败逐条记 `cleanup_failed`，绝不阻断 finalize 主路径）；
  4. 执行容器与租约：直接用当次 run 已冻结的池代际，禁止新建 Docker 连接通道。
- **验收标准**：注册→finalize 执行→报告清单事件的端到端测试（ScriptedLLM 模式）；
  失败不阻断断言；bridge 公开字段脱敏断言（参照
  `tests/test_verified_poc_compatibility.py` 手法）。

### E1.（随 A 类顺带确认）上游 0.2.4 补丁现状

- `docs/08 §5.8-4` 记录"custom-endpoint 健康检查跑真实 CLI 回合"为待合并项。
  已核实 **0.2.5（worker 镜像 UID/GID 探测+chown）本仓已自有实现**
  （`container_exec.py` `_query_worker_uid_gid` 一族）。请核对 0.2.4 的
  EndpointDriver hello 机制是否已被自有实现覆盖，并把结论回写到 `docs/10 §M9`，
  让账本与代码一致。

---

## B 类 · 可靠性与可观测性（P1–P2）

### B1. `_accept_flag` 的共享图写入仍为完全静默

- **现象**：`cli_solver.py` `_accept_flag` 内
  `shared_graph.flag_found(...)` 包在 `except Exception: pass`。
  完成判定失明的风险已由 `_sync_flags_from_graph` 接线补偿（2026-08-27,
  commit `4596b76`），所以**不再导致判错**，但丢失依旧是不可见的。
- **修复方向**：仿照同日提交 `d49a4a8`（`_note_intent_db_failure` /
  `intent_db_write_failed` 有限去重广播）加一次性 `flag_db_write_failed`
  观测点。注意保持"绝不扰动 accept 主流程"、同一 worker 生命周期内去重。
- **验收标准**：伪图抛错 →恰一条 bb delta 且 flag 流程继续；多次失败幂等单发。

### B2. 静默吞异常的全量排查与分级处置

- **现象**：内核约 174 处 2 行内吞掉的 `except Exception`。其中合理的
  （杀进程级联、best-effort 清理、log-guard）应保留；需要整治的是**涉及
  证据/资金/状态机写入**却被无声丢弃的点。
- **起手指令**：
  `grep -rn -A1 "except Exception:" dswarm/ | grep -B1 "^\s*pass"`
  重点目录：`container_exec.py`(12/23)、`btw.py`(11/22)、`swarm.py`(32/51)、
  `cli_driver.py`(7/31)。产出物是一份《处置清单》注释到各 call site（保留类打标
  `# best-effort ok`，整改类按 B1 模式接观测），而非一次性大改。
- **验收标准**：清单文档化（可附 PR 描述）；抽查任一"保留"类别归属正确。

### B3. `_record_runtime_degraded` 已测未接线

- **现象**：`runtime_degradation.py:69` 的运行时降级登记方法全仓无调用方
  （配套单测于 commit `023c490` 已存在）。降级信息目前只有 `_note_engine_*`
  引擎健康粒度，池代际回退粒度无叙事。
- **修复方向**：在 `worker_runtime_mixin._handle_runtime_failure` 决定回退后端时
  调用它；payload 已含 requested/fallback backend 与 reason 截断。
- **验收标准**：fake executor 触发 failover → `_runtime_degraded` 收录 +
  `runtime_degraded` bb delta 发射一次；`_runtime_metadata_for(outcome)`
  的 backend 翻转反映真实结果（既有测试形态）。

### B4. 未知事件种类的"契约冻结"前置

- **现象**：本月新增 13+ 种 bb delta 无前端消费者（列举：
  `flag_reaccept_blocked`、`ready_to_submit`、`claim_solved_rejected`、
  `worker_runtime_error`、`lane_locked/released/revived`、
  `review_proposal_decision`、`system_notice`、`direction_override`、
  `help_dismissed`、`worker_health_check` 等）。这不是要内核删功能，
  而是**事件词汇表已到了该立契的阶段**。
- **行动建议**：起草 mini-RFC《UI 事件契约 v1》（参照 archive/11–19 格式）：
  逐一裁决上述 kind 是进契约、标记 experimental 还是移除发射；payload 字段
  schema 化；此后新增 kind 必须改契约再发布。这是 UI 对接的前置条件，
  归档为本文件的姊妹任务，不必由内核修复人独立完成。

---

## C 类 · 结构债（P2，逐个立项、禁止顺手动）

| # | 问题 | 证据 | 处置路线 |
|---|---|---|---|
| C1 | `worker_runtime_mixin ↔ swarm` 循环依赖：`:284` 函数内延迟 import 一次性搬运 **10 个下划线私有符号** | worker_runtime_mixin.py:284 | 把被搬运者下沉为叶子模块（如 `swarm/_bootstrap_assets.py`），mixin 与 Swarm 都只向上引用叶子 |
| C2 | 超大文件：`shared_graph.py` ≈5153 行 / `cli_solver.py` ≈4350 行 | 当前 HEAD | 建议按域拆分：图生命周期域（poc/budget/energy 已各自成形）外置为 mixin-free 模块；marker 解析器独立成 parser 模块。**拆分前先冻结行为快照测试** |
| C3 | normalize/sanitize helper 繁殖：`normalize_operator_direction` 在 `direction_rules.py:168` 与 `apps/web/drivers.py:59` 两套同义不同体；`worker_profiles` 6 个 + `shared_graph` 5 个同类 | 各文件 | 统一入口收敛至公共叶子模块，web 侧重定向 import；受 `test_m4_operator_direction.py` 保护 |
| C4 | ~54 条事故编码注释（BUG①②③/run-ID/`鈥?` 型历史标签）承载回归知识 | `grep -rn "BUG①\|BUG②\|BUG③\|run-[0-9]" dswarm/ | wc` | 沉淀进 docs 或改名带案号的规范注释（例如 `// regression: run-75379 …`），建立索引表防止随重构失联 |

---

## D 类 · 平台卫生（P3，可碎片化处理）

### D1. 死代码四方块（孤儿验证后再删）

- `dswarm/solver/btw.py` ≈:1126 `sse_frame()` —— 与 `core/events.py` 的 SSE 序列化重复；
  兄弟函数 `_HEALTH_TTL`/`_health_cache` 仅被死函数使用
- `dswarm/solver/cli_driver.py` ≈:1159 `engine_liveness()`（apps/web 自有健康通道
  `driver.health_detail()`）
- `dswarm/swarm/board.py` ≈:479 `new_finding_id()`
- `dswarm/swarm/swarm.py` ≈:356 `_is_control_failure()`

**验收**：删除前 `grep -rn <symbol> --include='*.py' .`（含 tests/apps/scripts）确认零引用；
每删一类跑一次全量。

### D2. Windows 开发宿主的两处已知差异

1. token/key 文件 `chmod 0o600/0o700` 组位在 Windows 上无效（位置：
   `credential_accounts.py` 数处、`container_exec.py` token 落盘、
   `runtime_snapshot.py` runtime dir）。生产目标是 Linux 容器侧不受影响；
   最低要求：在这些 call site 加一行注释注明"dev-host 上弱隔离属预期"，
   并在 SECURITY.md 或 runtime-pools.md 各留一句说明。
2. 调试路径 `/tmp/dswarm_container_diag`（`container_exec.py`，debug env 触发）
   硬编码 POSIX tmp → 改 `tempfile.gettempdir()` 一致化。

### D3. 一个已定性 Windows 冒烟偶发

- `tests/test_secret_store.py::test_atomic_write_replaces_existing_file`
  在全量套件中偶发 `PermissionError`（毫秒级时间戳临时名 + `Path.replace`
  撞上杀软/索引器瞬时锁；单独跑必过）。
- **修复方向**：`atomic_write` 对 `tmp.replace(path)` 捕获 `PermissionError`
  后短暂退避重试一次（≤50ms），不吞第二次异常；严禁改成宽泛重试循环掩盖真问题。
- **验收**：本地反复全量 3 轮无该 flake；Linux CI 行为不变。

---

## E 类 · 明确不在本工单范围（防跑偏）

- UI 五批次改进方案（Batch 1–5）：另有交接，其中"unknown-kind 兜底灰线"等三项
  与内核正交可在 B4 契约冻结前先行。
- CHANGELOG Unreleased 区、push origin/main 的节奏决定权在维护者本人。
- eval / benchmark 生产线（NYU 等）：除 B2 排查可能波及外不动。
- 事故知识已在 `session-handoff.md` 有流水账；完成后请在该文件留一行去向说明。

## 建议的施工顺序

```
A1 scope 接线 → A2 cleanup 落地/降级决策 → B1+B3（小而硬） → D1/D3（热身兼熟悉仓）
→ B2 排查清单（产出物为主） → C1 解环 → C2 拆分（独立大项，先立 RFC） → C3/C4
```

每完成一个 Issue：更新 `docs/10` 相应状态行（若涉及账本宣告的能力），并在
本文档该条目后追加一行 `✅ closed in <commit>` —— 本文件同时充当验收台账。
