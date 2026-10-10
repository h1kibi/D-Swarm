# D-Swarm CTF 团队平台 — 快速参考与代码结构

**版本**: 1.0  
**日期**: 2026-10-10

---

## 快速参考卡

### 新增目录结构

```
apps/web/
├── accounts/
│   ├── __init__.py
│   ├── models.py           # Account, AccountApiKey 数据模型
│   ├── schemas.py          # Pydantic 验证模式（请求/响应）
│   ├── routes.py           # /api/accounts/* 端点
│   ├── service.py          # 账户逻辑（创建、禁用、查询）
│   └── dependencies.py     # 依赖注入（get_current_account）
│
├── events/
│   ├── __init__.py
│   ├── models.py           # Event 数据模型
│   ├── schemas.py
│   ├── routes.py           # /api/events/* 端点
│   └── service.py
│
├── audit/
│   ├── __init__.py
│   ├── models.py           # AuditLog 数据模型
│   ├── routes.py           # /api/audit/* 端点（Admin only）
│   └── service.py          # 日志记录辅助函数
│
├── external_api/
│   ├── __init__.py
│   ├── routes.py           # /api/external/* 端点
│   └── schemas.py          # 外部 API 请求/响应模式
│
├── db.py                   # SQLAlchemy 数据库连接
├── main.py                 # FastAPI 应用（改造现有）
└── middleware.py           # 认证中间件（改造现有）

dswarm/models/
├── __init__.py
├── account.py              # 账户域模型（新）
└── event.py                # Event 域模型（新）
```

### 数据库初始化

```bash
# 使用 Alembic 管理迁移

cd apps/web

# 创建新迁移（自动检测模型变化）
alembic revision --autogenerate -m "add accounts and events"

# 审查生成的迁移文件（alembic/versions/xxx_add_accounts_and_events.py）

# 应用迁移
alembic upgrade head
```

### 关键模块导入

```python
# 认证相关
from apps.web.accounts.dependencies import get_current_account
from apps.web.accounts.service import AccountService

# Event/Target 相关
from apps.web.events.service import EventService
from apps.web.targets.service import TargetService (扩展现有)

# 审计
from apps.web.audit.service import log_audit

# 外部 API
from apps.web.external_api.routes import router as external_router

# 数据库
from apps.web.db import SessionLocal, get_db
```

---

## 实现顺序建议

### 第 1 周：基础认证

**目标**：多账户登录可用。

```python
# 1. apps/web/accounts/models.py
@dataclass
class Account:
    id: str
    username: str
    password_hash: str  # bcrypt
    role: str  # "admin" | "member"
    is_active: bool
    created_at: float
    last_login: float | None

# 2. apps/web/accounts/schemas.py
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

# 3. apps/web/accounts/routes.py
@router.post("/auth/login")
async def login(req: LoginRequest) -> LoginResponse:
    # 验证用户名/密码
    # 返回 JWT token

# 4. apps/web/accounts/dependencies.py
async def get_current_account(
    authorization: str = Header(...)
) -> Account:
    # 从 Authorization header 解析 JWT
    # 返回 Account

# 5. tests/test_accounts.py
def test_login_success():
    ...
def test_login_invalid_password():
    ...
def test_token_expiry():
    ...
```

**验收**：
- [ ] Admin 可登录
- [ ] Token 有效期 12h
- [ ] 无效密码拒绝
- [ ] Token 刷新工作

---

### 第 2 周：Admin 账户管理

**目标**：Admin 可以创建/禁用队员账户。

```python
# 1. apps/web/accounts/routes.py 扩展
@router.get("/api/accounts", dependencies=[Depends(require_admin)])
async def list_accounts(db: Session) -> list[AccountResponse]:
    ...

@router.post("/api/accounts", dependencies=[Depends(require_admin)])
async def create_account(req: CreateAccountRequest, db: Session) -> AccountResponse:
    # 生成临时密码
    # 返回给 Admin 告知队员
    ...

@router.patch("/api/accounts/{account_id}", dependencies=[Depends(require_admin)])
async def update_account(account_id: str, req: UpdateAccountRequest, db: Session):
    # 禁用/启用账户
    ...

# 2. tests/test_accounts_admin.py
def test_admin_create_account():
    ...
def test_admin_disable_account():
    ...
def test_member_cannot_create_account():
    ...
```

**验收**：
- [ ] Admin 列表所有账户
- [ ] Admin 创建新账户
- [ ] Admin 禁用账户
- [ ] 队员无法访问管理端点

---

### 第 3 周：Event & Target 重组织

**目标**：将 Target 与 Event 关联，支持权限检查。

```python
# 1. 数据库迁移
# 新增 ctf_events 表
# 修改 ctf_targets 表：添加 event_id, created_by, is_solved, flags/facts 字段

# 2. apps/web/events/models.py
class Event(Base):
    __tablename__ = "ctf_events"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    status: Mapped[str]  # draft, active, archived
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[float]

# 3. apps/web/events/routes.py
@router.get("/api/events")
async def list_events() -> list[EventResponse]:
    ...

@router.post("/api/events", dependencies=[Depends(require_admin)])
async def create_event(req: CreateEventRequest, db: Session) -> EventResponse:
    ...

# 4. apps/web/targets/routes.py 改造
@router.post("/api/events/{event_id}/targets")
async def create_target(
    event_id: str,
    req: CreateTargetRequest,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> TargetResponse:
    target = Target(
        event_id=event_id,
        created_by=current_account.id,
        name=req.name,
        ...
    )
    db.add(target)
    db.commit()
    return TargetResponse.from_orm(target)

@router.delete("/api/targets/{target_id}")
async def delete_target(
    target_id: str,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db)
):
    target = get_target_or_404(target_id, db)
    
    # 权限检查
    if not (current_account.role == "admin" or current_account.id == target.created_by):
        raise PermissionDenied()
    
    # 级联删除 Run
    db.query(Run).filter(Run.target_id == target_id).delete()
    db.delete(target)
    db.commit()

# 5. tests/test_targets_permissions.py
def test_member_create_target():
    ...
def test_member_delete_own_target():
    ...
def test_member_cannot_delete_others_target():
    ...
def test_admin_delete_any_target():
    ...
def test_delete_target_cascade_delete_runs():
    ...
```

**验收**：
- [ ] Event 创建、列表、删除
- [ ] Target 与 Event 关联
- [ ] Target 权限检查（创建、编辑、删除）
- [ ] 删除 Event/Target 级联删除 Run

---

### 第 4 周：Run 隐私与 Token 追踪

**目标**：Run 支持创建者隐私，Token 消耗追踪。

```python
# 1. 数据库迁移
# 修改 ctf_runs 表：添加 created_by, token_usage, is_deleted 字段
# 新增 token_usage_logs 表

# 2. apps/web/run_manager.py 改造
class Run(Base):
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    token_usage: Mapped[str]  # JSON: {"openai": 50000, "anthropic": 30000}
    is_deleted: Mapped[int] = mapped_column(default=0)
    
    def can_view_by(self, account: Account) -> bool:
        if account.role == "admin":
            return True
        return account.id == self.created_by
    
    def can_delete_by(self, account: Account) -> bool:
        if account.role == "admin":
            return True
        return account.id == self.created_by

# 3. apps/web/runs/routes.py 改造
@router.get("/api/runs/{run_id}")
async def get_run(
    run_id: str,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> RunResponse:
    run = get_run_or_404(run_id, db)
    
    # 权限检查
    if not run.can_view_by(current_account):
        raise PermissionDenied()
    
    return RunResponse.from_orm(run)

@router.get("/api/accounts/me/runs")
async def get_my_runs(
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 10
) -> list[RunResponse]:
    runs = db.query(Run).filter(
        Run.created_by == current_account.id,
        Run.is_deleted == False
    ).offset(skip).limit(limit).all()
    return [RunResponse.from_orm(r) for r in runs]

@router.delete("/api/runs/{run_id}")
async def delete_run(
    run_id: str,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db)
):
    run = get_run_or_404(run_id, db)
    if not run.can_delete_by(current_account):
        raise PermissionDenied()
    
    run.is_deleted = True
    # 清理工作目录
    cleanup_workspace(run.workspace_id)
    
    db.commit()

# 4. Token 追踪（在 CliSolver 中集成）
# dswarm/solver/cli_solver.py 改造
class CliSolver:
    async def run(self, ...):
        # ... 现有逻辑 ...
        
        # Run 完成时记录 Token
        if hasattr(self, 'run_id'):  # 来自 web 层
            await self._record_token_usage()
    
    async def _record_token_usage(self):
        # 获取 Run 对象
        run = db.query(Run).filter(Run.id == self.run_id).one()
        
        # 统计 Token（从 UsageLedger）
        usage = self._collect_token_usage()  # {provider: count}
        
        # 更新 Run.token_usage
        run.token_usage = json.dumps(usage)
        
        # 记录详细日志
        for provider, count in usage.items():
            log = TokenUsageLog(
                account_id=run.created_by,
                provider=provider,
                tokens_used=count,
                run_id=self.run_id,
                timestamp=now()
            )
            db.add(log)
        
        db.commit()

# 5. tests/test_runs_privacy.py
def test_member_view_own_run():
    ...
def test_member_cannot_view_others_run():
    ...
def test_admin_view_any_run():
    ...
def test_token_usage_recorded():
    ...
def test_member_view_token_usage():
    ...
def test_member_cannot_view_others_token_usage():
    ...
```

**验收**：
- [ ] Run 创建者隐私（他人无法查看）
- [ ] 并发 Run 限制（最多 3 个）
- [ ] Token 追踪准确
- [ ] Admin 可查看全队统计，但看不到个人详情

---

### 第 5 周：本地 API & 外部集成

**目标**：本地 Agent 可以获取 Target 信息和提交成果。

```python
# 1. API Key 管理
# apps/web/accounts/models.py 扩展
class Account:
    api_keys: list[AccountApiKey]  # 关系

class AccountApiKey(Base):
    __tablename__ = "account_api_keys"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    provider: Mapped[str]  # "local_agent"
    encrypted_key: Mapped[str]  # Fernet 加密
    created_at: Mapped[float]
    revoked_at: Mapped[float | None] = None

# 2. apps/web/accounts/routes.py 扩展
@router.post("/api/accounts/me/api-keys")
async def generate_api_key(
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> ApiKeyResponse:
    # 生成长期 API token
    api_token = generate_api_token(current_account.id, expires_in=365*24*3600)
    
    # 保存（加密）
    api_key = AccountApiKey(
        account_id=current_account.id,
        provider="local_agent",
        encrypted_key=encrypt(api_token),
        created_at=now()
    )
    db.add(api_key)
    db.commit()
    
    # 仅返回一次（之后客户端自己保管）
    return {
        "api_token": api_token,
        "message": "save this token securely, it won't be shown again"
    }

# 3. apps/web/external_api/routes.py（新）
@router.get("/api/external/targets/{target_id}")
async def get_target_info(
    target_id: str,
    authorization: str = Header(...),
    db: Session = Depends(get_db)
) -> dict:
    # 验证 API token
    account = verify_api_token(authorization)
    
    # 获取 Target 信息
    target = db.query(Target).filter(Target.id == target_id).one()
    
    # 返回题目信息 + 当前 Flag/Fact（供参考）
    return {
        "id": target.id,
        "name": target.name,
        "description": target.description,
        "target_address": target.target_address,
        "attachments": target.attachments,
        "flag_format_hint": target.flag_format_hint,
        "current_flags": target.flags,  # 已解 Flag
        "current_facts": target.facts,  # 已知 Fact
        "metadata": {
            "is_solved": target.is_solved,
            "solved_by": target.solved_by,
            "total_runs": len(target.run_ids)
        }
    }

@router.post("/api/external/targets/{target_id}/submit")
async def submit_result(
    target_id: str,
    req: SubmitResultRequest,
    authorization: str = Header(...),
    db: Session = Depends(get_db)
) -> dict:
    # 验证 API token
    account = verify_api_token(authorization)
    
    target = db.query(Target).filter(Target.id == target_id).one()
    
    if req.type == "flag":
        # Flag 需要通过 Provenance gate
        from dswarm.solver.gate import flag_ok
        if not flag_ok(req.content):
            return {
                "accepted": False,
                "message": "flag format invalid"
            }
        
        target.flags.append(req.content)
        target.is_solved = True
        target.solved_by = account.id
        target.solved_at = now()
        
        # 审计
        log_audit("flag_submitted", account.id, "target", target.id, {
            "source": "external_api",
            "agent": req.metadata.get("agent_name")
        }, db)
    
    elif req.type == "fact":
        # Fact 直接记录（暂不验证）
        target.facts.append({
            "content": req.content,
            "submitted_by": account.id,
            "timestamp": now(),
            "metadata": req.metadata
        })
        
        log_audit("fact_submitted", account.id, "target", target.id, {
            "source": "external_api"
        }, db)
    
    db.commit()
    
    return {
        "accepted": True,
        "target_status": "solved" if target.is_solved else "in_progress"
    }

# 4. tests/test_external_api.py
def test_external_api_requires_token():
    ...
def test_external_get_target_info():
    ...
def test_external_submit_flag_valid():
    ...
def test_external_submit_flag_invalid():
    ...
def test_external_submit_fact():
    ...
```

**验收**：
- [ ] 本地 Agent 生成 API token
- [ ] 本地 Agent 获取 Target 信息
- [ ] 本地 Agent 提交 Flag（经 Provenance gate）
- [ ] 本地 Agent 提交 Fact
- [ ] 无效 token 被拒绝

---

### 第 6 周：审计与 Admin 面板

**目标**：Admin 可以查看所有操作日志。

```python
# 1. apps/web/audit/models.py
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(primary_key=True)
    action: Mapped[str]
    actor: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    resource_type: Mapped[str]
    resource_id: Mapped[str]
    details: Mapped[str]  # JSON
    timestamp: Mapped[float]
    status: Mapped[str]  # success / failure

# 2. apps/web/audit/service.py
def log_audit(
    action: str,
    actor: str,
    resource_type: str,
    resource_id: str,
    details: dict,
    db: Session,
    status: str = "success"
):
    log = AuditLog(
        id=generate_id(),
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        details=json.dumps(details),
        timestamp=now(),
        status=status
    )
    db.add(log)
    db.commit()

# 3. apps/web/audit/routes.py
@router.get("/api/audit/logs", dependencies=[Depends(require_admin)])
async def list_audit_logs(
    action: str | None = None,
    actor: str | None = None,
    resource_type: str | None = None,
    date_from: float | None = None,
    date_to: float | None = None,
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100
) -> list[AuditLogResponse]:
    query = db.query(AuditLog)
    
    if action:
        query = query.filter(AuditLog.action == action)
    if actor:
        query = query.filter(AuditLog.actor == actor)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if date_from:
        query = query.filter(AuditLog.timestamp >= date_from)
    if date_to:
        query = query.filter(AuditLog.timestamp <= date_to)
    
    logs = query.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()
    return [AuditLogResponse.from_orm(l) for l in logs]

# 4. tests/test_audit.py
def test_audit_log_account_creation():
    ...
def test_audit_log_target_edit():
    ...
def test_audit_log_flag_submission():
    ...
def test_member_cannot_view_audit():
    ...
```

**验收**：
- [ ] 所有关键操作被记录
- [ ] Admin 可以过滤/查询审计日志
- [ ] 队员无法访问审计日志

---

## 项目管理建议

### 依赖顺序

```
Week 1: 基础认证
    ↓
Week 2: Admin 账户管理
    ↓
Week 3: Event/Target 重组织
    ↓
Week 4: Run 隐私 + Token 追踪
    ↓
Week 5: 本地 API
    ↓
Week 6: 审计 + Admin 面板
```

### 测试覆盖

- **单元测试**：模型、服务、权限逻辑
- **集成测试**：API 端点、数据库、事务
- **权限测试**：Admin/Member/外部 API 访问控制
- **E2E 测试**：Web UI 工作流（可选，初期低优先度）

### 代码审查点

- [ ] 所有 API 都有权限检查
- [ ] 所有敏感操作都有审计日志
- [ ] SQL 查询参数化（使用 ORM）
- [ ] 密钥/Token 不在日志中
- [ ] 错误消息不泄露系统细节

### 性能检查

- [ ] 数据库有适当的索引
- [ ] 列表 API 支持分页
- [ ] 并发操作无死锁

---

## 常见问题

**Q: 能直接从现有 D-Swarm 迁移数据吗？**  
A: 可以。写迁移脚本将现有 Target 和 Run 映射到新的 Account 和 Event 表。

**Q: API Key 的加密如何处理？**  
A: 使用 Fernet（Python 内置）。环境变量存 DSWARM_ENCRYPTION_KEY，启动时读取。

**Q: Admin 首次登录用什么密码？**  
A: 环境变量 DSWARM_WEB_PASSWORD。首次登录后强制修改。

**Q: Token 如何刷新？**  
A: Web session token（12h）+ refresh token（7d）。客户端在 token 即将过期时调用 `/api/auth/refresh`。

**Q: 如何处理队员离职？**  
A: Admin 禁用账户。该账户的 Target/Run 保留（用于历史查看），但无法新建操作。

