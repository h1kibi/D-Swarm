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

- ✅ **A1 closed (2026-08-27)**：scope audit 已接入 pentest 收尾路径；审计仅读取 SharedGraph 有效事实，越界 finding 会持久化并 bridge 到 `review_finding`，无 scope 不扫描。针对性测试与 `uv run pytest -q` 均通过。

### A2. cleanup registry 已落地（M9 四件套最后一项）

- **核实结论（2026-08-27）**：安全版本已实现并接入生产收尾路径。实现不是原始草案中的任意命令执行，而是 typed cleanup registry；worker 只能注册四类动作：`remove_artifact`、`stop_listener`、`close_session`、`revoke_credential`。
- **事件与投影**：`dswarm/swarm/shared_graph.py` 增加 `cleanup_action_registered`、`cleanup_executed`、`cleanup_failed` 事件及可由事件流重建的 `cleanup_actions` 投影，注册按 action ID/idempotency key 幂等。
- **执行边界**：`remove_artifact` 仅允许 run-relative 的 `workers/...` 文件，并由 coordinator 直接执行受限 unlink；其余动作必须由 run owner 注入已授权 runtime adapter。没有 adapter 时记录失败，绝不 fallback 到宿主 shell、Docker shell 或 raw command。
- **收尾语义**：`_finalize_coordinator_run` 在释放 claims 前按注册顺序逆序执行；单条失败追加 `cleanup_failed`，不阻断后续动作、claims release、graph drain 或 run close。重复/缺失文件按幂等语义处理。
- **脱敏与黑板**：worker-facing blackboard 和 bridge 只公开 action type、ID、target digest/长度；原始 target 仅保存在 run-scoped graph 内。非法 marker（包括 `CLEANUP=rm -rf ...`、绝对路径和 `..`）被拒绝。
- **验证**：新增确定性测试覆盖 typed marker 校验、graph append-only/rebuild/idempotency、CLI 注册脱敏、逆序执行、失败隔离和 artifact 路径边界；`uv run pytest -q tests/test_cleanup_registry.py` 通过。

### E1.（随 A 类顺带确认）上游 0.2.4 补丁现状

- `docs/08 §5.8-4` 记录"custom-endpoint 健康检查跑真实 CLI 回合"为待合并项。
  已核实 **0.2.5（worker 镜像 UID/GID 探测+chown）本仓已自有实现**
  （`container_exec.py` `_query_worker_uid_gid` 一族）。请核对 0.2.4 的
  EndpointDriver hello 机制是否已被自有实现覆盖，并把结论回写到 `docs/10 §M9`，
  让账本与代码一致。

- ✅ **E1 closed (2026-08-28)**：已核实上游 0.2.4 是 Codex 专用修复，不是当前 pi-only 内核必须恢复的通用 CLI hello 路径。D-Swarm 的 `worker_config.py` 已把 endpoint account 的 `base_url` overlay 到 profile；`EndpointDriver._hello_argv()` 对 endpoint 明确不走 base CLI；profile readiness 由 `EndpointDriver.health_detail()` 调用 `probe_endpoint(validate_model=True)`，先做 `/models` 可达/认证/发现，再对实际 profile model 发配置协议或 auto fallback 的 Chat/Responses 请求，因此覆盖了开跑前的认证、协议与模型/schema 预检语义。
- `apps/web/account_test.py` 的 custom-endpoint 账号测试仍保持有意的 model-agnostic direct HTTP probe：它验证账号级 base_url + key 可达，不等同于带 pinned model 的 profile readiness；两条路径均不恢复 Race/Coordinator 或 Codex 兼容层。0.2.5 的 worker 镜像 UID/GID 探测+chown 也已核实为本仓自有实现。
- 验证：`tests/test_worker_endpoint.py`、`tests/test_cli_executor.py`、`tests/test_connectivity_probes.py`、`tests/test_worker_config.py` endpoint 相关测试通过；`uv run pytest -q` 全量通过。

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

- ✅ **B1 closed (2026-08-28)**：`_accept_flag` 对
  `shared_graph.flag_found` 的失败现已通过有界、去重的 `flag_db_write_failed` blackboard
  delta 暴露；异常只公开类型，不携带 flag payload、路径或原始异常文本，且不阻断本地
  accept、provenance gate 或 finalize。确定性失败/去重测试与全量回归通过。

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

### B2 disposition ledger (2026-08-28)

- ✅ **Closed (2026-08-28)**：本轮 AST 复核得到 **125 个** `except
  Exception` + `pass` call site；其中 5 个证据/状态写入点已从静默吞异常改为一次性、
  脱敏的 blackboard delta，剩余 **125 个** 均属于下列明确的 best-effort 类别。
- **已整改（D，durable evidence/state）**：
  - `dswarm/solver/cli_solver.py`：intent propose/claim/conclude、`add_evidence`、
    `flag_found`、`save_poc`/`conclude_poc`、review proposal 写入；
  - `dswarm/swarm/reason_scheduler.py`：Reason dispatch 的 intent registration。
  这些路径分别发出 `intent_db_write_failed`、`fact_db_write_failed`、
  `flag_db_write_failed`、`poc_db_write_failed`、`review_db_write_failed`；按 intent、
  fact digest、flag 或 PoC/marker 有界去重，原始 fact、payload、host path 不进入诊断。
  直接 conclude 路径也统一复用 `_conclude_intent_db`，避免回归静默写入。
- **本轮继续核实/收口（2026-08-28）**：
  - `dswarm/swarm/swarm.py::_persist_winner` 的 `winner.json` continuation-state
    写失败现发出一次 `winner_persist_failed(op=winner_json)`；只公开异常类型，
    不公开 payload 或 host path，且不阻断 solved finalize。
  - advisory `_run_reason` 的 `pin_facts` 与 intent dispatch 写失败分别通过
    `fact_db_write_failed(op=pin_facts)` / `intent_db_write_failed(op=propose)`
    有界观测；live scheduler 路径仍是唯一正式 dispatch 路径，不恢复旧 Race /
    Coordinator 模式。
  - `review_flow` 的 proposal decision fallback 若自身 append 失败，现发出一次
    `review_db_write_failed(op=decide_review_proposal)`；`cli_solver` 的
    `register_poc_reproduction` 失败复用 `poc_db_write_failed`，并将 rejection
    诊断限制为异常类型。
- **保留（K，kernel isolation）**：
  `dswarm/core/event_bus.py`；`dswarm/solver/cli_driver.py`、`container_exec.py`、
  `container_pool.py`、`container_runtime.py`、`control_client.py`；
  `dswarm/swarm/swarm.py` 的 bus/finalize/HITL/worker teardown；
  `dswarm/swarm/worker_runtime_mixin.py`。这些异常发生在 sink/fan-out、进程/容器回收、
  取消、运行时释放或前端通知边界；升级会遮蔽真实 worker outcome 或造成资源泄漏。
- **保留（R，readonly/enrichment）**：
  `dswarm/solver/btw.py`、`reason.py`、`summarizer.py`、`poc_verification_runtime.py`、
  `review_flow.py`、`runtime.py`；以及 `cli_solver.py` 中的 board/flag 读取、环境映射、
  marker/readonly enrichment。失败时已有空结果、降级文本或跳过语义，不写入事实或改变
  provenance gate。
- **保留（T，optional telemetry）**：
  `dswarm/swarm/energy_capture.py`、`energy_sidecar.py`、`projection.py`、
  `runtime_degradation.py`、`shared_graph.py` route metrics、`reason_scheduler.py`
  的 bus/provider/energy telemetry，以及 `cli_solver.py` 的 lifecycle/cost/InsightBus
  旁路。它们不能改变调度、flag acceptance、append-only graph 或 finalize。
- **复核方式**：后续新增 `except Exception: pass` 必须归入 K/R/T 之一并在 call site
  说明原因；涉及 durable graph、资金/ledger、intent 状态或 winner continuation 的新写入
  不得直接使用静默 `pass`。

### B3. `_record_runtime_degraded` 已接线

- ✅ **2026-08-27 closed in working tree (committed as one reviewed batch)**：runtime pool failover 在选出冻结候选后调用 `_record_runtime_degraded`，记录 requested/fallback backend 与截断后的 failure category/code；观测路径为 best-effort，不改变 failover、flag 或 finalize 主流程。
- **可见性**：记录会追加 `_runtime_degraded`，并通过 `runtime_degraded` blackboard delta 暴露一次；`_runtime_metadata_for(outcome)` 按实际 outcome backend 生成元数据，不再把所有降级错误地标成 `local`。
- **验证**：新增 deterministic fake-runtime failover 测试，覆盖记录、blackboard delta 与 container-to-container fallback 的真实 backend 元数据；`tests/test_runtime_failover.py` 与 `tests/test_runtime_degradation.py` 通过。

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

- ✅ **B4 closed (2026-08-28)**：新增
  `docs/ui-event-contract-v1.md`，冻结 envelope、`blackboard.delta` 最低契约与
  stable/experimental/replay-only 分类。UI reducer 对未知、缺失或非字符串 kind
  增加 generic timeline fallback：可见但 inert，不修改 typed blackboard state、调度、
  证据图或 flag/provenance；新增 `apps/web/ui/test/events-contract.test.ts` 覆盖未知
  与 malformed kind 的 replay 回归。

---

## C 类 · 结构债（P2，逐个立项、禁止顺手动）

| # | 问题 | 证据 | 处置路线 |
|---|---|---|---|
| C1 | `worker_runtime_mixin ↔ swarm` 循环依赖：`:284` 函数内延迟 import 一次性搬运 **10 个下划线私有符号** | worker_runtime_mixin.py:284 | 把被搬运者下沉为叶子模块（如 `swarm/_bootstrap_assets.py`），mixin 与 Swarm 都只向上引用叶子 |
| C2 | 超大文件：`shared_graph.py` ≈5153 行 / `cli_solver.py` ≈4350 行 | 当前 HEAD | 建议按域拆分：图生命周期域（poc/budget/energy 已各自成形）外置为 mixin-free 模块；marker 解析器独立成 parser 模块。**拆分前先冻结行为快照测试** |
| C3 | normalize/sanitize helper 繁殖：`normalize_operator_direction` 在 `direction_rules.py:168` 与 `apps/web/drivers.py:59` 两套同义不同体；`worker_profiles` 6 个 + `shared_graph` 5 个同类 | 各文件 | 统一入口收敛至公共叶子模块，web 侧重定向 import；受 `test_m4_operator_direction.py` 保护 |

- ✅ **C3 closed (2026-08-28)**：新增无业务依赖的
  `dswarm/core/normalization.py` 公共叶子模块，集中维护文本、方向原文、fact identity、
  route/lane/resource key 与 lane host 的确定性归一化。`apps/web/drivers.py` 删除本地
  `normalize_operator_direction` 包装，直接使用 `direction_rules` 的唯一实现；
  `shared_graph.py` 保留兼容方法但转发至公共实现，`worker_profiles.py` 统一使用
  `clean_text`。新增 M4 回归断言锁定单一入口与 graph wrapper 委托关系。
| C4 | ~54 条事故编码注释（BUG①②③/run-ID/`鈥?` 型历史标签）承载回归知识 | `grep -rn "BUG①\|BUG②\|BUG③\|run-[0-9]" dswarm/ | wc` | 沉淀进 docs 或改名带案号的规范注释（例如 `// regression: run-75379 …`），建立索引表防止随重构失联 |

- ✅ **C4 closed (2026-08-28)**：新增 `docs/regression-index.md`，将当前 `dswarm/**/*.py` 中的 29 个 run-ID（102 处引用）和独立 BUG-1/2/3/4 标签归档为稳定回归索引，明确它们不是运行模式、兼容注册表或 flag 来源。`tests/test_regression_index.py` 以源码扫描对索引做等集断言，防止后续拆分遗失历史回归锚点。

- ✅ **C1 closed (2026-08-28)**：新增无 Swarm 依赖的
  `dswarm/swarm/_bootstrap_assets.py` 叶子模块，承载 worker HOME、pi 配置、方向
  skill 与 blackboard link 的 bootstrap helper。`worker_runtime_mixin.py` 改为模块级
  依赖该叶子模块，移除运行时对 `dswarm.swarm.swarm` 的延迟私有导入；`swarm.py`
  仅保留兼容导出，既不恢复 Race/Coordinator，也不改变正式调度路径。
- **验证**：新增架构回归测试锁定 mixin 不得反向导入 `swarm.py`；helper 兼容测试、
  `tests/test_architecture.py`、`tests/test_swarm.py` 与 `tests/test_external_skills.py`
  通过。

- 🔧 **C2 marker-parser slice closed (2026-08-28)**：将
  `cli_solver.py` 的纯 worker-marker 正则与解析函数下沉至无 solver-state 依赖的
  `dswarm/solver/marker_parser.py`；`CliSolver` 保留同名薄 wrapper，保持既有调用面、
  provenance/gate 路径与 cleanup/review 处理不变。新增
  `tests/test_marker_parser_snapshot.py` 冻结 FOUND_FLAG、fact/finding、NEED_INPUT、
  READY_TO_SUBMIT、PoC、CLEANUP 与 raw-flag guard 的行为；solver 其余状态域仍保留在
  `CliSolver`，避免为拆分而改变 provenance/gate 边界。

- 🔧 **C2 event-reader slice closed (2026-08-28)**：将
  `events`、`recent_events`、`events_since` 与异步 `subscribe_events` 的只读查询/轮询
  逻辑下沉到无 `shared_graph` 反向依赖的 `dswarm/swarm/event_reader.py`。
  `SQLiteSharedGraph` 仅保留兼容 wrapper；append-only 写入、challenge 过滤、kind 过滤、
  顺序与轮询语义均由 `tests/test_shared_graph_event_reader_snapshot.py` 冻结。

- ✅ **C2 closed (2026-08-28)**：C2 的可安全外置边界已
  收口为 POC lifecycle、worker marker parser 与 event reader 三个无运行模式依赖的叶子域；
  token budget (`budget.py`) 与 offline energy (`energy.py`) 原本已是独立模块。核心事实/意图
  物化与 SQLite 事务仍留在 `shared_graph.py`，避免为了缩短文件而引入第二事实源或破坏
  append-only 语义。以上切片均有确定性快照测试，全量 pytest 已通过。

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

- ✅ **D1 closed (2026-08-28)**：已确认当前仓库（含
  `tests/`、`apps/`、`scripts/`）不再引用 `sse_frame`、`engine_liveness`、`new_finding_id`
  或 `_is_control_failure`；SSE 序列化继续使用 `Event.to_sse()`，engine health 继续走
  现有 health endpoint，相关回归测试通过。

### D2. Windows 开发宿主的两处已知差异

1. token/key 文件 `chmod 0o600/0o700` 组位在 Windows 上无效（位置：
   `credential_accounts.py` 数处、`container_exec.py` token 落盘、
   `runtime_snapshot.py` runtime dir）。生产目标是 Linux 容器侧不受影响；
   最低要求：在这些 call site 加一行注释注明"dev-host 上弱隔离属预期"，
   并在 SECURITY.md 或 runtime-pools.md 各留一句说明。
2. 调试路径 `/tmp/dswarm_container_diag`（`container_exec.py`，debug env 触发）
   硬编码 POSIX tmp → 改 `tempfile.gettempdir()` 一致化。

- ✅ **D2 closed (2026-08-28)**：已在凭据存储、
  runtime credential projection、runtime snapshot/diagnostics 以及 RCP token
  staging 的 `chmod(0o600/0o700)` call site 明确注明：native Windows 上权限位仅
  best-effort，不能提供 POSIX owner-only isolation；生产安全边界仍是 Docker/Linux
  runtime，不能把 Windows host staging 当成隔离。`container_exec.py` 的 debug
  日志目录已使用 `tempfile.gettempdir()`，不再硬编码 `/tmp`。
- **验证**：Windows 兼容性说明已同步至 `SECURITY.md` 与 `docs/runtime-pools.md`；
  定向 runtime/credential/container 测试与 `uv run pytest -q` 通过。

### D3. 一个已定性 Windows 冒烟偶发

- `tests/test_secret_store.py::test_atomic_write_replaces_existing_file`
  在全量套件中偶发 `PermissionError`（毫秒级时间戳临时名 + `Path.replace`
  撞上杀软/索引器瞬时锁；单独跑必过）。
- **修复方向**：`atomic_write` 对 `tmp.replace(path)` 捕获 `PermissionError`
  后短暂退避重试一次（≤50ms），不吞第二次异常；严禁改成宽泛重试循环掩盖真问题。
- **验收**：本地反复全量 3 轮无该 flake；Linux CI 行为不变。

- ✅ **D3 closed (2026-08-28)**：`atomic_write` 仅对
  `tmp.replace(path)` 的 `PermissionError` 做一次 10ms 短暂退避重试，第二次异常继续抛出；
  定向 `tests/test_secret_store.py` 已连续 3 轮通过，且覆盖重试次数与退避行为的确定性测试。

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

---

## 2026-08-28 追加（外部使用中发现的接线缺口，已闭环）

### F1. web 派工路径从未冻结 M9a 运行时策略（run-4408 静默阵亡）

- **现象**：web 指挥台派发的容器后端 run，所有 spawn 被 `runtime_policy_required`
  fail-closed 击毙，run 无 worker 空转约 1 分钟后 unsolved 结束；界面骨架屏等不到
  `RUN_STARTED`。`build_docker_runtime_context` 全仓仅 TUI 调用，
  `RunManager.ensure_runtime_context` 在 web 启动路径零调用方（`git log -S` 证实
  自 M9a 落地起从未接线——非 111 文件批次引入）。
- **修复（✅ closed 2026-08-28）**：`RunManager.start` 在派发前调用新的
  `_freeze_dispatch_runtime`：按与派工路径完全相同的解析顺序（body > worker_config >
  env，含 offline 网络钳制）构建 `build_runtime_policy`，容器后端走 docker 冻结
  （镜像预检失败 => POST /start 直接 400 `image_resolution_failed`），本地后端走
  双门禁（启动请求显式 `local_dev` + `DSWARM_ALLOW_LOCAL_WORKERS=1`）；
  `RunManager` 默认装配真实 `RuntimeSnapshotBuilder` 与
  `runtime_factory.build_pool_manager_for_run` 池组合（从 TUI 路径抽取共用，行为不变，
  `tests/test_runtime_factory.py` 5/5 绿）；mock/idle driver 跳过冻结。
- **验收**：`tests/test_web_launch_runtime.py` 5 项端到端（真实 HTTP 路由）：容器冻结
  +快照落盘+池组合+网络钳制 / 本地双门禁两态 / 镜像缺失 400 且 run 不再静默死亡 /
  重复派发幂等（同一快照对象）/ mock 跳过。全量 pytest exit=0。

### F2. 身份模型仍 fabricated 已回收的 0.2.0 方向镜像（2026-08-28，✅ closed）

- **现象**：新架构 seats/environments 不携带 image；`identity_model._seat_direction_image`
  按 seat 方向从 `_DIRECTION_IMAGE_TAG` 硬编码拼出 `ctf-swarm-pi-<dir>:0.2.0`。
  该镜像族已废弃回收后，任何新派工都会在镜像预检处 400。
- **修复**：统一镜像教义落地——`DEFAULT_WORKER_IMAGE` = M9a tag
  （`DSWARM_WORKER_IMAGE` env 可覆盖），`direction_image()` 一律解析到统一镜像；
  `worker_config`/`worker_image` 钉死旧 tag 的测试更新为统一契约。全量 exit=0。

### F3. 运行时身份链残留两处 `kali` 用户硬编码（2026-08-28，✅ closed）

- **现象**：M9a 镜像的 worker 用户是 `ctf`（Dockerfile useradd 1000:1000，`Config.User="ctf"`），
  但 ①快照预检 `DockerImageInspector.resolve` 只探测 `kali`（`id -u kali` 必败 →
  `worker_identity_mismatch`，冻结阶段全灭）；②池容器身份证明
  `container_runtime._container_identity` 执行 `docker exec id -u kali`（探针失败回退
  uid=0 → `runtime_identity_mismatch`，池代际永不就绪）。二者的合流表象是冒烟
  8/8 "completed without startup test ok marker"。
- **修复**：①预检改为候选用户链 `("ctf", "kali")`，首个证明者胜，跨池数值一致性
  校验不变；②容器身份探针改为 `docker exec id <flag>`（无用户名，证明容器**有效**
  身份，与 `--user` 创建参数数值对账，任意镜像用户名通用）。
- **验收**：`scripts/repro_pool_lease.py`（冻结→租约全链复现工具）实测预检/容器创建
  通过；全量 pytest exit=0。遗留的最终失败为用户配置层模型/凭据不匹配（glm-5.3-flash
  vs DeepSeek 端点），见冒烟面板指引。

---

## 2026-08-29 追加：M9a web 全链接通战役（9 个提交，冒烟到达最后一层）

修复序列（每层都有事件流/复现脚本证据，全量 pytest 绿）：
1. `e51f860` web 派工冻结接线（F1，run-4408）
2. `0296741` 统一镜像事实源（F2，fabricated 旧 tag）
3. `a8a11d3` 身份链 kali 硬编码 ×2（预检候选链 ctf→kali + 容器 exec 数值自证）
4. `b566293` 池容器 user=0:0(root) → PoolSpec.uid:gid；冒烟 harness remember_dispatch 接线
5. `9ed1f8f` worker.status detail 字段 + 池容器死亡证据落盘（本战役的可观测性基建）
6. `2bac39a` gateway 模式池探针自签任务令牌（LEASE OK 里程碑）
7. `74b89d9` offline 钳制反转修复（默认不得 network:none）
8. `dd0a604` 租约 worker 走 executor RCP run 路径（不再当 legacy ContainerHandle）
9. `f9ddb5d` + `70f20fa` + `52ba2f4` runtime pi-config 补齐 gateway 扩展 + Windows 拷贝回退
10. `bfec1b6` + `c9da3db` strict 租约路径保留隔离 HOME 准备（worker_env=None 跳过 HOME 块的断层）

**已证明**：复现脚本 LEASE OK（冻结→池→容器→RCP→网关→真实探针调用→租约）；
容器内隔离 HOME 配置完整（extensions/models.json 实测在位）。

**剩余唯一症状**：worker pi 会话仍报 `Unknown provider "ctf-gateway"`（容器外取证显示其
HOME/配置应完整）。**头号嫌疑**：`dswarm/solver/control_client.py:38` 的
`_PI_ENV_KEYS = {"PI_CODING_AGENT_DIR"}` —— 会话 env 转发白名单可能丢弃了
HOME/DSWARM_PI_PROVIDER 等（或 agent 侧另有一份白名单），导致 pi 实际读到的 HOME
并非 mixin 准备的隔离 HOME。下一步：核对 control link 的会话 env 转发白名单与
runtime-agent（cmd/runtime-agent）的 env 透传行为，让会话 env 与 create env 走同一契约。

### F4.（2026-08-29 续）租约 worker 执行链七连修 + 当前唯一残留

后续修复（全量 pytest 绿，均有活体取证）：
- `74b89d9` offline 钳制反转（默认 network:none 导致 hello 必败）——整个冒烟循环的真凶
- `dd0a604` 租约 worker 改走 executor.run/run_streaming（原先被当 legacy ContainerHandle）
- `2bac39a` gateway 模式池探针自签任务令牌
- `bfec1b6`+`c9da3db` strict 派工保留隔离 HOME 准备（worker_env=None 跳过 HOME 块）
- `f9ddb5d` runtime pi-config 补齐 ctf-gateway-provider.ts + thinkingLevelMap + d.ts compat
- `70f20fa` Windows 宿主 HOME 配置/技能链接拷贝回退（NTFS 符号链接容器内不可解析）
- `7a3ca7f` lease 路径强制容器侧 HOME（Windows 路径形态下 mapper 前缀失配 → 过滤器拒发）
- `a290e2d` 修复 container 赋值被合并编辑吞掉 + spawn→lease env 白名单合并

**已证明**（活体）：容器内隔离 HOME 完整（extensions/models.json 实测）；worker pi 完整
执行生命周期（delegating→claimed→agent_settled→fact_added→concluded，事件全流回）。

**当前唯一残留（行为层，非管道）**：冒烟 worker 完成 pi 生命周期但未产生 `startup_test_ok`
标记输出——模型收到 printf 任务后走了 recon 流程/无 stdout。候选方向：①模型能力
（glm-5.3-flash 对该指令的服从性；可换 deepseek-v4-pro 对照——注意须同时还原 pi-main 凭据）；
②冒烟任务提示与 recon 角色 preamble 的冲突；③lease 流式路径的输出捕获（TEXT delta 只有
delegating/agent_settled 帧，无逐 token 流——对照 TUI/旧路径确认流式颗粒度）。

