# D-Swarm CTF 团队平台 — 实现指南与技术决策

**版本**: 1.0  
**日期**: 2026-10-10

---

## 快速导航

- [技术栈确认](#技术栈确认)
- [与现有 D-Swarm 的集成点](#与现有-d-swarm-的集成点)
- [社区代码复用清单](#社区代码复用清单)
- [实现中的常见坑](#实现中的常见坑)
- [验收测试清单](#验收测试清单)

---

## 技术栈确认

### 后端

| 组件 | 选型 | 理由 |
|------|------|------|
| **Web 框架** | FastAPI（保持现有） | 异步、自动文档、与 D-Swarm 一致 |
| **认证** | JWT + Refresh Token | 适合本地 API token，无状态 |
| **数据库** | SQLite + SQL Alchemy ORM | 单机部署；后续可迁移 Postgres |
| **密钥存储** | Fernet 加密（cryptography）| 简单、内置 Python |
| **密码哈希** | bcrypt（passlib）| 行业标准 |
| **会话持久化** | SQLite（现有 SessionStore）| 复用不改 |

### 前端

| 组件 | 选型 | 理由 |
|------|------|------|
| **框架** | Next.js 13+（保持现有） | React、TypeScript、SSR |
| **认证状态** | zustand | 轻量级状态管理 |
| **HTTP 客户端** | axios + 拦截器 | Token 刷新、错误处理 |
| **UI 组件** | shadcn/ui（如已用）或 antd | 保持一致性 |

### 部署

| 组件 | 选型 | 理由 |
|------|------|------|
| **容器化** | Docker + docker-compose | 可选；单机部署也支持 |
| **反向代理** | nginx（可选）| 静态资源、HTTPS |
| **进程管理** | systemd 或 supervisor | 生产部署 |

---

## 与现有 D-Swarm 的集成点

### 不能动的（核心不变）

```
✓ dswarm/swarm/shared_graph.py    — Append-only event graph（核心）
✓ dswarm/solver/gate.py           — Provenance gate（绝对不动）
✓ dswarm/solver/cli_driver.py     — CLI agent 执行
✓ dswarm/solver/container_pool.py — Kali 沙箱管理
✓ dswarm/core/session_store.py    — 会话存储（可复用）
✓ dswarm/core/usage_ledger.py     — Token 成本追踪（扩展而非改）
```

### 可以扩展的（新增字段 + 关联）

```
~ apps/web/run_manager.py
  └─ 新增: Run.created_by, Run.event_id, Run.token_usage
  └─ 新增: 权限检查 (can_view_by, can_delete_by)

~ apps/web/targets/models.py
  └─ 新增: Target.event_id, Target.created_by, Target.is_solved, Target.flags/facts
  └─ 新增: 权限检查、编辑队列、快照

~ apps/web/auth.py
  └─ 扩展: 支持多账户而非单密码
  └─ 新增: Account 模型与数据库层
```

### 新增模块（完全独立）

```
✚ apps/web/accounts/        — 账户管理（新模块）
✚ apps/web/events/          — Event 管理（新模块）
✚ apps/web/audit/           — 审计日志（新模块）
✚ apps/web/external_api/    — 本地 Agent API（新模块）
✚ dswarm/models/account.py  — 账户域模型（新）
```

---

## 社区代码复用清单

### GZCTF 代码参考

**推荐直接参考或改编的文件**：

```python
# 1. 账户模型 & 认证
# GZCTF: src/Models/AccountModel.cs
# 改编为 Python，参考设计:
# - Role enum (Admin, Member)
# - PasswordHasher 集成
# - LastLoginAt 追踪

# 2. 题目与队伍关系
# GZCTF: src/Models/ChallengeModel.cs
# 参考设计:
# - SolvedByTeam 追踪
# - SolvedAt 时间戳
# - 多 Flag 支持

# 3. 提交与验证
# GZCTF: src/Models/SubmissionModel.cs
# 参考设计:
# - Submission 记录
# - 与 Provenance gate 的互操作

# 4. Team 权限管理
# GZCTF: src/Services/TeamService.cs
# 参考设计:
# - Role-based access control
# - 队伍成员管理
```

**不建议直接用的（C# 特定）**：
- 整个 `src/Controllers/` 目录（改用 FastAPI）
- Entity Framework ORM（改用 SQLAlchemy）

### CTFd 代码参考

**推荐参考的模式**：

```python
# 1. 插件系统
# 如需自定义 Flag 验证器，参考 CTFd 的插件架构
# 但暂不实现（保持简单）

# 2. 题目与团队关系
# CTFd: ctf.models
# 参考分离 User、Team、Challenge 的方式

# 3. 动态评分（可选，第二阶段）
# CTFd: scorebarding.py
# 参考如何计算实时排名
```

### 现有 D-Swarm 代码复用

**直接复用（无改造）**：

```python
# apps/web/auth.py
→ 复用 _derive_secret(), constant_time_compare()

# dswarm/core/session_store.py
→ 复用 SessionStore 类（添加 account_id 字段）

# dswarm/solver/credential_accounts.py
→ 复用 CredentialAccount 存储机制（新增账户关联）

# dswarm/core/usage_journal.py
→ 扩展添加账户 ID，追踪个人 Token 消耗
```

**改进版复用**：

```python
# apps/web/run_manager.py
→ 复用 Run 模型，新增:
  - run.created_by (Account ID)
  - run.token_usage (按 provider 统计)
  - 权限检查方法

# apps/web/targets/models.py
→ 复用 Target 模型，新增:
  - target.event_id
  - target.created_by
  - target.is_solved
  - target.flags (Array)
  - target.facts (Array)
  - 快照机制
```

---

## 实现中的常见坑

### 1. 认证与授权混淆

**坑**：在 API 中混淆认证（你是谁）和授权（你能做什么）。

**案例**：
```python
# ❌ 错误做法
@app.get("/api/targets/{target_id}")
async def get_target(target_id: str, token: str):
    if not verify_token(token):
        return 401
    return target  # 没有检查是否可访问

# ✓ 正确做法
@app.get("/api/targets/{target_id}")
async def get_target(target_id: str, current_account: Account = Depends(get_current_account)):
    target = get_target_or_404(target_id)
    if not target.can_view_by(current_account):  # 授权检查
        return 403
    return target
```

**避免方法**：
- 每个 API 都显式检查权限
- 创建 `can_view_by(account)` 等方法在模型上
- 写单元测试验证权限边界

### 2. API Key 与 Session Token 混用

**坑**：用一种 token 同时处理 Web 会话和本地 API 认证。

**案例**：
```python
# ❌ 错误做法
def generate_token(account_id):
    # 同一个函数生成 session token 和 API token
    return jwt.encode({"account_id": account_id, "exp": now + 1h})

# ✓ 正确做法
def generate_session_token(account_id):
    return jwt.encode({
        "account_id": account_id, 
        "type": "session",
        "exp": now + 12h
    })

def generate_api_token(account_id):
    return jwt.encode({
        "account_id": account_id,
        "type": "api",
        "exp": now + 365d  # 长期
    })

def verify_token(token, expected_type: str):
    payload = jwt.decode(token, ...)
    if payload.get("type") != expected_type:
        raise InvalidToken()
```

**避免方法**：
- JWT 负载中包含 `type` 字段
- 验证函数参数化 `expected_type`
- 不同端点检查不同 token 类型

### 3. Target 编辑的并发冲突

**坑**：多人同时编辑 Target，导致某人的改动被覆盖。

**案例**：
```
时间    Alice (Web UI)           Bob (API)
T0      读取 Target v1
T1                               读取 Target v1
T2      修改 description
T3                               修改 category
T4      PUT /api/targets/xx      PUT /api/targets/xx
        写入: description=new    写入: category=new
T5      ↑ Bob 的改动丢失！       ↑ Alice 的改动丢失！
```

**避免方法**：编辑队列（FIFO）

```python
# 不支持并发编辑，排队处理
async def put_target(target_id: str, changes: dict, account: Account):
    target = get_target_or_404(target_id)
    
    # 检查权限
    if not (account.role == "admin" or account.id == target.created_by):
        return 403
    
    # 加入编辑队列（FIFO）
    queue_entry = TargetEditQueue(
        target_id=target_id,
        editor_account_id=account.id,
        queued_at=now(),
        operation="update",
        changes=changes
    )
    db.add(queue_entry)
    db.commit()
    
    # 返回队列位置（让客户端轮询）
    return {
        "status": "queued",
        "position": get_queue_position(target_id, account.id)
    }

# 后台任务定期处理队列
async def process_edit_queue():
    while True:
        entry = db.query(TargetEditQueue).filter_by(status="queued").first()
        if not entry:
            await asyncio.sleep(1)
            continue
        
        entry.status = "in_progress"
        entry.started_at = now()
        db.commit()
        
        try:
            # 应用变更
            target = get_target_or_404(entry.target_id)
            for field, new_value in entry.changes.items():
                setattr(target, field, new_value)
            
            # 记录快照
            snapshot = TargetSnapshot(
                target_id=entry.target_id,
                edited_by=entry.editor_account_id,
                edited_at=now(),
                operation="update",
                changes=entry.changes,
                snapshot=target.to_dict()
            )
            db.add(snapshot)
            
            entry.status = "completed"
            entry.completed_at = now()
        except Exception as e:
            entry.status = "failed"
            log_error(e)
        finally:
            db.commit()
```

**避免方法**：
- 编辑操作不立即生效，加入队列
- 单个后台任务 FIFO 处理队列
- 快照记录每次编辑
- 客户端轮询获取处理状态

### 4. Flag 验证的两重检查

**坑**：本地 Agent 上传 Flag，不经 Provenance gate。

**案例**：
```python
# ❌ 错误做法
@app.post("/api/external/targets/{target_id}/submit")
async def submit_result(target_id: str, payload: dict, account: Account):
    if payload["type"] == "flag":
        # 直接相信本地 Agent 上传的 Flag！
        target.flags.append(payload["content"])
        target.is_solved = True
        db.commit()
        return {"accepted": True}

# ✓ 正确做法
@app.post("/api/external/targets/{target_id}/submit")
async def submit_result(target_id: str, payload: dict, account: Account):
    if payload["type"] == "flag":
        # 先验证，再记录
        from dswarm.solver.gate import flag_ok
        
        flag = payload["content"]
        if not flag_ok(flag):
            return {
                "accepted": False, 
                "message": "flag format invalid or suspicious"
            }
        
        # 只有通过 Provenance gate 才记录
        target.flags.append(flag)
        target.is_solved = True
        
        # 记录审计日志
        audit_log = AuditLog(
            action="flag_submitted",
            actor=account.id,
            resource_type="target",
            resource_id=target_id,
            details={"source": "external_api", "agent": payload.get("agent_name")},
            timestamp=now(),
            status="success"
        )
        db.add(audit_log)
        db.commit()
        
        return {"accepted": True}
    else:
        # Fact 不经 gate，直接记录
        target.facts.append({
            "content": payload["content"],
            "submitted_by": account.id,
            "timestamp": now(),
            "metadata": payload.get("metadata", {})
        })
        db.commit()
        return {"accepted": True}
```

**避免方法**：
- Flag 上传 → Provenance gate 验证 → 记录
- Fact 上传 → 直接记录（标记为"未验证"）
- 在 UI 中区分"已验证 Flag"和"待验证 Fact"

### 5. Token 消耗追踪的延迟

**坑**：实时追踪 Token，每个 Run 的每步都写数据库，导致性能下降。

**案例**：
```python
# ❌ 实时追踪（性能差）
async def on_run_step(run_id: str, tokens_used: int):
    log = TokenUsageLog(
        run_id=run_id,
        tokens_used=tokens_used,
        timestamp=now()
    )
    db.add(log)
    db.commit()  # 每次都 flush，性能差！

# ✓ 批量追踪（性能好）
# 在 CliSolver 中缓存 Token 统计
class CliSolver:
    def __init__(self):
        self.token_buffer = {}  # {provider: count}
    
    async def on_step(self, step):
        # 缓存在内存
        self.token_buffer[provider] += tokens
    
    async def on_run_complete(self, run_id):
        # Run 完成时，一次性写入
        for provider, count in self.token_buffer.items():
            log = TokenUsageLog(
                run_id=run_id,
                provider=provider,
                tokens_used=count,
                timestamp=now()
            )
            db.add(log)
        db.commit()  # 一次 flush，性能好
```

**避免方法**：
- 内存缓冲 + 定期 flush
- 或异步队列（如 Celery/RQ）+ 后台聚合
- 每个 Run 结束时汇总一次

### 6. API Key 的加密存储

**坑**：API Key 明文存储，被数据库泄露后直接能用。

**案例**：
```python
# ❌ 明文存储
account.api_keys = {
    "openai": "sk-...",
    "anthropic": "claude-key-..."
}

# ✓ 加密存储
from cryptography.fernet import Fernet

cipher = Fernet(os.environ["DSWARM_ENCRYPTION_KEY"])

def store_api_key(account_id: str, provider: str, key: str):
    encrypted = cipher.encrypt(key.encode())
    account_api_key = AccountApiKey(
        account_id=account_id,
        provider=provider,
        encrypted_key=encrypted,  # 存加密后的
        created_at=now()
    )
    db.add(account_api_key)
    db.commit()

def retrieve_api_key(account_id: str, provider: str) -> str:
    entry = db.query(AccountApiKey).filter_by(
        account_id=account_id, 
        provider=provider
    ).one()
    return cipher.decrypt(entry.encrypted_key).decode()
```

**避免方法**：
- 使用 Fernet（Python 内置，简单）
- 加密密钥存在环境变量 `DSWARM_ENCRYPTION_KEY`
- 定期轮换密钥（可选，复杂）
- 不在日志中打印明文 key

---

## 验收测试清单

### 功能测试

**认证与授权**
- [ ] Admin 可以创建新账户
- [ ] 新账户用密码登录成功
- [ ] 错误密码登录失败
- [ ] Token 过期后需要刷新
- [ ] Admin 可以禁用账户，该账户无法登录

**Event 与 Target**
- [ ] Admin 创建 Event
- [ ] 所有队员可以查看 Event
- [ ] 队员创建 Target
- [ ] 队员可以删除自己的 Target，不能删除他人的
- [ ] Admin 可以删除任何 Target
- [ ] 删除 Target 时级联删除其 Run

**Run 与隔离**
- [ ] 队员创建 Run，其他队员看不到
- [ ] Run 状态更新同步到 Target
- [ ] 解出 Flag 时，全队可见
- [ ] Fact 上传，全队可见
- [ ] 队员可以删除自己的 Run
- [ ] Admin 可以查看任何 Run（但看不到 API Key）

**Token 追踪**
- [ ] Run 完成后 Token 统计准确
- [ ] 队员可以查看自己的 Token 消耗
- [ ] Admin 看不到队员的 Token 消耗
- [ ] Admin 可以查看全队 Token 消耗统计

**本地 API**
- [ ] 本地 Agent 用 API token 获取 Target 信息
- [ ] 本地 Agent 上传 Flag，通过 Provenance gate
- [ ] 本地 Agent 上传 Fact，直接记录
- [ ] 无效 API token 被拒绝

**审计日志**
- [ ] Admin 可以查看所有操作日志
- [ ] 日志包含操作者、操作类型、时间戳
- [ ] 队员看不到审计日志

### 安全测试

**认证**
- [ ] 密码用 bcrypt 哈希（不是 MD5）
- [ ] JWT token 签名验证（不能伪造）
- [ ] Token 过期时间合理（Web session: 12h, API: 365d）

**授权**
- [ ] 队员 A 不能查看队员 B 的 Run
- [ ] 队员不能删除他人的 Target
- [ ] 队员不能访问 Admin 接口

**隐私**
- [ ] API Key 加密存储，Admin 看不到
- [ ] Token 消耗数据隔离，Admin 看不到个人消耗
- [ ] Run 日志存储在创建者隐私工作目录
- [ ] API 返回中不包含明文密钥

**输入验证**
- [ ] SQL 注入防护（ORM 参数化查询）
- [ ] XSS 防护（JSON 响应，不 HTML）
- [ ] CSRF 保护（如适用）
- [ ] Flag 提交经 Provenance gate

### 性能测试

**并发**
- [ ] 3 个队员同时创建 Run，不冲突
- [ ] 2 个队员同时编辑 Target，排队处理（不丢改动）
- [ ] 100 条审计日志查询 <100ms

**资源限制**
- [ ] 队员最多 3 个并发 Run（第 4 个被拒）
- [ ] Run 日志文件大小限制（或流式传输）

### 集成测试

**与 D-Swarm 核心**
- [ ] Flag 上传经过 Provenance gate（不伪造）
- [ ] Run 使用现有 Kali 容器池
- [ ] Token 消耗正确统计（与 UsageLedger 一致）
- [ ] CliSolver 输出正确解析

**与本地 Agent**
- [ ] 本地 Agent 获取 Target 信息准确
- [ ] 本地 Agent 提交成果被正确记录
- [ ] 提交元数据（agent 名称、执行时间）保存

### 数据一致性

- [ ] Target.solved_by 与 flags 列表一致
- [ ] Run.is_deleted = true 时不显示在列表
- [ ] 审计日志记录完整（每个关键操作）
- [ ] 快照记录 Target 编辑历史（Admin 可回溯）

---

## 部署检查清单

### 环境变量

```bash
# 认证
DSWARM_WEB_PASSWORD=          # 初始 Admin 密码（安全起见首次登录强制修改）
DSWARM_JWT_SECRET=            # JWT 签名密钥（随机 32+ 字符）
DSWARM_ENCRYPTION_KEY=        # API Key 加密密钥（Fernet）
DSWARM_JWT_ALGORITHM=HS256    # JWT 算法

# 数据库
DSWARM_DATABASE_URL=sqlite:///./dswarm.db  # SQLite 路径
# 或 PostgreSQL：
# DSWARM_DATABASE_URL=postgresql://user:password@localhost/dswarm

# Token 生命周期
DSWARM_WEB_SESSION_TTL=43200       # Web session 12 小时
DSWARM_API_TOKEN_TTL=31536000      # API token 365 天
```

### 首次部署

```bash
# 1. 初始化数据库
python -m alembic upgrade head  # 运行迁移

# 2. 创建 Admin 账户
python -c "
from dswarm.models.account import Account
from apps.web.db import SessionLocal

db = SessionLocal()
admin = Account(
    username='admin',
    display_name='Administrator',
    password_hash=hash_password(os.getenv('DSWARM_WEB_PASSWORD')),
    role='admin'
)
db.add(admin)
db.commit()
print(f'Admin account created: {admin.id}')
"

# 3. 启动服务
uvicorn apps.web.main:app --host 0.0.0.0 --port 8000
```

### 安全检查

- [ ] HTTPS 启用（nginx 或 Caddy）
- [ ] JWT 密钥足够长（32+ 字符）
- [ ] 加密密钥安全存储（环境变量或密钥管理系统）
- [ ] 数据库备份策略（定期备份）
- [ ] 日志文件权限正确（600）
- [ ] Admin 初始密码已修改

---

## 总结

这份实现指南明确了：

1. **技术栈**：FastAPI + SQLite + JWT，与现有 D-Swarm 一致
2. **集成策略**：保留核心，扩展应用层
3. **社区复用**：参考 GZCTF、CTFd 的成熟设计
4. **常见坑**：认证/授权、并发、隐私、安全
5. **验收标准**：功能、安全、性能、一致性

按照这份指南实现，可以确保：
- ✓ 与现有 D-Swarm 的兼容性
- ✓ 代码质量与安全性
- ✓ 团队协作的正确性
- ✓ 审计和合规性

