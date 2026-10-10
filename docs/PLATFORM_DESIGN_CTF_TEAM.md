# D-Swarm CTF Team Collaboration Platform — 设计方案

**版本**: 1.0  
**日期**: 2026-10-10  
**状态**: 设计阶段

---

## 目录

1. [需求概述](#需求概述)
2. [核心数据模型](#核心数据模型)
3. [系统架构](#系统架构)
4. [权限与安全](#权限与安全)
5. [API 设计](#api-设计)
6. [数据库 Schema](#数据库-schema)
7. [分阶段实现计划](#分阶段实现计划)
8. [参考与借鉴](#参考与借鉴)

---

## 需求概述

### 目标

将 D-Swarm 从单人求解系统升级为**团队协作的 CTF 竞赛平台**，让团队成员在一个平台上：
- 共享题目信息（Target）和解题进展
- 各自独立执行求解任务（Run）
- 记录解题成果（Flag 和 Fact）
- 追踪资源消耗（Token 使用）
- 保留完整的审计日志

### 关键约束

| 约束 | 说明 |
|------|------|
| **团队规模** | 队长 1 人，队员最多 5 人 |
| **账号管理** | 无注册，Admin 创建账号；支持多个 Event（竞赛）|
| **隔离级别** | 队员间账号隔离；Run 独立沙箱；Event 间数据不共享 |
| **权限模型** | Admin（完全控制）+ Member（有限权限）|
| **安全需求** | API Key 个人隐私；Token 消耗个人可见；审计日志 Admin 可见 |
| **资源限制** | 每个账号最多 3 个并发 Run；Tag 消耗不计团队总额 |
| **部署** | 单机部署；可公网或内网；充足时间迭代 |

---

## 核心数据模型

### 1. Account 模型

```python
@dataclass
class Account:
    """用户账户"""
    id: str                          # UUID
    username: str                    # 唯一用户名
    display_name: str                # 显示名称
    role: str                        # "admin" | "member"
    password_hash: str               # bcrypt 哈希
    
    # API 密钥管理
    api_keys: dict[str, str]         # {provider -> key_id}（映射到 CredentialAccount）
    
    # 统计
    created_by: str                  # 创建者 ID（admin）
    created_at: float
    updated_at: float
    last_login: float | None
    
    # 状态
    is_active: bool                  # false = 禁用
    
    @property
    def can_create_target(self) -> bool:
        return self.is_active
    
    @property
    def can_delete_target(self) -> bool:
        return self.role == "admin"
    
    @property
    def can_view_audit(self) -> bool:
        return self.role == "admin"
```

### 2. Event 模型

```python
@dataclass
class Event:
    """竞赛事件"""
    id: str                          # UUID
    name: str                        # 事件名称（e.g., "DefCon 2026 Practice"）
    description: str
    status: str                      # "draft" | "active" | "archived"
    
    # 权限
    created_by: str                  # Admin ID
    
    # Target 集合
    target_ids: list[str]            # 此 Event 下的所有 Target
    
    # 统计
    created_at: float
    updated_at: float
    archived_at: float | None
    
    @property
    def is_visible_to_all(self) -> bool:
        """Event 对全体队员可见"""
        return self.status in {"active", "archived"}
```

### 3. Target 模型（扩展）

现有的 Target 需要扩展以支持团队协作：

```python
@dataclass
class Target:
    """竞赛题目"""
    id: str
    event_id: str                    # 归属的 Event
    name: str
    description: str
    category: str                    # "crypto" | "web" | "pwn" | ...
    
    # 题目信息
    target_address: str | None       # e.g., "example.com:8080"
    attachments: list[str]           # 附件 URL/路径
    expected_flags: int = 1
    flag_format_hint: str
    
    # 解题状态
    is_solved: bool = False          # 是否已由某队员解出
    solved_by: str | None            # 解出者 Account ID
    solved_at: float | None
    
    # 解题成果
    flags: list[str]                 # 已确认的 Flag 列表
    facts: list[dict]                # 已确认的 Fact 列表
    
    # 协作信息
    created_by: str                  # 创建者 Account ID
    created_at: float
    updated_at: float
    
    # 快照与审计
    snapshots: list[dict]            # Target 编辑历史快照
    edit_queue: dict                 # 当前排队编辑操作（防止并发冲突）
    
    # 关系
    run_ids: list[str]               # 此 Target 下的所有 Run
    
    @property
    def can_delete_by(self, account: Account) -> bool:
        """检查账户是否可删除此 Target"""
        if account.role == "admin":
            return True
        return account.id == self.created_by
```

### 4. Run 模型（扩展）

现有 Run 需要扩展支持多人共享：

```python
@dataclass
class Run:
    """单次解题尝试"""
    id: str
    target_id: str                   # 关联的 Target
    event_id: str                    # 冗余：便于查询
    
    # 执行者与隔离
    created_by: str                  # 执行者 Account ID
    created_at: float
    
    # 工作进展
    status: str                      # "running" | "completed" | "failed" | "stopped"
    
    # 成果
    solved: bool = False             # 是否成功解出
    flags_found: list[str]           # 找到的 Flag 列表
    facts: list[dict]                # 发现的 Fact 列表
    
    # 资源
    workspace_id: str                # 沙箱工作目录
    container_id: str | None         # Kali 容器 ID
    
    # Token 消耗追踪（个人隐私）
    token_usage: dict[str, int]      # {provider -> token_count}
    
    # 日志与输出
    logs: list[dict]                 # 运行日志（可被创建者清理）
    artifacts: list[str]             # 生成物 URL/路径
    
    is_deleted: bool = False         # 软删除标记（创建者可删除）
    
    @property
    def can_view_by(self, account: Account) -> bool:
        """检查账户是否可查看此 Run"""
        if account.role == "admin":
            return True
        return account.id == self.created_by
    
    @property
    def can_delete_by(self, account: Account) -> bool:
        """检查账户是否可删除此 Run"""
        if account.role == "admin":
            return True
        return account.id == self.created_by
```

### 5. TargetSnapshot 模型（编辑历史）

```python
@dataclass
class TargetSnapshot:
    """Target 编辑的快照记录"""
    id: str
    target_id: str
    
    # 编辑信息
    edited_by: str                   # 编辑者 Account ID
    edited_at: float
    
    # 变更内容（JSON Patch 或完整快照）
    changes: dict                    # {"field": "old_value" -> "new_value"}
    
    # 操作类型
    operation: str                   # "create" | "update" | "delete_run" | ...
    
    # 快照内容
    snapshot: dict                   # Target 的完整状态
```

### 6. AuditLog 模型

```python
@dataclass
class AuditLog:
    """审计日志（Admin 可见）"""
    id: str
    
    # 操作
    action: str                      # "account_create" | "target_edit" | "run_start" | ...
    actor: str                       # Account ID
    
    # 目标
    resource_type: str               # "target" | "run" | "event" | ...
    resource_id: str
    
    # 详情
    details: dict                    # 操作的具体参数
    timestamp: float
    ip_address: str | None           # 如果适用
    status: str                      # "success" | "failure"
```

---

## 系统架构

### 整体设计

```
┌─────────────────────────────────────────────────────────┐
│                  Admin Dashboard                        │
│  ├─ Account Management (创建/禁用/查看)                  │
│  ├─ Event Management (创建/管理)                         │
│  ├─ Audit Logs (查看所有操作)                            │
│  └─ Resource Monitor (并发 Run、Token 消耗)              │
├─────────────────────────────────────────────────────────┤
│                Member Dashboard                         │
│  ├─ Event & Target List                                │
│  ├─ Create/Delete Target (自己的)                       │
│  ├─ Launch Run                                         │
│  ├─ View Run Results                                   │
│  ├─ View Team Findings (共享的 Flag/Fact)               │
│  └─ Token Usage (个人统计)                              │
├─────────────────────────────────────────────────────────┤
│              API Layer (认证 Token)                      │
│  ├─ /api/auth/* (登录、token 管理)                      │
│  ├─ /api/accounts/* (Admin only)                       │
│  ├─ /api/events/* (Event CRUD)                         │
│  ├─ /api/targets/* (Target CRUD + snapshot)            │
│  ├─ /api/runs/* (Run 管理)                              │
│  └─ /api/audit/* (Admin only)                          │
├─────────────────────────────────────────────────────────┤
│           Core D-Swarm Solver Engine                   │
│  ├─ Shared Graph (append-only)                         │
│  ├─ Container Pool (Kali 沙箱)                         │
│  ├─ CliSolver (Pi Agent)                               │
│  └─ Provenance Gate (Flag validation)                  │
└─────────────────────────────────────────────────────────┘
```

### 关键分离点

1. **认证层**：新增多账户支持，每个账户有独立的 API token
2. **授权层**：基于 Role（admin/member）和 Resource ownership
3. **隔离层**：Event/Target/Run 间数据完全隔离
4. **审计层**：所有操作记录日志（Admin 可查）

---

## 权限与安全

### 权限矩阵

| 操作 | Admin | Member | 说明 |
|------|--------|--------|------|
| 创建 Account | ✓ | ✗ | Admin 创建，无注册 |
| 禁用 Account | ✓ | ✗ | Admin 禁用 |
| 查看 Audit Log | ✓ | ✗ | 完整审计 |
| 创建 Event | ✓ | ✗ | 竞赛事件管理 |
| 查看 Event | ✓ | ✓ | 所有 Event 可见 |
| 创建 Target | ✓ | ✓ | 所有人可创建 |
| 编辑 Target | ✓ 全部 | Member 自己的 | 排队编辑防冲突 |
| 删除 Target | ✓ 全部 | 自己的 | 级联删除 Run |
| 查看 Target | ✓ | ✓ | 全员可见 |
| 创建 Run | ✓ | ✓ | 所有人可创建 |
| 查看 Run | ✓ 全部 | 自己的 + Flag/Fact | 隐私边界 |
| 删除 Run | ✓ 全部 | 自己的 | 工作目录清理 |
| 查看 API Key | Account 自己 + Admin | 自己 | 严格隐私 |
| 查看 Token 消耗 | Account 自己 + Admin | 自己 | 严格隐私 |
| 查看容器使用 | ✓ | ✗ | 资源监控 |

### 认证方案

```python
# 改进的认证流程

# 1. 首次登录（静态密码）
POST /api/auth/login
{
    "username": "alice",
    "password": "plain_password"
}

# Response
{
    "access_token": "jwt_token_long_lived",
    "refresh_token": "jwt_token_short_lived",
    "expires_in": 86400,
    "account_id": "acc_xxx",
    "role": "member"
}

# 2. 后续请求
GET /api/events
Authorization: Bearer {access_token}

# 3. Token 刷新
POST /api/auth/refresh
Authorization: Bearer {refresh_token}

# 4. API Key 配置（成员自管）
POST /api/accounts/me/api-keys
{
    "provider": "openai",
    "key": "sk-..."
}

# Response（不返回 key，只返回 ID）
{
    "key_id": "key_xxx",
    "provider": "openai",
    "created_at": 1697234567
}
```

### 隐私与隐藏

| 数据 | 谁能看 | 谁不能看 | 存储方式 |
|------|--------|--------|---------|
| API Key | Account 自己 | 任何人（包括 Admin） | 加密存储 + 可撤销 |
| Token 消耗 | Account 自己 + Admin | 其他成员 | 个人日志 |
| Run 日志 | Run 创建者 + Admin | 其他成员 | 工作目录 |
| Target Flag | 全队成员 | 外部 | Target 表 |
| Audit Log | Admin only | 任何人 | 审计表 |
| 编辑历史 | Admin + 编辑者 | 其他成员 | Snapshot 表 |

---

## API 设计

### 认证 API

```python
# GET /api/auth/me — 获取当前用户信息
Response 200:
{
    "id": "acc_xxx",
    "username": "alice",
    "display_name": "Alice Chen",
    "role": "member",
    "created_at": 1697234567,
    "last_login": 1697334567
}

# POST /api/auth/logout — 登出
Response 200: { "status": "ok" }

# PUT /api/accounts/me/password — 修改密码
Request:
{
    "old_password": "...",
    "new_password": "..."
}
Response 200: { "status": "ok" }
```

### Account 管理 API（Admin only）

```python
# GET /api/accounts — 列表所有账户
Response 200:
[
    {
        "id": "acc_alice",
        "username": "alice",
        "display_name": "Alice Chen",
        "role": "member",
        "is_active": true,
        "created_at": 1697234567,
        "last_login": 1697334567
    },
    ...
]

# POST /api/accounts — 创建新账户
Request:
{
    "username": "bob",
    "display_name": "Bob Smith",
    "password": "random_generated",
    "role": "member"
}
Response 201:
{
    "id": "acc_bob",
    "username": "bob",
    "temporary_password": "..." # Admin 需要告知 Bob
}

# PATCH /api/accounts/{account_id} — 禁用/启用账户
Request:
{
    "is_active": false
}
Response 200: { "status": "ok" }

# DELETE /api/accounts/{account_id} — 删除账户（级联删除其 Target/Run）
Response 204

# GET /api/accounts/{account_id}/usage — 查看某成员的资源使用
Response 200:
{
    "account_id": "acc_bob",
    "token_usage": {
        "openai": 50000,
        "anthropic": 30000
    },
    "concurrent_runs": 1,
    "total_runs": 5,
    "container_memory_mb": 512
}
```

### Event API

```python
# GET /api/events — 列表所有事件
Response 200:
[
    {
        "id": "evt_defcon2026",
        "name": "DefCon 2026 Practice",
        "status": "active",
        "target_count": 42,
        "created_by": "acc_admin",
        "created_at": 1697234567
    }
]

# POST /api/events — 创建事件（Admin only）
Request:
{
    "name": "DefCon 2026",
    "description": "...",
    "status": "active"
}
Response 201: { "id": "evt_xxx", ... }

# PATCH /api/events/{event_id} — 编辑事件（Admin only）
Request: { "status": "archived" }
Response 200: { ... }

# DELETE /api/events/{event_id} — 删除事件及其所有 Target/Run（Admin only）
Response 204
```

### Target API

```python
# GET /api/events/{event_id}/targets — 列表某事件的所有题目
Response 200:
[
    {
        "id": "tgt_xxx",
        "event_id": "evt_xxx",
        "name": "Web Challenge #1",
        "description": "SQL injection in login form",
        "category": "web",
        "is_solved": true,
        "solved_by": "acc_alice",
        "solved_at": 1697334567,
        "flags": ["flag{...}"],
        "facts": [...],
        "run_count": 3,
        "created_by": "acc_alice",
        "created_at": 1697234567
    }
]

# POST /api/events/{event_id}/targets — 创建新题目
Request:
{
    "name": "Crypto Challenge",
    "description": "RSA key recovery",
    "category": "crypto",
    "target_address": "none",
    "attachments": ["file.zip"],
    "flag_format_hint": "flag{...}"
}
Response 201: { "id": "tgt_xxx", ... }

# GET /api/targets/{target_id} — 获取题目详情
Response 200: { ... }

# PUT /api/targets/{target_id} — 编辑题目（排队处理）
Request:
{
    "description": "Updated description",
    "category": "crypto_advanced"
}
Response 200:
{
    "id": "tgt_xxx",
    "status": "editing",
    "edit_queue_position": 0
}

# DELETE /api/targets/{target_id} — 删除题目及其 Run（按权限）
Response 204

# GET /api/targets/{target_id}/snapshots — 获取编辑历史（Admin only）
Response 200:
[
    {
        "id": "snap_xxx",
        "edited_by": "acc_alice",
        "edited_at": 1697334567,
        "operation": "update",
        "changes": {
            "description": ["old", "new"],
            "category": ["web", "crypto"]
        }
    }
]

# POST /api/targets/{target_id}/flag — 上传 Flag（来自 Run 或本地 Agent）
Request:
{
    "flag": "flag{...}",
    "source": "run" | "local_agent",
    "run_id": "run_xxx" | null
}
Response 200 | 400: # Provenance gate 检验

# POST /api/targets/{target_id}/fact — 上传 Fact（发现记录）
Request:
{
    "fact": "SQL injection point found in /login endpoint",
    "source": "run" | "local_agent",
    "run_id": "run_xxx" | null
}
Response 201: { "id": "fact_xxx", ... }
```

### Run API

```python
# POST /api/targets/{target_id}/runs — 启动新的解题尝试
Request:
{
    "use_local_api_keys": true | false
}
Response 201:
{
    "id": "run_xxx",
    "target_id": "tgt_xxx",
    "status": "running",
    "created_at": 1697234567,
    "workspace_id": "ws_xxx"
}

# GET /api/runs/{run_id} — 获取 Run 详情（权限检查）
Response 200:
{
    "id": "run_xxx",
    "target_id": "tgt_xxx",
    "created_by": "acc_alice",
    "status": "running",
    "solved": false,
    "flags_found": [],
    "facts": [],
    "created_at": 1697234567
}

# GET /api/runs/{run_id}/logs — 获取 Run 日志（创建者可见）
Response 200:
{
    "logs": [
        { "timestamp": 1697234568, "level": "info", "message": "..." },
        ...
    ]
}

# DELETE /api/runs/{run_id} — 删除 Run 及其工作目录（按权限）
Response 204

# GET /api/accounts/me/runs — 获取自己的所有 Run（分页）
Response 200:
[
    { "id": "run_xxx", "target_id": "tgt_xxx", "status": "completed", ... },
    ...
]

# GET /api/accounts/me/token-usage — 获取个人 Token 消耗
Response 200:
{
    "openai": { "tokens": 50000, "model": "gpt-4", "cost_estimate": "$1.50" },
    "anthropic": { "tokens": 30000, "model": "claude-3", "cost_estimate": "$0.90" }
}
```

### 本地 Agent API

```python
# GET /api/external/targets/{target_id} — 本地 Agent 获取题目信息
Headers: Authorization: Bearer {api_token}
Response 200:
{
    "id": "tgt_xxx",
    "name": "Web Challenge",
    "description": "...",
    "target_address": "example.com:8080",
    "attachments": [...],
    "flag_format_hint": "flag{...}",
    "current_flags": [...],  # 已解出的 Flag（供参考）
    "current_facts": [...]   # 已有的 Fact（供参考）
}

# POST /api/external/targets/{target_id}/submit — 本地 Agent 提交成果
Headers: Authorization: Bearer {api_token}
Request:
{
    "type": "flag" | "fact",
    "content": "flag{...}" | "fact content",
    "metadata": {
        "agent_name": "MyCustomAgent",
        "execution_time_s": 300,
        "logs": "..." # 可选的执行日志
    }
}
Response 200:
{
    "id": "submission_xxx",
    "accepted": true | false,
    "message": "flag validated successfully" | "fact recorded (not verified)",
    "target_status": "solved" | "in_progress"
}
```

### Audit API（Admin only）

```python
# GET /api/audit/logs — 查看审计日志
Query params:
  - action: "account_create" | "target_edit" | "run_start" | ...
  - actor: account_id
  - resource_type: "target" | "run" | "event" | ...
  - date_from: timestamp
  - date_to: timestamp

Response 200:
[
    {
        "id": "log_xxx",
        "action": "target_edit",
        "actor": "acc_alice",
        "resource_type": "target",
        "resource_id": "tgt_xxx",
        "details": { ... },
        "timestamp": 1697334567,
        "status": "success"
    },
    ...
]
```

---

## 数据库 Schema

### 现有 D-Swarm 的保留表

- `events` (shared_graph.py 中的 Event)
- `intents`, `facts`, `pocs` (核心证据图)
- `session_store` (Run 会话数据)

### 新增表

```sql
-- 账户表
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT CHECK (role IN ('admin', 'member')) NOT NULL,
    
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_login REAL,
    
    is_active INTEGER DEFAULT 1,
    
    FOREIGN KEY (created_by) REFERENCES accounts(id)
);

-- API 密钥映射表（不存储密钥本身，存储在 CredentialAccount）
CREATE TABLE IF NOT EXISTS account_api_keys (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    provider TEXT NOT NULL,  -- "openai" | "anthropic" | ...
    credential_account_id TEXT NOT NULL,  -- 指向 dswarm.CredentialAccount
    
    created_at REAL NOT NULL,
    revoked_at REAL,
    
    UNIQUE (account_id, provider),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

-- Event 表（扩展）
CREATE TABLE IF NOT EXISTS ctf_events (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT CHECK (status IN ('draft', 'active', 'archived')) DEFAULT 'draft',
    
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    
    FOREIGN KEY (created_by) REFERENCES accounts(id)
);

-- Target 表（扩展）
CREATE TABLE IF NOT EXISTS ctf_targets (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT,
    
    target_address TEXT,
    attachments TEXT,  -- JSON 数组
    expected_flags INTEGER DEFAULT 1,
    flag_format_hint TEXT,
    
    is_solved INTEGER DEFAULT 0,
    solved_by TEXT,
    solved_at REAL,
    
    flags TEXT,  -- JSON 数组
    facts TEXT,  -- JSON 数组
    
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    
    run_ids TEXT,  -- JSON 数组
    
    FOREIGN KEY (event_id) REFERENCES ctf_events(id),
    FOREIGN KEY (created_by) REFERENCES accounts(id),
    FOREIGN KEY (solved_by) REFERENCES accounts(id)
);

-- Target 编辑队列表（防并发冲突）
CREATE TABLE IF NOT EXISTS target_edit_queue (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    
    editor_account_id TEXT NOT NULL,
    queued_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    
    operation TEXT,  -- "update" | ...
    changes TEXT,  -- JSON
    
    status TEXT CHECK (status IN ('queued', 'in_progress', 'completed', 'failed')) DEFAULT 'queued',
    
    UNIQUE (target_id, editor_account_id, queued_at),
    FOREIGN KEY (target_id) REFERENCES ctf_targets(id),
    FOREIGN KEY (editor_account_id) REFERENCES accounts(id)
);

-- Target 快照表（编辑历史）
CREATE TABLE IF NOT EXISTS target_snapshots (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    
    edited_by TEXT NOT NULL,
    edited_at REAL NOT NULL,
    
    operation TEXT,  -- "create" | "update" | "delete_run" | ...
    changes TEXT,  -- JSON: {"field": {"old": ..., "new": ...}}
    snapshot TEXT,  -- JSON: Target 的完整状态快照
    
    FOREIGN KEY (target_id) REFERENCES ctf_targets(id),
    FOREIGN KEY (edited_by) REFERENCES accounts(id)
);

-- Run 表（扩展）
CREATE TABLE IF NOT EXISTS ctf_runs (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    
    status TEXT CHECK (status IN ('running', 'completed', 'failed', 'stopped')) DEFAULT 'running',
    
    solved INTEGER DEFAULT 0,
    flags_found TEXT,  -- JSON 数组
    facts TEXT,  -- JSON 数组
    
    workspace_id TEXT,
    container_id TEXT,
    
    token_usage TEXT,  -- JSON: {"openai": 50000, "anthropic": 30000}
    
    logs_location TEXT,  -- 日志文件路径
    artifacts TEXT,  -- JSON 数组
    
    is_deleted INTEGER DEFAULT 0,
    
    FOREIGN KEY (target_id) REFERENCES ctf_targets(id),
    FOREIGN KEY (event_id) REFERENCES ctf_events(id),
    FOREIGN KEY (created_by) REFERENCES accounts(id)
);

-- 审计日志表
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    
    action TEXT NOT NULL,  -- "account_create" | "target_edit" | "run_start" | ...
    actor TEXT NOT NULL,
    
    resource_type TEXT,  -- "target" | "run" | "event" | ...
    resource_id TEXT,
    
    details TEXT,  -- JSON
    timestamp REAL NOT NULL,
    ip_address TEXT,
    status TEXT CHECK (status IN ('success', 'failure')) DEFAULT 'success',
    
    FOREIGN KEY (actor) REFERENCES accounts(id)
);

-- Token 消耗日志表（个人隐私）
CREATE TABLE IF NOT EXISTS token_usage_logs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    
    provider TEXT NOT NULL,  -- "openai" | "anthropic" | ...
    model TEXT,
    tokens_used INTEGER,
    
    run_id TEXT,  -- 关联的 Run（如果有）
    timestamp REAL NOT NULL,
    
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (run_id) REFERENCES ctf_runs(id)
);
```

---

## 分阶段实现计划

### 第一阶段：基础认证与账户管理（3 周）

**目标**：支持多账户登录和基本权限管理。

#### 1.1 账户认证系统
- [ ] 扩展 `apps/web/auth.py`：支持多账户、密码哈希（bcrypt）
- [ ] JWT token 生成与验证（替换或并行现有 HMAC scheme）
- [ ] 登录 API 端点 (`/api/auth/login`, `/api/auth/logout`)
- [ ] 密码修改接口
- [ ] 测试：单元测试 token 生命周期，集成测试登录流

**参考代码**：
- GZCTF: `src/Models/AccountModel.cs`（用户角色模型）
- 现有：`apps/web/auth.py`（扩展而非替换）

#### 1.2 账户管理 API（Admin only）
- [ ] Account 数据模型（Role、is_active）
- [ ] Admin 创建/禁用账户接口
- [ ] 账户列表接口（分页）
- [ ] 数据库迁移：新增 `accounts`, `account_api_keys` 表
- [ ] 测试：权限检查、级联删除

**参考代码**：
- GZCTF: `src/Controllers/AccountController.cs`（账户管理）

#### 1.3 Web UI 改造
- [ ] 登录页面（username/password）
- [ ] 账户管理面板（Admin only）
- [ ] 个人资料页

**可复用的 UI 代码**：
- 现有 Next.js 框架（`apps/web/ui/`）

---

### 第二阶段：Event & Target 重组织（3 周）

**目标**：将题目从全局视图改为按 Event 组织，支持多人创建和权限隔离。

#### 2.1 数据模型迁移
- [ ] Event 表和模型定义（独立于原有的 Event 概念）
- [ ] Target 表扩展：`event_id`, `created_by`, `is_solved`, `flags/facts` 字段
- [ ] 数据库迁移脚本（处理向后兼容）
- [ ] Target 快照表（编辑历史）
- [ ] 编辑队列表（防并发冲突）

**参考代码**：
- GZCTF: `src/Models/ChallengeModel.cs`（题目模型）
- 现有：`apps/web/targets/models.py`（扩展）

#### 2.2 Event API
- [ ] 创建/列表/编辑/删除 Event
- [ ] Event 状态管理（draft → active → archived）
- [ ] 级联删除（删除 Event 时删除所有 Target/Run）

#### 2.3 Target API 与权限
- [ ] Target CRUD 接口
- [ ] 权限检查：
  - 创建：所有认证用户
  - 编辑：创建者 + Admin（排队处理）
  - 删除：创建者 + Admin
  - 查看：所有人
- [ ] 编辑队列管理（FIFO 防冲突）
- [ ] Target 快照记录每次编辑

#### 2.4 题目信息管理
- [ ] 题目附件管理（文件上传/下载）
- [ ] Flag 记录（自动与 Provenance gate 集成）
- [ ] Fact 记录（来自 Run 或本地 Agent）
- [ ] Flag/Fact 全队可见

#### 2.5 测试
- [ ] Target 权限单元测试
- [ ] 编辑队列并发测试
- [ ] 级联删除集成测试

---

### 第三阶段：Run 与隔离管理（3 周）

**目标**：Run 支持多人隐私、Token 追踪、资源限制。

#### 3.1 Run 表扩展
- [ ] `created_by` 字段（隐私边界）
- [ ] `token_usage` 字段（每个 Run 的 Token 消耗）
- [ ] `is_deleted` 字段（软删除）
- [ ] Run 与 Account 的多对多关系

#### 3.2 Run API 与权限
- [ ] 创建 Run：所有认证用户
- [ ] 查看 Run：创建者 + Admin（查看日志）
- [ ] 删除 Run：创建者 + Admin
- [ ] 列表 Run：个人的 Run 列表（`/api/accounts/me/runs`）
- [ ] Run 并发限制：每个账号最多 3 个

#### 3.3 Token 追踪
- [ ] 新增 `token_usage_logs` 表
- [ ] 每个 Run 执行时记录 Token 消耗（按 provider）
- [ ] 个人 Token 统计接口（`/api/accounts/me/token-usage`）
- [ ] Admin 可查看全队 Token 消耗（但看不到具体 key）

#### 3.4 资源监控（Admin 仪表板）
- [ ] 并发 Run 数统计
- [ ] 每个成员的容器资源占用
- [ ] Token 消耗趋势

#### 3.5 测试
- [ ] Run 权限隔离测试
- [ ] Token 追踪准确性测试
- [ ] 并发 Run 限制测试

---

### 第四阶段：本地 Agent API 与集成（2 周）

**目标**：支持本地 Agent 通过 API 获取题目信息和上传成果。

#### 4.1 外部 API 设计
- [ ] `/api/external/targets/{target_id}` — 获取题目信息（需 API token）
- [ ] `/api/external/targets/{target_id}/submit` — 提交 Flag/Fact（需 API token）
- [ ] 端点认证：使用账户的 API token（不同于 Web session token）

#### 4.2 API Token 管理
- [ ] Account 可生成多个 API token（长期有效）
- [ ] API token 绑定到账户（追踪来源）
- [ ] Token 撤销功能
- [ ] Token 自动过期（可选）

#### 4.3 本地 Agent 集成
- [ ] 客户端库或文档（如何调用 API）
- [ ] Flag 提交时的 Provenance 处理（暂时直接记为 Fact）
- [ ] Metadata 记录（agent 名称、执行时间、日志）

#### 4.4 测试
- [ ] 本地 API 权限测试
- [ ] Flag/Fact 提交验证
- [ ] Token 追踪测试

---

### 第五阶段：审计与 Admin 面板（2 周）

**目标**：完整的审计日志和 Admin 管理界面。

#### 5.1 审计日志系统
- [ ] 新增 `audit_logs` 表
- [ ] 记录所有关键操作（账户、Event、Target、Run）
- [ ] 操作细节 JSON 化（易于查询）
- [ ] `/api/audit/logs` 端点（Admin only，支持过滤）

#### 5.2 Admin 仪表板
- [ ] 账户管理页面
- [ ] Event 管理页面
- [ ] Resource Monitor（并发 Run、容器、Token）
- [ ] Audit Log 查看器
- [ ] 快照回溯（查看 Target 编辑历史）

#### 5.3 测试
- [ ] 审计日志准确性测试
- [ ] Admin 权限隔离测试

---

### 第六阶段：优化与波兰（1 周）

**目标**：性能、UI 打磨、文档。

#### 6.1 性能优化
- [ ] 数据库索引（event_id, created_by, timestamp）
- [ ] API 响应缓存（如适用）
- [ ] 并发限制的实现（Redis 或内存）

#### 6.2 UI/UX
- [ ] 响应式设计
- [ ] 错误提示完善
- [ ] 加载动画

#### 6.3 文档
- [ ] API 文档（OpenAPI/Swagger）
- [ ] 部署指南
- [ ] 用户手册

---

## 参考与借鉴

### 1. GZCTF 项目
**地址**：https://github.com/GZTimeWalker/GZCTF

**核心借鉴**：
- 多队伍、多题目的数据模型组织
- 队伍权限管理（Team role 系统）
- 题目状态追踪（Solved by, time, points）
- Scoreboard 与实时更新

**相关代码**：
- `src/Models/AccountModel.cs` — 用户角色模型
- `src/Models/ChallengeModel.cs` — 题目模型
- `src/Models/SubmissionModel.cs` — 提交记录
- `src/Controllers/` — API 端点示例

**可复用的设计**：
- Role-based access control (RBAC)
- 题目分类与组织方式
- 提交与验证流程

### 2. CTFd 项目
**地址**：https://github.com/CTFd/CTFd

**核心借鉴**：
- 插件系统（可扩展的验证器）
- 动态评分模式
- 队伍与用户的分离

### 3. D-Swarm 现有设计

**保留与扩展**：
- `apps/web/auth.py` — 现有认证机制（扩展为多账户）
- `apps/web/targets/` — Target 模型（扩展权限字段）
- `apps/web/run_manager.py` — Run 管理（添加隐私隔离）
- `dswarm/core/session_store.py` — 会话持久化（复用）
- Provenance gate — Flag 验证（复用不改）

---

## 后续决策点

1. **认证方案选择**：
   - JWT vs Session-based cookies?
   - 建议：JWT（易于 API token，适合本地 Agent）

2. **数据库选择**：
   - SQLite（当前 D-Swarm 用的）vs PostgreSQL?
   - 建议：单机部署初期用 SQLite，后续可迁移 Postgres

3. **编辑冲突处理**：
   - FIFO 队列（阻塞）vs Operational Transformation（复杂）?
   - 建议：FIFO 队列（简单，足够用于 5 人团队）

4. **Token 消耗追踪**：
   - 实时追踪 vs 事后统计?
   - 建议：事后统计（减少开销），定期聚合

5. **API Key 存储**：
   - 加密存储 vs 外部 Vault（Hashicorp Vault）?
   - 建议：加密存储（Fernet）+ 单机部署

---

## 总结

这份设计方案围绕**多人协作 + 权限隔离 + 审计追踪**的核心需求，通过分阶段实现逐步达成。

**关键里程碑**：
- **第一阶段**（3w）：多账户认证，基础权限
- **第二阶段**（3w）：Event/Target 重组织
- **第三阶段**（3w）：Run 隐私 + Token 追踪
- **第四阶段**（2w）：本地 API 集成
- **第五阶段**（2w）：审计与 Admin 面板
- **第六阶段**（1w）：优化与文档

**总工期**：14 周（充足的迭代时间内）

**风险最低的实现路径**：
1. 保持现有 D-Swarm 的核心架构不动
2. 在 `apps/web/` 层增加新的账户、事件、权限模块
3. 复用 GZCTF 和 CTFd 的成熟模式，避免重复造轮子
4. 定期验证与现有 Pi Agent 的集成

