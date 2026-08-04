# 架构总览

## 目标

构建一个本地优先、可授权的 CTF / 渗透测试多 Agent 平台，核心能力包括：

- 一个 Go daemon 作为控制平面，负责 API、调度、Docker、模型网关、Pi RPC、SQLite。
- 每个题目按题型运行一个独立 Docker 沙箱，沙箱内是 `pi --mode rpc` worker。
- 多个 worker 通过事件源 SharedGraph 共享事实、dead-end、intent、flag、PoC。
- Reason 服务负责从 SharedGraph 规划下一批 intent，不直接执行命令。
- Flag 接受必须经过 provenance gate，不能只依赖模型在最终回复里声称找到。
- 可选 Wails / React 桌面端，以及 CLI / TUI，全部订阅同一事件流。

## 总体架构

```text
+-----------------------+
| Desktop / CLI / TUI   |
| Wails + React or CLI  |
+-----------+-----------+
            |
            | REST + SSE / WebSocket
+-----------v-----------+
|   Go daemon            |
|                        |
|  API / EventHub        |
|  Scheduler / Task FSM  |
|  Model Gateway         |
|  Sandbox Manager       |
|  Pi RPC Manager        |
|  SharedGraph / Reason  |
|  Provenance Gate       |
|  SQLite                |
+-----------+-----------+
            |
            | Docker API
+-----------v-----------+
| Docker bridge network  |
|                        |
|  worker-web            |
|  worker-pwn            |
|  worker-rev            |
|  worker-crypto         |
|  worker-forensics      |
|  worker-misc           |
+-----------v-----------+
            |
            | pi --mode rpc
+-----------v-----------+
| Pi worker              |
| .pi/agents/*.md        |
| .pi/skills/cyberboard  |
| .pi/skills/web|pwn|... |
| ctf-gateway provider   |
+-----------------------+
```

## 组件职责

| 组件 | 职责 |
|---|---|
| `internal/api` | REST API、SSE / WebSocket、鉴权、任务控制 |
| `internal/eventhub` | 进程内 pub/sub，持久化任务事件，前端重放 |
| `internal/storage` | SQLite schema、任务、事件、模型用量、graph 事件 |
| `internal/scheduler` | 任务状态机、FIFO 队列、并发上限、worker 调度 |
| `internal/modelgateway` | OpenAI 兼容反向代理，短期 task token 换真实 key |
| `internal/sandbox` | Docker 容器生命周期、资源限制、网络模式 |
| `internal/agent` | Pi RPC 会话、JSONL 解析、prompt 构造、worker 生命周期 |
| `internal/coordination` | SharedGraph、Reason、Gate、Review、Resource Lock |
| `frontend` | Wails + React 桌面控制台或纯 Web 控制台 |

## 关键技术决策

### 1. Go 控制平面为主

推荐把 Muteki 的协作内核用 Go 重新实现，而不是在 Go daemon 外再维护一个 Python sidecar。原因：

- 单二进制部署更简单。
- SQLite、Docker、HTTP、Pi RPC 都在 Go 生态内有成熟支持。
- Reason 只是“读 graph + 调 LLM + 写 intent”，不需要 Python agent 运行时。
- BTFly 的 Go daemon 骨架可以直接保留。

如果希望更快复用 Muteki 代码，可以临时跑一个 Python reasoner sidecar，通过 HTTP/Unix socket 通信，但长期不推荐。

### 2. SharedGraph 使用 SQLite 事件源

每个 run 拥有一个 `graph_events` 表，所有事实、intent、dead-end、flag、PoC 都是 append-only 事件。

从事件表派生：

- verified facts
- candidate facts
- open / claimed / concluded intents
- dead-ends
- flags
- suppressed routes
- resource locks

好处：

- 可审计。
- 可回放。
- 可跨容器共享。
- 可通过 SQLite WAL 支持多进程读写。

### 3. Worker 通过 blackboard skill 访问共享图

每个 Pi worker 镜像内置 `cyberboard` skill，提供：

- `read-facts`
- `read-deadends`
- `read-intents`
- `claim <intent_id>`
- `write-fact <text> [--verified]`
- `mark-deadend <reason>`
- `claim-resource <lane>`
- `release-resource <lane>`
- `read-directives`
- `read-flags`

默认实现是宿主导出的共享 SQLite DB 通过 bind mount 进入容器。后续如果容器网络被隔离，可增加 HTTP blackboard API，但第一版建议保持 SQLite bind mount。

### 4. Worker 是 Pi RPC，不是 `pi -p`

容器内运行：

```text
pi --mode rpc \
  --session-dir /workspace/.pi-sessions \
  --provider ctf-gateway \
  --model <model-id>
```

Go daemon 通过 Docker attach 到的 stdin/stdout 发送和接收 JSONL RPC。

### 5. 模型请求必须经过 model gateway

容器内不保存真实 API key。Pi 通过 `ctf-gateway` provider 指向：

```text
http://host.docker.internal:<port>/model
Authorization: Bearer <task-token>
```

Go daemon 校验 task token，替换为真实 upstream key，再反向代理到上游。

### 6. Reason 不执行命令

Reason 由 Go daemon 调用，使用低成本模型，输入是 SharedGraph summary，输出是严格 JSON：

- `verdict`
- `goal_met`
- `intents`
- `pinned_facts`
- `audit`

Reason 不启动 shell，不访问 Docker，不写文件，只规划。

### 7. Flag 必须通过 provenance gate

Flag 候选来源：

- Pi 输出中的 `FOUND_FLAG=` marker。
- `artifacts/final-result.json`。
- `WRITEUP.md` 最终章节。
- Pi tool result 原始输出。

provenance gate 要求：

- 格式匹配。
- 不是占位符。
- 候选 flag 必须逐字出现在真实命令输出或 artifact 内容中。

## 运行流程

### 创建并启动题目

1. 用户创建 task，指定 category、title、description、target、flag format、model profile。
2. daemon 选择对应镜像。
3. daemon probe 模型网关。
4. daemon 签发 task token。
5. sandbox manager 创建容器。
6. daemon 启动 Pi RPC。
7. daemon 发送初始 prompt。

### 多 Agent 协作

```text
Read graph
  -> Reason
  -> propose intents
  -> scheduler picks worker profile
  -> worker claims intent
  -> Pi worker runs commands / tools
  -> worker writes facts / dead-ends / flags via cyberboard
  -> daemon applies provenance gate
  -> graph changes
  -> Reason replans
```

## 目录结构

```text
Cybersec-agent/
├── cmd/
│   └── daemon/
│       └── main.go
├── internal/
│   ├── api/
│   ├── eventhub/
│   ├── storage/
│   ├── scheduler/
│   ├── modelgateway/
│   ├── sandbox/
│   ├── agent/
│   └── coordination/
├── frontend/
│   ├── src/
│   └── package.json
├── workers/
│   ├── base/
│   │   ├── Dockerfile
│   │   ├── pi/
│   │   │   ├── agents/
│   │   │   ├── skills/
│   │   │   └── extensions/
│   │   └── cyberboard/
│   ├── web/
│   ├── pwn/
│   ├── crypto/
│   ├── reverse/
│   ├── forensics/
│   └── misc/
├── eval/
│   ├── nyu/
│   └── local/
├── docs/
└── README.md
```

## 前端控制平面

第一版可以只做 Web 控制台：

- FastAPI/Go API + SSE。
- React 控制台。
- Wails 桌面壳后续再套。

这样能先聚焦 daemon、Docker、Pi worker 和协作内核。
