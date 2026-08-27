# D-Swarm 架构规范（权威 · Blue Book）

> **状态：现行权威（2026-08-27）**。本文是项目的单一架构入口：它浓缩并取代了
> `docs/archive/` 中 01–05 的 Go-daemon 时代设计草案（本文件描述的是真实存在的系统，
> 与代码同步维护）。历史沿革见 [docs/06](archive/06-route-a-plan.md)，研究输入见
> [docs/08](08-oss-research-and-kernel-improvements.md)，实施账本见
> [docs/10](10-v4-kernel-improvement-implementation.md)。
>
> 变更纪律：架构语义的修改必须先改本文（或以 RFC 附件形式），代码随后跟上；
> 实施进度只记录在 [docs/10](10-v4-kernel-improvement-implementation.md)，不回写本文。

## 1. 定位与边界

D-Swarm 是**自主多模型 CTF / 授权渗透测试解题智能体集群**：

- 它是一个**编排层**，不实现模型循环。Worker 执行器 = 被.shell 出来的完整 CLI 编码智能体
  （引擎名册当前仅 `pi`），自带 agentic shell 循环与工具集；D-Swarm 负责规划、派发、证据管理
  与正确性把关。执行器边界：`dswarm/solver/cli_driver.py`（进程/健康）、
  `dswarm/solver/cli_solver.py`（marker 协议、分模式提示词、黑盒约束）。
- 适用范围：CTF、自有靶场、书面授权的渗透测试。信任模型与安全非目标见
  [SECURITY.md](../SECURITY.md)，运维契约见 [docs/runtime-pools.md](runtime-pools.md)，
  端到端工作方式科普见 [工作原理.md](工作原理.md)。三者作为独立文档由本文链接引用。

## 2. 六条根原则（不变式）

任何模块不得违反；每条都有守护测试或结构性保障。

1. **溯源神圣** — 一个 flag 只有出现在真实执行输出（stdout/stderr/工件语料）中才被接受。
   门是硬编码的 `dswarm/solver/gate.py::_flag_ok` + `cli_solver` 反洗白检查，永远不做成可插拔
   verifier。零误报优先于解题率。HITL 指导永远是上下文，不能成为 flag 来源。
2. **黑盒纯净** — solver 只见活靶 + 题面 + 玩家可见 `files`（code-review 题型除外：源码即题面）。
   永不投喂 `solution.*`/writeup；能力评测必须离线（禁 WebSearch/WebFetch），在线"解出"不计入
   solve-rate。
3. **事件溯源 · append-only** — 全体协作锚在一张 SQLite 共享证据图上（`shared_graph.py`）。
   权威只有事件流（`events` 表），一切投影表可从事件流重建；禁止原地覆写。M3 起 payload/
   事实行有不可变 guard（RFC 链见 `archive/11…13`）。
4. **Fail-closed 验证** — 一切"成功声明"默认不可信：M9 Verified-PoC 只认容器内干净终态
   （`finished` 且 rc=0、未 OOM/未 steer/未超时）且 indicator 命中真实输出；M5 账本先记账后放行。
5. **前端是哑总线订阅者** — Web/TUI 只渲染事件流并经 HITL 通道下达指令，永不直连求解核心。
6. **竞速 vs 协调两态** — 默认并行竞速：同题多 profile 各自打，先过溯源门者赢（单 flag 语义）。
   两阶段协调经 `Swarm(stage_policy={"coordinator": {...}})` 启用（`dswarm/swarm/stage_policy.py`
   ），从图上规划 typed intent 再派工。顶层 legacy `coordinator` 字段已被 web 层拒绝。

## 3. 分层架构与数据流

```
        ┌───────────────────────────────────────────────────────┐
        │ 前端（哑终端） apps/web  = FastAPI 后端(:8000, API-only)  │
        │                     + Next.js 指挥台(:3001, 生产构建)    │
        │                apps/tui  = Textual 指挥台               │
        └──────┬──────────────────────────▲─────────────────────┘
     HITL 命令 │ hint/redirect/focus/pause/resume/submit    │ SSE 事件流（只渲染）
               ▼                                          │
┌─────────────────────────────────────────────────────────────┐
│ 编排层 dswarm/swarm/ （Swarm 由多个 mixin 组成）              │
│  SwarmWorkerRuntime·ReviewFlow·RuntimeDegradation·Budget … │
│  ReasonScheduler ← dswarm/solver/reason.py（独立 Reason 相位：│
│      读图 → verdict/goal_met → ≤4 个不重叠 typed intents）    │
│  两阶段 stage_policy：explore(单发冲刺) → review(审查回收)     │
│         │ claim/dispatch（intent 租约 + SpawnGuard + 预算门禁）│ ▲ 事件
│         ▼                                                  │ │
│  SharedGraph(dswarm/swarm/shared_graph.py) append-only      │─┘
│    权威 events 流；facts/intents/pocs/poc_reproductions/… 投影 │
│    workers 经 skills/dswarm-blackboard 直读写（lead≠真相）    │
└────────┬─────────────────────────────────▲──────────────────┘
         │ spawn（租约 · gateway token · 凭据投影）│ stdout/stderr/工件
         ▼                                        │
┌─────────────────────────────────────────────────────────────┐
│ Worker 执行器 dswarm/solver/                                  │
│  CliDriver 进程与健康 → CLI 智能体 `pi`（完整模型循环）          │
│  CliSolver marker 解析（FOUND_FLAG/POC_SAVE/POC_REPRO/        │
│    DEADEND/NEED_INPUT/FACTS…）· 分模式提示词 · 黑盒隔离         │
│  gate.py 溯源门 ◄── 只认真实执行输出                            │
│  container_runtime/container_pool/exec：RCP-v2 控制链路        │
└────────┬────────────────────────────────────────────────────┘
         ▼
 docker/worker-kali 容器沙箱（bash/python/ghidra/pwntools/…）
 底座 dswarm/core/：EventBus 事件脊柱 · JSONL 会话日志/SSE 重放 · 成本账本 · dotenv 启动
```

## 4. 关键机制

### 4.1 marker 协议与溯源门
worker 在自己的真实输出中打标记，CliSolver 从流中收割：`FOUND_FLAG=`（flag 候选）、
`POC_SAVE=path|cmd|status|note`（PoC 工件落 CAS + 图）、`POC_REPRO=path|indicator`
（pentest 限定，见 4.5）、`DEADEND=`、`NEED_INPUT=`/`NEED_KIND=`、结构化 FACTS。
flag 候选必经 gate.py：占位符/洗白路径/不在 provenance corpus 中一律拒绝；反洗白检查
（tool-call echo、结果复读、终局复扫）保证语料覆盖。

### 4.2 共享图与并发正确性
- intent 生命周期 `propose → claim(租约) → conclude(owner-fenced)`；结论无去重键——迟到
  conclusion 保留为过程证据。
- activity lock 按 `verb:target` 加租约锁：两个 worker 同时 nmap 同一靶机时后者直接让位。
- 多 flag：`Challenge.expected_flags`（默认 1）；`=1` 字节级等同首血即停，仅 `>1` 进入多
  flag 路径；完成判定以图快照为最终事实源（内存 `_found_flags` 只是缓存，由
  `Swarm._sync_flags_from_graph` 对账吸收，operator-invalidated flag 永不入计数）。

### 4.3 预算体系（M5）
run 级 UsageJournal/UsageLedger 记账先于消费；ProfileBudgetGate + SpawnGuard 在生成前拦截；
web 提供 `/api/runs/{id}/budget` 与 rebuild。事件流有 `USAGE_RECORDED` 等专属类型。

### 4.4 运行时池（M9a，Docker-first）
控制面冻结一份 run 级 runtime snapshot，按 PoolKey 持有长寿命容器池代际（generation），
spawn 经租约工厂获准。worker 容器永不获得 Docker socket、宿主 HOME、宿主 `.pi` 状态、完整凭据
库或 solution 文件；凭据按操作投影。细则与回滚手册：
[docs/runtime-pools.md](runtime-pools.md)。local 后端是开发逃生舱，双门禁明示。

### 4.5 pentest 模式与 Verified-PoC 门（M9）
pentest 模式下（Origin/Goal/Hints 题面框架）：
1. 高严重度 blocker finding 若恰好绑定一个已保存 PoC 且存在 reproduction 注册
   （`Reproduction{command, indicator}`，注册时公开流只见 digest），review_flow 为其创建
   确定性 idempotent verifier intent；
2. verifier 租约只跑图中注册的不可变 command（容器内、CAS 工件只读 staging、拒绝符号链接
  逃逸），运行态经 fail-closed 归一化后，indicator 必须命中真实 stdout/stderr 才能把
   reproduction 置 `verified` 并回写 finding；
3. 注册/开始/终态全程 append-only 事件化（`poc_reproductions` 是可重建投影）；
4. 范围事后审计（scope_audit）：解析 `Challenge.scope` 白名单，扫描 provenance 语料找越界
   主机引用 → `scope_violation` finding + 报告排除 + HITL 提示；私有/链路本地地址恒视为范围内。

### 4.6 冻结区
Advisor 建议（M8）仅限离线实验收集，生产永久 No-Go（污染风险）。子系统引入在线规划/
联网检索需另立 RFC。

## 5. 里程碑索引（权威进度一律查 docs/10）

| 里程碑 | 内容 | 账本节 |
|---|---|---|
| M0–M2 | Pi 引擎基线 / 双 lane / 成本口径 | docs/10 §M0–§M2 |
| M3 | 事件不可变 RFC v3 | docs/10 §M3 + archive/11–13 |
| M4/M4.1 | 方向诊断 / 操作员 primary direction | docs/10 §M4 |
| M5 | 唯一 token 账本 v4.1（六轮评审） | docs/10 §M5 + archive/14–19 |
| M6(+a/b) | 血统跟踪 / 死路治理 | docs/10 §M6 |
| M7 | 离线能量实验（tie-break 开关关=现网） | docs/10 §M7 |
| M8 | Advisor 离线证据（生产 No-Go） | docs/10 §M8 |
| M9 | Verified-PoC 门 + scope 审计 | docs/10 §M9 |
| M9a | Docker-first 运行时池 | docs/runtime-pools.md |

## 6. 测试与验收教义

- 定义of done：`uv run pytest -q` 绿 + 新行为有确定性测试 + solve-rate 主张必须有真实黑盒
  trace（flag 出现在 worker 真实输出）。ScriptedLLM 模式使全仓无需 key 即绿。
- 可选 opt-in 套件走显式 env 门（如 `DSWARM_RUN_DOCKER_TESTS=1`）；POSIX 专属行为用
  `posix` 标记自动跳过 Windows。
- 不变式回归：provenance gate 用例、事件不可变 guard、append-only 重放全保持绿；任何模块
  不得修改 gate.py 判定逻辑。

## 7. 已知债务与后续路线（本 spec 的诚实清单）

以下为已识别、未处置的技术债（处置须走 RFC/专项，见 ROADMAP）：

1. **超大文件**：`shared_graph.py`(≈5.2k 行) 与 `cli_solver.py`(≈4.3k 行) 远超维护阈值；
   拆分建议：图生命周期域（poc/budget/energy 各自独立模块）与 marker 解析器外置。
2. **mixin 解环**：`worker_runtime_mixin ↔ swarm` 经函数内延迟 import 维持循环；
   应把被搬运的私有 helper 下沉为叶子模块。
3. **normalize/sanitize helper 繁殖**：`direction_rules` 与 web 层各持一套 operator 方向归一
   化；`worker_profiles/shared_graph` 各自繁殖同类 helper —— 收敛到公共叶子模块。
4. **吞异常观测性**：core 内仍有大量 best-effort `except Exception: pass`（杀进程/清理类属合
   理）；共享图写路径已加观测点，其余高频位点应逐步补事件或计数。
5. **事故知识文本化**：~54 条 BUG①②③/run-ID 注释承载回归知识，应沉淀为本文件的附录或测试
   名义，避免随重命名失联。
6. **Windows dev-host 隔离弱化**：token/key 文件 `chmod 0o600` 在 Windows 上组位无效；生产目
   标是 Linux 容器侧，dev host 上应显式知晓该差异。
7. **ROADMAP 早期迭代项**仍含已删除 SDK 的过时内容（I2/I3 已标注 OBSOLETE），跟随下一轮
   roadmap 修订清理。

## 8. 术语速查

| 术语 | 含义 |
|---|---|
| Worker 执行器 | 被 shell 出的 CLI 智能体进程（CliDriver 管理 + CliSolver 接线） |
| Insight Bus | run 内事件广播（worker 间提示互传，lead 而非命令） |
| SharedGraph / 黑板 | append-only SQLite 事件图 + 各投影表，协作唯一事实源 |
| Reason 相位 | 独立规划器：读图产 typed intents（≤4 个不重叠），不持 flag 判定权 |
| Intent / Activity lock | 任务领用单元（带租约）/ 动作级去重锁（verb:target） |
| Provenance gate | flag 接受的唯一硬编码守卫（gate.py + cli_solver 反洗白） |
| CAS / workspace | 共享对象存储（`shared/objects/.../<sha>`）+ 每 worker 工作区 |
| RCP-v2 | 容器控制链路协议版本（run/streaming/审计帧），池代际冻结 |
| stage_policy | 两阶段编配配置（explore → review；coordinator/budgets 子字典） |
| Verified-PoC | pentest 下"重放注册 PoC + indicator 命中真输出才 verified"的门 |
| HITL | 人机协同通道（hint/redirect/focus/pause/resume/submit），上下文而非 flag 来源 |

## 9. 文档地图

| 层级 | 文档 |
|---|---|
| 本文 | 架构规范（唯一入口） |
| 🟢 现行 | [工作原理.md](工作原理.md)（协作模式科普）· [runtime-pools.md](runtime-pools.md)（运维手册）· [docs/10](10-v4-kernel-improvement-implementation.md)（实施账本） |
| 🟡 决定案卷 | [docs/06](archive/06-route-a-plan.md)（路线 A 批准记录）· [docs/07](07-d-swarm-ui-audit-and-redesign.md)（UI 规范记录）· [docs/08](08-oss-research-and-kernel-improvements.md)（研究决策）· [docs/09](09-kernel-improvement-review-feedback.md)（评审意见） |
| 🔵 闭环 RFC | archive/11–19（M3 不可变 ×3、M5 唯一账本 ×6） |
| 🗄️ 历史草案 | archive/01–05（Go-daemon 时代，已被本文取代）、brand-inventory 快照、早期 superpowers 计划书 |
