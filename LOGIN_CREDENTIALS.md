# 🔑 D-Swarm Web 登录凭证

## 📋 默认账户

### 管理员账户 (Admin)

```
用户名: admin
密码:   admin123
角色:   admin
```

**权限**: 完全控制
- 创建和管理事件
- 创建和管理账户
- 查看审计日志
- 管理所有资源

### 成员账户 (Member)

```
用户名: member
密码:   member123
角色:   member
```

**权限**: 受限权限
- 创建和管理自己的目标
- 创建和运行任务
- 查看自己的资源和统计
- 不能创建事件或管理其他账户

---

## 🌐 如何登录

### 方法 1: Web UI (推荐)

1. 打开浏览器: http://127.0.0.1:3001
2. 输入用户名: `admin` (或 `member`)
3. 输入密码: `admin123` (或 `member123`)
4. 点击登录按钮

### 方法 2: API 测试 (使用 cURL)

**管理员登录**:
```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'
```

**成员登录**:
```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"member","password":"member123"}'
```

响应示例:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "account_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "admin",
  "display_name": "Administrator",
  "role": "admin"
}
```

### 方法 3: API 交互式文档

1. 打开: http://127.0.0.1:8000/docs
2. 找到 `POST /api/auth/login`
3. 点击 "Try it out"
4. 输入用户名和密码
5. 点击 "Execute"

---

## 🎯 功能对比

| 功能 | Admin | Member |
|------|-------|--------|
| 创建 Event | ✅ | ❌ |
| 创建 Target | ✅ | ✅ |
| 查看自己的 Target | ✅ | ✅ |
| 查看他人的 Target | ✅ | ❌ |
| 创建 Run | ✅ | ✅ |
| 查看自己的 Run | ✅ | ✅ |
| 查看他人的 Run | ✅ | ❌ |
| 查看审计日志 | ✅ | ❌ |
| 管理账户 | ✅ | ❌ |
| 修改其他用户密码 | ✅ | ❌ |

---

## 🔐 安全信息

- **认证方式**: JWT (HS256)
- **令牌有效期**: 12 小时（会话），365 天（API）
- **密码加密**: bcrypt (自动盐化)
- **API 密钥加密**: Fernet (对称加密)

---

## ✨ 快速开始流程

### 1. 用 Admin 账户登录

```
用户名: admin
密码:   admin123
```

### 2. 创建 Event (仅 Admin)

- 事件 → 新建事件
- 输入事件名称: "DEFCON 32"
- 输入描述: "Annual CTF competition"
- 点击创建

### 3. 创建 Target

- 目标 → 新建目标
- 输入目标名称: "Web Challenge"
- 选择类别: "web"
- 输入目标地址: "web-challenge.ctf.local"
- 设置预期 flags: 1
- 点击创建

### 4. 创建 Run

- 运行 → 新建运行
- 选择目标: 选择刚创建的目标
- 选择事件: 选择刚创建的事件
- 点击启动

### 5. 查看运行信息

- 运行详情 → 查看 token 使用情况
- 查看工作者信息
- 查看发现的 flags 和 facts

### 6. 查看审计日志 (仅 Admin)

- 审计 → 审计日志
- 可以看到所有操作记录

---

## 📚 相关文档

| 文档 | 说明 |
|------|------|
| **DSWARM_LOCAL_START.md** | 本地启动指南 |
| **PHASE_5_TESTING_GUIDE.md** | 完整测试指南 |
| **PLATFORM_DESIGN_CTF_TEAM.md** | 原始设计方案 |
| **QUALITY_CHECK_REPORT.md** | 质检报告 (100/100) |

---

## 🌐 API 端点

完整的 API 端点列表和交互式文档:
- **Swagger UI**: http://127.0.0.1:8000/docs
- **ReDoc**: http://127.0.0.1:8000/redoc

---

## ⚙️ 修改密码

### 通过 Web UI

1. 登录后，点击用户菜单
2. 点击 "修改密码"
3. 输入旧密码和新密码
4. 点击保存

### 通过 API

```bash
curl -X PUT http://127.0.0.1:8000/api/accounts/me/password \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "old_password": "admin123",
    "new_password": "new_secure_password_123"
  }'
```

---

## 🆘 故障排查

### Q: 密码不对？
**A**: 确保没有多余空格，密码区分大小写

### Q: 忘记密码？
**A**: 联系系统管理员（需要 admin 权限）或重新初始化数据库

### Q: 如何创建新账户？
**A**: 需要 admin 权限
```bash
curl -X POST http://127.0.0.1:8000/api/accounts \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "newuser",
    "display_name": "New User",
    "password": "secure_password_123",
    "role": "member"
  }'
```

---

**最后更新**: 2026-10-10  
**状态**: ✅ 可用  
**数据库**: SQLite (dswarm.db)
