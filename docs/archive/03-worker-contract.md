> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../00-architecture-spec.md) 取代；本文保留作为时代记录。

# Pi Worker 契约

## 目标

定义所有 Pi worker 都必须遵守的输入、输出、共享状态和停止条件，避免每个 category skill 各自发明一套协议。

## Worker 目录结构

```text
workers/
├── base/
│   ├── Dockerfile
│   ├── pi/
│   │   ├── AGENTS.md
│   │   ├── agents/
│   │   │   ├── coordinator.md
│   │   │   ├── pi-web.md
│   │   │   ├── pi-pwn.md
│   │   │   ├── pi-crypto.md
│   │   │   ├── pi-reverse.md
│   │   │   ├── pi-forensics.md
│   │   │   └── pi-misc.md
│   │   ├── skills/
│   │   │   ├── cyberboard/
│   │   │   │   ├── SKILL.md
│   │   │   │   └── cyberboard.py
│   │   │   ├── web/
│   │   │   ├── pwn/
│   │   │   ├── crypto/
│   │   │   ├── reverse/
│   │   │   ├── forensics/
│   │   │   └── misc/
│   │   └── extensions/
│   │       └── ctf-gateway.ts
│   └── cyberboard/
└── web/
│   ├── Dockerfile
│   └── pi/skills/web/
└── pwn/
    ├── Dockerfile
    └── pi/skills/pwn/
```

## Agent 文件规范

每个 agent 是一个 Markdown 文件，带 YAML frontmatter。

```markdown
---
name: pi-web
description: Solve authorized web CTF challenges with HTTP inspection and exploit scripts.
model: openai-codex/gpt-5.5
thinking: high
tools: [read, grep, find, bash, python, curl]
skills: [cyberboard, web]
---

You are a web CTF specialist.

Operate only inside `/workspace` and the supplied target.
Use cyberboard before starting a direction and after confirming facts.
Do not claim a flag unless it appears in real command output or artifact content.
```

核心 frontmatter：

| 字段 | 含义 |
|---|---|
| `name` | agent 名称，例如 `pi-web` |
| `description` | 供调度器选择 worker 的摘要 |
| `model` | 可选，默认继承 run model |
| `thinking` | 可选：low / medium / high / xhigh |
| `tools` | 可选工具 allowlist |
| `skills` | 加载的 skill 集合 |

## 共享工作区

每个 task 一个工作区：

```text
data/workspaces/task_xxx/
├── attachments/
├── artifacts/
│   ├── final-result.json
│   └── subtasks/
├── .cyberboard/
│   ├── graph.db
│   └── requests/
├── .pi-sessions/
└── WRITEUP.md
```

## 初始 Prompt 契约

daemon 发送的第一条 RPC prompt 必须包含：

```text
任务 ID
题型
题目描述
目标地址/端口
Flag 格式
附件路径
模型使用说明
共享图路径
最终产物规范
安全边界
```

## Worker 输出 Marker

Pi worker 在 assistant message 或 tool output 中输出这些 marker：

| Marker | 用途 |
|---|---|
| `VERIFIED_FACT=<text>` | 从真实输出确认的事实 |
| `DEADEND=<reason>` | 已排除的方向 |
| `NEED_INPUT=<text>` | 需要操作员提供的资源 |
| `NEED_KIND=<kind>` | 外部阻塞分类 |
| `POC_SAVE=<path>\|<cmd>\|<status>\|<note>` | 保存可复用 PoC |
| `FOUND_FLAG=<flag>` | 候选 flag，必须过 gate |
| `FINAL_RESULT=<json>` | 最终结果简写 |

daemon 会解析这些 marker，但不会把 marker 本身当作事实来源。

## final-result.json

成功：

```json
{
  "status": "solved",
  "flags": [
    {
      "value": "flag{...}",
      "verified": true,
      "evidence": "command output or artifact path"
    }
  ]
}
```

未成功：

```json
{
  "status": "unsolved",
  "flags": []
}
```

## WRITEUP.md 契约

每个 worker 在结束前必须写 `WRITEUP.md`：

- 中文或英文可复现报告。
- 至少包含：题目分析、关键证据、命令、脚本、flag 或未解原因。
- 如果使用脚本，必须写到 `artifacts/`。
- 不在文档中粘贴未公开题目附件或敏感数据。

## cyberboard skill 使用规则

### 动手前

```bash
python3 cyberboard.py read-deadends
python3 cyberboard.py read-review
python3 cyberboard.py read-facts
```

### 确认事实后

```bash
python3 cyberboard.py write-fact "admin:admin login returns 302 -> /dashboard" --verified
```

### 排除方向后

```bash
python3 cyberboard.py mark-deadend "no SQLi on /search"
```

### 领取任务

```bash
python3 cyberboard.py list-intents
python3 cyberboard.py claim I3
```

### 独占资源

```bash
python3 cyberboard.py claim-resource "destructive:tcp:445@172.22.11.45"
python3 cyberboard.py release-resource "destructive:tcp:445@172.22.11.45"
```

## Pi RPC 协议

daemon 通过 Docker attach 到容器 stdin/stdout。

### 发送 prompt

```json
{
  "id": "prompt-abc",
  "type": "prompt",
  "message": "..."
}
```

### 中止当前回合

```json
{
  "type": "abort"
}
```

### 恢复同一会话

```json
{
  "id": "resume-abc",
  "type": "prompt",
  "message": "..."
}
```

## 停止条件

每个 worker 必须返回以下之一：

- `solved`：找到经过 provenance gate 的 flag。
- `blocked`：需要操作员资源。
- `stuck`：方向已死，建议 dead-end。
- `done`：任务边界完成。
- `failed`：环境或模型不可用。

## 输出契约

worker 最终结果由 daemon 统一封装：

```json
{
  "worker_id": "pi-web-01",
  "status": "done",
  "facts": [],
  "deadends": [],
  "flags": [],
  "artifacts": [],
  "writeup_path": "/workspace/WRITEUP.md",
  "next_action": "..."
}
```

## 安全约束

- 只访问显式目标。
- 不把题目附件、flag、凭据上传到第三方。
- 不执行 `rm -rf`、`ssh`、真实主机扫描等危险动作，除非 operator 明确批准。
- 不在 worker 中保存真实模型 key。
- 所有非 `/workspace` 写入都应被 Docker 文件系统限制。
