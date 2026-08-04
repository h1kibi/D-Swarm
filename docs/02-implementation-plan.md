# 分阶段实施计划

## 阶段 0：基线确认与代码边界

目标：在不直接复制有争议源码的前提下，确定参考边界。

### 输入

- `C:\Projects\Agent-projects\muteki`
- `C:\Projects\Agent-projects\CTF-BTFly`

### 动作

1. 从 BTFly git 历史提取完整源码作为架构参考，不直接复制进新项目。
2. 从 Muteki 提取 SharedGraph、Reason、Gate、blackboard 的概念和 SQLite 事件模型。
3. 确认 license：
   - Muteki 是 AGPL-3.0。
   - BTFly 当前 HEAD 没有 LICENSE，历史里曾多次增删。
   - 新项目只借鉴设计，不搬运闭源/授权不明确代码。
4. 确定新项目命名空间和模块边界。

### 交付物

- 本目录文档集。
- `go.mod` 初始模块。
- 组件依赖图。

### 退出标准

- 明确哪些模块借鉴 BTFly，哪些借鉴 Muteki，哪些全新实现。
- 确认新项目可以独立构建。

---

## 阶段 1：Go daemon 骨架

目标：先把控制平面跑起来，不接 Docker 和模型。

### 模块

```text
cmd/daemon/main.go
internal/api/server.go
internal/eventhub/hub.go
internal/storage/sqlite.go
internal/platform/model.go
```

### 任务

1. 定义 task、event、model usage、execution settings 的数据模型。
2. SQLite schema：
   - `tasks`
   - `task_events`
   - `model_usage`
   - `settings`
   - `graph_events`
   - `graph_views`
3. 实现 task 状态机：
   - ready
   - queued
   - provisioning
   - running
   - paused
   - settled
   - delegating
   - failed
   - cancelled
4. 实现事件 hub，支持持久化事件和 SSE 订阅。
5. 实现 `health`、`task`、`queue` 基础 API。

### 退出标准

- `go test ./...` 通过。
- 可以通过 API 创建任务、查询状态、删除终态任务。
- 事件可以持久化并重放。

---

## 阶段 2：模型网关

目标：容器内不接触真实 API key。

### 模块

```text
internal/modelgateway/config.go
internal/modelgateway/gateway.go
internal/modelgateway/manager.go
```

### 任务

1. 实现 OpenAI 兼容反向代理。
2. 支持多个 model profile：
   - 单模型旧配置兼容。
   - `CTF_MODELS=deepseek,vision` 多模型配置。
3. 实现 task token：
   - `Issue(taskID)` 生成 256 位随机 token。
   - `Revoke(token)` 立即失效。
   - 请求头 `Authorization: Bearer <task-token>`。
4. 记录模型用量：
   - input tokens
   - cached input tokens
   - output tokens
   - reasoning tokens
   - latency
   - status code
5. 支持 DeepSeek 的 `developer -> system` 兼容改写。

### 退出标准

- 未配置模型时健康检查可见。
- 容器只能通过 task token 访问。
- 错误 token 返回 401。
- 用量写入 SQLite。

---

## 阶段 3：Docker 沙箱 + Pi RPC

目标：让一个 Pi worker 在专项镜像里通过 RPC 解题。

### 模块

```text
internal/sandbox/manager.go
internal/agent/rpc.go
internal/agent/service.go
workers/base/Dockerfile
workers/web/Dockerfile
workers/pwn/Dockerfile
...
```

### 任务

1. 构建 base image：
   - Node / Pi runtime。
   - `ctf-gateway` provider extension。
   - `cyberboard` skill。
   - common AGENTS.md。
   - 非 root 用户 `ctf`。
2. 构建题型镜像：
   - web：curl、nmap、sqlmap、gobuster、whatweb、httpx、pyjwt。
   - pwn：gdb、pwntools、checksec、ropper、patchelf、qemu-user。
   - crypto：z3、gmpy2、pycryptodome、sympy。
   - reverse：ghidra 头less、angr、gdb、radare2。
   - forensics：binwalk、tshark、volatility、sleuthkit。
   - misc：ffmpeg、imagemagick、steghide、zbar。
3. 实现 sandbox manager：
   - `Start`
   - `Prompt`
   - `Abort`
   - `Pause`
   - `Resume`
   - `Stop`
4. 容器启动命令：
   ```text
   pi --mode rpc --session-dir /workspace/.pi-sessions \
      --provider ctf-gateway --model <model-id>
   ```
5. 实现 Pi RPC JSONL 解析：
   - `agent_start`
   - `turn_start`
   - `tool_call`
   - `tool_result`
   - `message`
   - `agent_end`
   - `agent_settled`
   - `error`
6. 实现初始 prompt：
   - challenge description
   - flag format
   - workspace layout
   - cyberboard skill 使用规则
   - final-result.json 规范
   - WRITEUP.md 规范

### 退出标准

- 一个 web 题目容器能启动并完成一轮 Pi RPC。
- daemon 能收到 tool_call / tool_result / final result。
- daemon 能暂停、恢复、中止容器。

---

## 阶段 4：SharedGraph 与 blackboard

目标：多个 worker 能共享事实、dead-end 和 intent。

### 模块

```text
internal/coordination/graph.go
internal/coordination/graph_events.go
internal/coordination/graph_sqlite.go
workers/base/cyberboard/cyberboard.py
workers/base/pi/skills/cyberboard/SKILL.md
```

### 任务

1. 实现 append-only `graph_events`：
   - `fact_added`
   - `dead_end`
   - `intent_proposed`
   - `intent_claimed`
   - `intent_concluded`
   - `flag_found`
   - `flag_invalidated`
   - `poc_saved`
   - `review_finding`
   - `fact_challenged`
   - `route_suppressed`
   - `resource_locked`
   - `resource_released`
   - `operator_directive`
2. 实现 materialized views / query functions：
   - verified facts
   - candidate facts
   - open intents
   - dead-ends
   - flags
   - PoCs
   - resource locks
3. 实现原子 intent claim：
   - `claim_intent(worker, intent_id, lease_s)` 返回 WON / LOST。
4. 实现 `cyberboard.py`：
   - 只依赖 Python stdlib。
   - 使用 SQLite WAL。
   - 从环境变量读取 `CYBERBOARD_DB`、`CYBERBOARD_WORKER_ID`、`CYBERBOARD_INTENT_ID`。
5. 把 `cyberboard` skill 安装到每个 worker image。

### 退出标准

- 两个 worker 容器能共享同一个 graph DB。
- 一个 worker 写入 verified fact，另一个 worker 能读到。
- 两个 worker 同时 claim 同一 intent，只有一个 WON。

---

## 阶段 5：Reason 规划与调度

目标：从“每个 worker 自己瞎撞”升级为“reason 规划 -> worker 执行”。

### 模块

```text
internal/coordination/reason.go
internal/coordination/intent.go
internal/coordination/review.go
internal/scheduler/dispatcher.go
```

### 任务

1. 实现 graph summary：
   - verified facts
   - candidate facts
   - open intents
   - attempted intents
   - dead-ends
   - flags
   - directives
2. 实现 Reason prompt：
   - 输出严格 JSON。
   - 最多 4 个独立非重叠 intent。
   - 每个 intent 引用事实 seq。
   - 对候选事实必须 audit。
   - 识别重复 intent 为 `dup_of`。
   - 支持 `complete` / `course_correct` / `explore`。
3. 实现 scheduler：
   - race mode：多个 worker 同时打整题。
   - coordinator mode：reason 规划后 dispatch explore worker。
   - review mode：当出现重复路线、冲突事实、被质疑 fact 时启动 review worker。
4. 实现 worker profile 选择：
   - 按 category 选择 `pi-web`、`pi-pwn` 等。
   - 按引擎健康度选择可用 worker。
   - 按资源锁避免并发撞同一目标。
5. 实现 backpressure：
   - 最大 worker 数。
   - 每轮新增 explore worker 数量。
   - 连续无产出时 soft pause。

### 退出标准

- Reason 能在 graph 变化时产生 intents。
- scheduler 能把 intents 调度到对应 category worker。
- worker claim 后不会重复 dispatch 同一 intent。
- review worker 能对 candidate fact 发起验证。

---

## 阶段 6：Provenance Gate

目标：防止模型“声称找到 flag”就算解出。

### 模块

```text
internal/coordination/gate.go
internal/agent/marker.go
internal/agent/evidence.go
```

### 任务

1. 收集证据：
   - Pi RPC `tool_result` 原始输出。
   - `artifacts/final-result.json`。
   - `WRITEUP.md`。
   - `FOUND_FLAG=` marker。
2. 实现 `flag_ok`：
   - flag format 匹配。
   - 非占位符。
   - flag 逐字出现在原始输出或 artifact 中。
3. 实现 anti-laundering：
   - 只出现在 assistant prose 中的 flag 不接受。
   - 只出现在 `FOUND_FLAG=` marker 中但无真实输出的 flag 不接受。
   - `flag{...}`、`{uuid}`、`<flag>` 等占位符不接受。
4. 把 gate 写成不可插拔的硬编码函数，不允许前端传入自定义 verifier。

### 退出标准

- 单元测试覆盖真实输出、占位符、伪造 marker。
- 只有 traceable 的 flag 才写入 graph。

---

## 阶段 7：父子 Agent 委派

目标：主 Agent 可以创建最多 3 个专项子 Agent。

### 模块

```text
internal/agent/delegation.go
internal/agent/transfer.go
internal/agent/handoff.go
```

### 任务

1. 父 Agent 在工作区写入 `.cyberboard/subtasks/requests/*.json`。
2. daemon 校验：
   - category 白名单。
   - 最多 3 个子任务。
   - artifactPaths 只能引用当前工作区普通文件。
   - 请求大小限制。
3. daemon 创建子 task：
   - 复制 attachments 和 input。
   - 写入 `handoff/request.json`。
   - 使用对应题型镜像。
   - 进入 FIFO 队列或立即启动。
4. 子 Agent 完成后：
   - 报告写回父工作区 `artifacts/subtasks/`。
   - 父 Agent 被唤醒。
   - 只有父 Agent 能整合最终 flag 和 WRITEUP。

### 退出标准

- 父 Agent 能请求子 Agent。
- 子 Agent 结果能回传。
- 父 Agent 能继续会话。
- 子 Agent 不能创建子 Agent。

---

## 阶段 8：前端与 HITL

目标：提供题目管理、运行控制、事件时间线、文件预览和 Writeup。

### 模块

```text
frontend/src/
frontend/package.json
internal/api/
```

### 任务

1. React 控制台：
   - 题目卡片。
   - 创建题目。
   - 上传附件。
   - 配置模型。
   - 查看 graph。
   - 查看 worker 状态。
   - 查看 tool_call / tool_result。
   - 暂停 / 恢复 / 中止。
   - 下载 WRITEUP。
2. Wails 壳：
   - 窗口。
   - 系统托盘。
   - 启动 daemon。
   - 安全退出。
3. HITL：
   - operator directive 写入 graph。
   - worker `NEED_INPUT=` 请求外部资源。
   - operator 可以 spawn / kill worker。

### 退出标准

- 不依赖手工查看容器日志。
- 前端能实时看到事件流。
- HITL 指令进入下一个 worker 或下一次 Reason。

---

## 阶段 9：评测

目标：用公开 benchmark 验证能力，而不是只靠手头题目。

### 模块

```text
eval/
eval/nyu/
eval/local/
```

### 任务

1. 接入 NYU CTF Bench 或 CTFTiny。
2. 接入 PicoCTF 本地题目作为快速回归。
3. 离线模式：
   - 禁用 WebSearch / WebFetch。
   - 防止 worker 搜 writeup。
4. 记录指标：
   - solve rate
   - Pass@1
   - median solve time
   - cost
   - tokens
   - winner per category / engine
5. 保存 trace：
   - graph events
   - Pi RPC events
   - final-result
   - gate evidence

### 退出标准

- 新改动必须跑 benchmark 回归。
- 声称提升必须有真实 trace 支持。

## 推荐开发顺序

```text
Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6
         -> Phase 7 -> Phase 8 -> Phase 9
```

最小可验证版本：

```text
Phase 1 + Phase 2 + Phase 3 + Phase 6
```

这个版本已经可以：

- 创建题目。
- 启动 Pi RPC 容器。
- 调用模型网关。
- 产生解题过程。
- 用 provenance gate 验收 flag。
