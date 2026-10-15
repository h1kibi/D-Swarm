# 前端登录问题排查指南

> **[2026-10-16 更新] 本文件已过时。** 文末描述的"mock 端点"方案是错误做法：它伪造空数据、
> 遮蔽了真实的 `/api/runs`、`/api/targets` 等接口，并且绕过了真正的账户系统。该方案及其
> 临时路由（`auth_direct.py`、`mock.py`）已被删除；登录现在走真实账户系统（`/api/auth/login`
> → `/api/accounts` 的 DbAccountService，密码可选用户名）。当前状态与排障请以
> `session-handoff.md` 为准。下文仅作历史记录保留。

## 问题现象

输入密码 `TestPass123!` 后，登录界面闪烁一下，无法进入后台。

## 后端检测结果

✓ **完全正常**

- ✓ `/api/auth/login` 返回 200 OK
- ✓ Token 成功生成
- ✓ JSON 响应格式正确
- ✓ 支持 password-only 模式
- ✓ 通过前端代理也工作正常

## 可能原因

1. 前端登录后加载应用时出现错误
2. localStorage token 保存失败
3. 应用加载时从 API 获取数据失败
4. JavaScript 运行时错误

## 故障排查步骤

### 1️⃣ 打开浏览器开发者工具

按 `F12` 或右键 → `检查元素`

### 2️⃣ 查看 Console 标签页

- 是否有红色错误信息？
- 是否有 401/403/404/500 等错误？
- 记下所有错误信息

### 3️⃣ 查看 Network 标签页

登录时观察网络请求：
- 查看 `/api/auth/login` 请求
- 检查状态码是否为 200
- 检查响应内容中是否包含 `token` 字段
- 是否有其他请求返回错误？

### 4️⃣ 查看 Application 标签页 → Storage → LocalStorage

- 是否存在 `dswarm_auth_token` 键？
- Token 值是否非空且不是 `undefined`？

### 5️⃣ 观察页面切换过程

**登录前**：显示密码输入框
**输入密码后**：应显示加载指示器
**如果立即返回登录框**：说明 `login()` 返回了 `ok=false`

## 浏览器控制台测试

在浏览器开发者工具的 Console 标签中执行以下命令：

### 测试 1: 直接测试登录

```javascript
fetch('/api/auth/login', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({password: 'TestPass123!'})
}).then(r => r.json()).then(d => {
  console.log('Login response:', d);
  console.log('Has token:', !!d.token);
  console.log('OK:', d.ok);
})
```

### 测试 2: 检查 Token 是否保存

```javascript
console.log('Saved token:', localStorage.getItem('dswarm_auth_token'))
```

### 测试 3: 检查认证状态

```javascript
fetch('/api/auth/me').then(r => {
  console.log('Auth me status:', r.status);
  return r.json();
}).then(d => console.log('Auth me response:', d))
```

### 测试 4: 获取运行列表（应用需要的数据）

```javascript
fetch('/api/runs').then(r => {
  console.log('Runs status:', r.status);
  return r.json().catch(() => null);
}).then(d => console.log('Runs response:', d))
```

## 收集诊断信息

请提供以下信息帮助诊断：

1. **Console 中的所有错误信息**
   - 红色错误的完整文本
   - 错误堆栈跟踪

2. **Network 标签中的请求**
   - `/api/auth/login` 的完整请求和响应
   - 登录后其他失败的请求

3. **执行 Console 测试的输出**
   - 上述测试 1-4 的输出结果

4. **浏览器信息**
   - 浏览器类型和版本
   - 操作系统

## 常见问题

### Q: 看到 "Cannot reach server" 或网络错误
**A**: 
- 确保后端在 http://127.0.0.1:8000 运行
- 检查防火墙是否阻止了连接
- 尝试直接访问 http://127.0.0.1:8000/docs

### Q: 看到 401 错误
**A**:
- Token 可能没有正确保存到 localStorage
- 检查浏览器是否允许 localStorage

### Q: 看到 404 错误
**A**:
- 对应的 API 端点可能不存在或拼写错误
- 提供完整的请求 URL

### Q: 看到 JavaScript 语法错误
**A**:
- 这可能是代码问题
- 提供错误的完整堆栈跟踪

## 下一步

根据诊断结果，您可以：

1. 提供收集到的诊断信息
2. 我会根据具体错误进行修复
3. 重新启动服务进行测试

---

**联系方式**: 提供诊断信息时包含 Console 输出和 Network 标签的截图。
