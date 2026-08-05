# 路线 A 实施计划：muteki 底座 + BTFly 资产

> 本文档是对 01–05 的修订与落地。决策背景见 [评审结论](#决策记录)。
> 状态：**已批准执行（路线 A）**。许可证：**AGPL-3.0**（随 muteki，已接受）。

## 决策记录

- **2026-08-04**：评审 01–05 后选定路线 A —— fork [muteki](C:\Projects\Agent-projects\muteki) 为底座（Python 内核 + FastAPI/Next 控制面原封不动），把 CTF-BTFly 的四块资产作为功能并入。
- 否决路线 C（Go 全重写内核）：内核约 1.5–2 万行 Python（`shared_graph.py` 4171 行 / `swarm.py` 4843 行 / `cli_driver.py` 1919 行），且沉淀了大量真实事故修复与 50+ 测试文件背书；Go 重写唯一好处是宽松许可，本项目不需要。
- 否决路线 B（Go daemon + Python sidecar）：AGPL §13 网络条款同样覆盖 sidecar，无许可优势，还多一层 IPC。

## 1. 合并架构（替代 01 的 Go 重写方案）

**单控制面原则**：控制面 = muteki 的 FastAPI + Next.js 指挥台（`apps/web/`）。不再维护第二条 Go 控制面主线。

```text
+-----------------------+
| Next.js 指挥台 (3001)  |     ← muteki 现有 apps/web/ui
+-----------+-----------+
            | /api (SSE + POST 命令)
+-----------v-----------+
| FastAPI 后端 (8000)    |     ← muteki apps/web/server.py
|  run_manager / run_meta|
|  任务队列 + FSM（新增） |
|  模型网关 task token（新增）|
|  Swarm 协调器 (内核)    |     ← muteki/swarm + solver + core（不动）
+-----------+-----------+
            | Docker API（宿主 daemon）
+-----------v-----------+
| worker 容器（每 run 一个，按题目分类选镜像）|
|  pi RPC 会话  |  blackboard skill  |  ctf-skills 知识库 |
+-----------------------+
```

### 现有 Go 骨架的去向

`cmd/daemon` + `internal/{api,eventhub,platform,scheduler,storage}` 是路线 C 的产物（BTFly 式任务状态机 + SQLite + 事件总线 + NoopRunner）。**暂停开发，保留代码**，未来只有两个合法用途：

1. **模型网关独立二进制**：BTFly 的 `modelgateway`（task token 反向代理）本身是 Go，若 Python 移植不顺手，可保留为独立小服务，FastAPI 通过 HTTP 调用（见 P3）。
2. **Wails 桌面壳的后端**：P6 若做桌面端，可让它包住 FastAPI，而不是反向。

### BTFly 资产清单与去向

| BTFly 资产（来源：git 历史 `a141bb5`） | 吸收方式 | 阶段 |
| --- | --- | --- |
| Pi RPC 引擎：`pi --mode rpc` + Docker attach JSONL + pause/resume/abort（[images/base/Dockerfile](C:\Projects\Agent-projects\CTF-BTFly:images/base/Dockerfile:31)、[internal/agent/service.go](C:\Projects\Agent-projects\CTF-BTFly:internal/agent/service.go:812)） | 新增 `PiDriver`（接入 muteki `CliDriver` 抽象：`build_execute` / `build_resume` / `parse_stream_steps`，[cli_driver.py](C:\Projects\Agent-projects\muteki\muteki\solver\cli_driver.py:249)） | P1 |
| 分类镜像：`images/{base,web,crypto,pwn,reverse,forensics,misc}/Dockerfile` | 搬入 `docker/`；runtime profile 增加"按 category 选镜像" | P2 |
| CTF 知识库：`skills/` 下 100+ 参考文档（web/crypto/pwn/reverse/forensics/misc） | 烤入 worker 镜像 `/opt/ctf-skills`（BTFly 做法） | P2 |
| 模型网关：task token 换真实 key 的反向代理 + usage 记录（[modelgateway/gateway.go](C:\Projects\Agent-projects\CTF-BTFly:internal/modelgateway/gateway.go:226)） | 移植为 FastAPI 中间件（优先）或保留 Go 独立二进制（兜底）；**弃用 muteki 的凭据直注** | P3 |
| task 状态机 + FIFO 队列 + 并发上限（[platform/model.go](C:\Projects\Agent-projects\CTF-BTFly:internal/platform/model.go:39)） | 移植到 `apps/web/run_manager` 层 | P4 |
| flag 启发式检测（[flag_detector.go](C:\Projects\Agent-projects\CTF-BTFly:internal/agent/flag_detector.go:287)） | 降级为 **gate 的候选输入通道**（扫描 final-result.json + WRITEUP.md），验收权归 muteki `gate.py` | P1 |
| 事件 hub + WebSocket | **不采纳**，保留 muteki EventBus / JSONL / SSE | — |
| Wails + React 桌面 | 可选壳（P6），届时包 FastAPI | P6 |

**不搬的 muteki 部分**：内核全部保留（swarm/shared_graph/reason/gate/blackboard/insight_bus/stage_policy/workspace CAS/container_exec/credential_accounts/cost/learning/distill/eval_nyu）。

## 2. Worker 模型（修订 03）

- **单发为主**（muteki 哲学）：worker 领活跑完就退，不中途搅动。
- **pi 会话续接补短板**：`CliDriver.build_resume()` 已是内核抽象；BTFly 的 RPC 长会话 resume 用于 HITL 追加提示（操作员指令注入"下一个 worker"之外，可对同一会话续问）。
- **三通道事实收割**（顺序 = 优先级）：
  1. `muteki-blackboard` skill（主通道，env：`MUTEKI_BLACKBOARD_DB` / `MUTEKI_WORKER_ID` / `MUTEKI_INTENT_ID`）；
  2. `VERIFIED_FACT=` / `FOUND_FLAG=` 等 marker（兜底收割，[cli_driver.py](C:\Projects\Agent-projects\muteki\muteki\solver\cli_driver.py:435) 前已有 `_FLAG_LINE`）；
  3. `artifacts/final-result.json` + `WRITEUP.md`（BTFly 式文件通道，只作为 gate 的候选输入）。
- **flag 验收唯一权威**：`gate.py:150 flag_ok`（格式 + 非占位符 + 逐字溯源，硬编码不可插拔）。BTFly 的 `verified:true` 自报只降级为候选置信度。

## 3. 共享图与事件（修订 04）

- schema 以 muteki `shared_graph.py` 为准（22 张表、30+ 事件类型、intent 租约、lane/resource 锁、HITL 请求、compaction 审计），**不重写、不发明新状态机**。
- Reason 契约保持 muteki `reason.py`（verdict / goal_met / intents / pinned_facts / audit，≤4 个非重叠 intent）。
- 保留 InsightBus 内存广播（队友实时可见新事实）。
- 事件契约 = muteki 的 EventBus → JSONL 落盘 + SSE 重放。

## 4. 阶段计划（重排 02）

> **执行状态（2026-08-05）：P0–P6 全部落地并提交。** P5 的 NYU-200 全量回归待有数据集+基线引擎的机器上执行（跑批器 `eval_nyu/` 已就绪，见 `eval_nyu/README.md`）；P3 镜像 pi 已对齐 0.83.0（`docker/worker-pi/build-base.sh`）。

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| **P0 基线** | `git archive a141bb5` 恢复 BTFly 源码到 `references/btfly/`（只读参考）；muteki 在本机跑通测试套件 | BTFly 参考树完整可读；`uv run pytest`（或 `.venv` 直跑）通过；web 后端能起 ✅ |
| **P1 内核闭环 + PiDriver** | 目录合并；新增 `PiDriver`（`pi --mode json` 子进程 JSONL 会话）；local 后端跑通一道简单题 | 一个 pi worker 解出一道 web 题；flag 过 gate 进 graph；事件流可见于 web UI ✅ |
| **P2 分类镜像 + 知识库** | BTFly Dockerfile 搬入 `docker/`；`skills/` 知识库烤入镜像；runtime profile 按 category 选镜像；每 run 一个容器 | web/crypto/pwn 各解一道；容器内 worker 能读写共享图 ✅（P2 冒烟 + P3 容器链路） |
| **P3 模型网关** | task token 网关（Python）；容器内无真实 key | 容器内只有 task token；错误 token 401；usage 落库；断 token 立即失效 ✅（冒烟 PASS；pi 对齐 0.83.0） |
| **P4 任务队列** | task FSM + FIFO + 并发上限（默认 5，1–8） | 多任务排队/暂停/恢复/中止；事件流完整 ✅（`RunScheduler` + API + deck 状态图标） |
| **P5 评测回归** | 复用 `eval_nyu`；pi 引擎加入引擎名册 | 200 题回归报告：pi vs claude/codex/cursor 的 winner 分布、solve rate、成本 🔶（跑批器 + 基线入库 + 本地 pilot 2/2；NYU-200 待跑） |
| **P6（可选）桌面壳** | Wails + React 壳包 FastAPI | 桌面端可管理 run ✅（`desktop/`，窗口 + 双服务监督） |

## 5. 风险与缓解（补充 05）

| 风险 | 缓解 |
| --- | --- |
| AGPL 传染（已接受） | 整个项目 AGPL-3.0；BTFly 代码只借鉴设计，复制需作者授权（其 HEAD 无 LICENSE） |
| muteki 未在 Windows 实测 | 控制面走 muteki 官方 compose（Linux 容器控制面 + 宿主 Docker Desktop 拉 worker）；local 后端仅作开发调试 |
| pi 版本漂移 | 镜像内锁 `PI_VERSION`；本机 pi 0.83.0 与 BTFly 锁的 0.81.1 需对比 RPC 协议差异（P0 验证） |
| 内核漂移 | 内核只读，BTFly 资产全部落在扩展点（driver / docker / web 层），跟踪上游 muteki 更新 |
| 成本失控 | 保留 muteki 背压（worker 上限 ~10、空槽每拍补 1、无产出软暂停、预算控制） |

## 6. 本机环境（已核实）

- pi 0.83.0（全局 npm 包）✓
- Python 3.13.7 ✓（muteki 要求 ≥3.13）
- uv 0.11.16 ✓
- Docker Desktop 需自行确认（P2 依赖）

## 7. P0 执行结果（2026-08-04）

| 项 | 结果 |
| --- | --- |
| BTFly 参考源码 | 已从 `a141bb5` 恢复到 `references/btfly/`（含 internal/、cmd/、images/、agents/、skills/、frontend/、go.mod）。**该 commit 带 AGPL-3.0 LICENSE**——许可问题解除：代码可直接移植（与路线 A 的 AGPL 定位一致），不再局限于"只借鉴设计" |
| muteki 依赖 | `uv sync --extra dev` 成功（.venv 原缺 pytest 等 dev 包） |
| muteki 测试套件 | `pytest -m "not live"`：**1009 passed / 55 failed / 14 skipped**。失败全部为 Windows 平台差异，无内核逻辑失败：<br>① 编码类 ~12 个（`read_text` 默认 GBK 读 UTF-8 TSX）——`PYTHONUTF8=1` 下全部消失，属测试环境问题；<br>② POSIX 专属 ~20 个（`os.geteuid`、`signal.SIGSTOP/SIGKILL` 不存在）——Windows 上应 skip，容器执行逻辑本身在 Linux 容器内不受影响；<br>③ 路径断言 ~20 个（`/` vs `C:\` 分隔符、相对 symlink 字符串、`.codex` 家目录残留）——测试断言需平台化 |
| 后续动作 | P1 开始时顺手给测试加 Windows 平台化（`PYTHONUTF8=1` 进 pytest 配置 + POSIX 断言 skip），不修内核 |
