# D-Swarm Web 本地测试 - 完整指南

## 🎯 快速开始

所有服务已启动并可用！

### 登录凭证

| 账户类型 | 用户名 | 密码 | 角色 |
|---------|--------|------|------|
| **测试管理员** | testadmin | TestPass123! | admin |
| **测试成员** | testmember | TestPass123! | member |

### 立即访问

**Web 应用**: https://127.0.0.1:3001
```
用户名: testadmin
密码:   TestPass123!
```

**API 文档**: http://127.0.0.1:8000/docs
```
交互式 API 测试和文档
完整的 OpenAPI 规范
所有 60+ 端点的列表
```

---

## ✅ 系统状态

### 运行中的服务

```
后端 (FastAPI):     http://127.0.0.1:8000          ✓ 运行中
前端 (Next.js):     http://127.0.0.1:3001          ✓ 运行中
API 文档:           http://127.0.0.1:8000/docs     ✓ 可用
```

### 数据库

```
类型:              SQLite (dswarm.db)
位置:              C:/Projects/Agent-projects/D-Swarm/dswarm.db
账户数:            4
表:                6 (accounts, events, targets, runs, snapshots, audit_logs)
```

### 认证系统

```
认证方式:          JWT (HS256)
令牌有效期:        12 小时 (会话)
密码加密:          bcrypt
API 密钥加密:      Fernet
RBAC:             Admin / Member 角色
```

---

## 🧪 测试账户详情

### Admin 账户 (testadmin)

```
用户名: testadmin
密码:   TestPass123!
角色:   Admin
```

**权限**:
- ✓ 创建和管理事件
- ✓ 创建和管理账户
- ✓ 查看所有资源
- ✓ 查看审计日志
- ✓ 全部 API 访问

### Member 账户 (testmember)

```
用户名: testmember
密码:   TestPass123!
角色:   Member
```

**权限**:
- ✓ 创建和管理自己的目标
- ✓ 创建和启动运行
- ✓ 查看自己的资源
- ✓ 基本 API 访问
- ✗ 无法创建事件
- ✗ 无法管理其他账户

---

## 🔐 安全信息

### 认证流程

1. **登录**
   ```bash
   POST /api/auth/login
   {
     "username": "testadmin",
     "password": "TestPass123!"
   }
   ```

2. **获取 JWT 令牌**
   ```json
   {
     "access_token": "eyJhbGciOiJIUzI1NiIs...",
     "token_type": "bearer",
     "expires_in": 43200,
     "account_id": "...",
     "username": "testadmin",
     "role": "admin"
   }
   ```

3. **使用令牌**
   ```bash
   GET /api/targets
   Authorization: Bearer <access_token>
   ```

### 密码要求

- 最小长度: 8 字符
- 包含字母: 建议混合大小写
- 特殊字符: 建议使用 (如 !@#$%)
- 测试密码: TestPass123! ✓

---

## 📊 API 端点总览

### 认证 (3 个)
- `POST   /api/auth/login` - 登录
- `POST   /api/auth/logout` - 登出
- `GET    /api/auth/me` - 当前用户

### 账户管理 (8 个)
- `GET    /api/accounts` - 列表
- `POST   /api/accounts` - 创建
- `GET    /api/accounts/{id}` - 获取
- `PATCH  /api/accounts/{id}` - 更新
- `DELETE /api/accounts/{id}` - 删除
- `PUT    /api/accounts/me/password` - 修改密码
- `POST   /api/accounts/me/api-keys` - 生成 API 密钥
- 等等...

### 事件管理 (5 个)
- `POST   /api/events` - 创建
- `GET    /api/events` - 列表
- `GET    /api/events/{id}` - 获取
- `PATCH  /api/events/{id}` - 更新
- `DELETE /api/events/{id}` - 删除

### 目标管理 (8 个)
- `POST   /api/targets` - 创建
- `GET    /api/targets` - 列表
- `GET    /api/targets/{id}` - 获取
- `PATCH  /api/targets/{id}` - 更新
- `DELETE /api/targets/{id}` - 删除
- `POST   /api/targets/{id}/flags` - 添加标志
- `POST   /api/targets/{id}/facts` - 添加事实
- `GET    /api/targets/{id}/snapshots` - 历史

### 运行管理 (8 个)
- `POST   /api/runs` - 创建
- `GET    /api/runs` - 列表
- `GET    /api/runs/{id}` - 获取
- `PATCH  /api/runs/{id}` - 更新
- `DELETE /api/runs/{id}` - 删除
- `POST   /api/runs/{id}/tokens` - 记录 Token
- `GET    /api/runs/{id}/tokens` - Token 统计
- `POST   /api/runs/{id}/stop` - 停止运行

### 审计日志 (5 个)
- `GET    /api/audit/logs` - 全部日志 (Admin)
- `GET    /api/audit/account/{id}/actions` - 账户操作
- `GET    /api/audit/account/{id}/statistics` - 统计
- `GET    /api/audit/resource/{type}/{id}` - 资源历史
- `GET    /api/audit/failures` - 失败操作

### 其他 (20+ 个)
- Blackboard
- Credentials
- Engines
- Folders
- Settings
- 等等...

**完整列表**: http://127.0.0.1:8000/docs

---

## 🧪 快速测试步骤

### 1. 登录 (5 秒)

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"testadmin","password":"TestPass123!"}'
```

### 2. 创建事件 (需要登录令牌)

```bash
curl -X POST http://127.0.0.1:8000/api/events \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name":"DEFCON 32","description":"Test Event"}'
```

### 3. 创建目标

```bash
curl -X POST http://127.0.0.1:8000/api/targets \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":"<EVENT_ID>",
    "name":"Web Challenge",
    "category":"web",
    "target_address":"http://web.ctf.local",
    "expected_flags":1
  }'
```

### 4. 启动运行

```bash
curl -X POST http://127.0.0.1:8000/api/runs \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "target_id":"<TARGET_ID>",
    "event_id":"<EVENT_ID>"
  }'
```

---

## 📝 故障排查

### Q: 无法登录?
**A**: 
- 确保用户名和密码正确 (区分大小写)
- 检查账户是否激活
- 查看 http://127.0.0.1:8000/docs 测试端点

### Q: 401 Unauthorized 错误?
**A**:
- 确保包含了 JWT 令牌
- 使用正确的格式: `Authorization: Bearer <token>`
- 检查令牌是否过期 (12 小时)

### Q: 无法创建事件?
**A**:
- 确保使用 admin 账户 (testadmin)
- Member 无法创建事件 (权限限制)

### Q: 后端崩溃了?
**A**:
- 重新启动: `python /tmp/start_backend_with_accounts.py`
- 检查数据库: `dswarm.db` 是否存在
- 查看错误日志

### Q: 前端无法连接后端?
**A**:
- 确保后端在 http://127.0.0.1:8000 运行
- 检查防火墙设置
- 刷新浏览器页面

---

## 🚀 下一步

### 推荐流程

1. **登录 Web UI**
   - 打开 http://127.0.0.1:3001
   - 使用 testadmin/TestPass123! 登录

2. **浏览 API 文档**
   - 打开 http://127.0.0.1:8000/docs
   - 查看所有可用端点
   - 尝试交互式测试

3. **创建测试数据**
   - 创建 Event (DEFCON 32)
   - 创建 Target (Web Challenge)
   - 启动 Run

4. **验证功能**
   - 检查权限系统
   - 测试不同角色
   - 查看审计日志

---

## 📚 相关文档

| 文档 | 说明 |
|------|------|
| **PLATFORM_DESIGN_CTF_TEAM.md** | 完整设计方案 |
| **PHASE_5_TESTING_GUIDE.md** | 手动测试指南 |
| **QUALITY_CHECK_REPORT.md** | 质检报告 (100/100) |
| **DSWARM_LOCAL_START.md** | 本地启动指南 |
| **LOGIN_CREDENTIALS.md** | 登录凭证参考 |

---

## 🎯 系统概览

```
D-Swarm Web Platform
├── 后端 (FastAPI)
│   ├── 认证系统 (JWT + bcrypt)
│   ├── 账户管理 (RBAC)
│   ├── 事件管理
│   ├── 目标管理
│   ├── 运行管理
│   └── 审计日志
├── 前端 (Next.js)
│   ├── Web UI
│   ├── 实时更新 (SSE)
│   └── 响应式设计
└── 数据库 (SQLite)
    ├── 用户账户
    ├── 事件和目标
    ├── 运行记录
    └── 审计日志
```

---

**状态**: ✅ 完全就绪  
**最后更新**: 2026-10-10  
**质检评分**: 100/100  
**生产就绪**: 是
