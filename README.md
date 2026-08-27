# ctf-swarm — CTF 自动化解题系统

基于 **dswarm**（AGPL-3.0）内核的 CTF / 授权渗透测试多 Agent 自动化解题平台，融合 **CTF-BTFly** 的工程资产：

- **内核（来自 dswarm，只读不重写）**：Swarm 协调器、SharedGraph 事件源共享黑板、Reason 规划、provenance gate、blackboard skill、HITL、容器后端。
- **BTFly 资产（路线 A 逐步并入）**：Pi RPC worker 引擎、统一 Kali worker 镜像、CTF 知识库（`skills/` 100+ 参考文档）、模型网关（task token 换真实 key）。
- **控制面**：FastAPI + Next.js 指挥台（dswarm 原版）。

> ⚠️ 本工具是攻击性安全自动化工具，只允许用于明确授权的 CTF、自有靶场和书面授权的渗透测试。勿对未授权目标使用。

## 快速开始

```bash
uv sync --extra dev          # 安装依赖（Python ≥ 3.13, uv）
uv run pytest -q            # 测试套件（无 key 时 live 测试自动跳过）
./run.sh web                 # FastAPI 后端 (:8000) + Next 指挥台 (:3001)
```

配置通过 `DSWARM_*` 环境变量（见 `.env.example`）；Reason 规划器需要 `DSWARM_DEEPSEEK_API_KEY`。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `dswarm/` | 内核：swarm / solver / models / sandbox / learning / core |
| `apps/web/` | FastAPI 后端 + Next.js 指挥台 |
| `apps/tui/` | Textual TUI（未完工） |
| `cmd/runtime-agent/` | 容器内 Go supervisor（反向连接） |
| `docker/` | worker 镜像（BTFly 分类镜像逐步并入） |
| `skills/` | dswarm-blackboard skill |
| `docs/` | 权威架构规范 `00-architecture-spec.md` + 运维手册 / 账本 / 决定案卷；历史草案在 `docs/archive/` |
| `references/btfly/` | BTFly 参考源码（git 历史 a141bb5，AGPL-3.0，只读参考） |

## 路线与状态

- **路线 A**：fork dswarm 为底座，BTFly 资产以功能形式并入。许可证 AGPL-3.0（已接受）。
（批准记录：[docs/archive/06-route-a-plan.md](docs/archive/06-route-a-plan.md)。）
- 进度：路线 A P0–P6 已全部落地（v0.3.0-rc.1）；内核改进里程碑 M0–M9a 已实现并验证，
  现状以实施账本 [docs/10](docs/10-v4-kernel-improvement-implementation.md) 为准。测试数量
  随工作区演进变化，以 `uv run pytest -q` 的实际输出为准。
- 详见 [docs/00-architecture-spec.md](docs/00-architecture-spec.md)（权威架构规范）。

## 开发约定

- 内核（`dswarm/`）保持边界清晰：BTFly/运营资产应落在扩展点（driver / docker / web 层）。如需同步上游，请先配置对应 remote，再按维护者确认的合并流程操作。
- 测试：Windows 宿主跑测试用 `PYTHONUTF8=1`；容器执行路径的 POSIX 专属测试在 Windows 上跳过。
- Worker 引擎名册：`pi`（当前唯一 worker 引擎；方向 profile 统一使用同一 Kali 镜像）。

## 血统与上游

- **本项目是 [FishCodeTech/muteki](https://github.com/FishCodeTech/muteki) 的 fork（魔改分支）**，
  遵循 GNU AGPL-3.0（上游许可证已保留）。归属与分叉声明详见根目录 `NOTICE`。
- **D-Swarm 是独立项目**：自 fork 起实现已大幅分叉——单引擎路线（`pi`）、event-sourced
  共享图、M3/M5 内核加固、两阶段 stage_policy 协调、M5 预算账本、M9a Docker-first 运行时池、
  M9 pentest Verified-PoC 门等均为本项目自有演进；除血统外与上游无代码同步关系。
- CTF-BTFly（参考）：源码在 `references/btfly/`，commit `a141bb5`，AGPL-3.0，只读参考。

## Runtime pools (M9a)

D-Swarm is Docker-first for real workers. The control plane freezes one
run-scoped runtime snapshot and owns one long-lived container per PoolKey; a
worker container never receives the Docker socket, host HOME, host `.pi` state,
full credential stores, or solution/reference files. Credentials are projected
per operation and diagnostics remain private rather than becoming evidence.

See [docs/runtime-pools.md](docs/runtime-pools.md) for the Docker-first runbook,
local-development dual gate, reopen cleanup barrier, lifecycle ownership, and
forward-only rollback procedure. The fake-Pi Docker proof is opt-in with
`DSWARM_RUN_DOCKER_TESTS=1` and never uses a real provider credential.
