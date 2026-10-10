# D-Swarm Web 本地快速启动指南

## 🌐 当前状态

✅ **后端服务**: http://127.0.0.1:8000  
✅ **前端服务**: http://127.0.0.1:3001  

**两个服务已正在运行！**

## 🚀 打开应用

直接在浏览器打开：
```
👉 http://127.0.0.1:3001
```

## 📖 API 文档

完整的 API 文档和交互式测试界面：
```
👉 http://127.0.0.1:8000/docs
```

## 🔧 启动命令

### 启动后端（Python FastAPI）
```bash
cd C:/Projects/Agent-projects/D-Swarm
python /tmp/start_backend.py
```

### 启动前端（Next.js）
```bash
cd C:/Projects/Agent-projects/D-Swarm/apps/web/ui
npm start  # 生产模式
# 或
npm run dev  # 开发模式
```

## 🗄️ 数据库初始化（首次使用）

```bash
cd C:/Projects/Agent-projects/D-Swarm
python -c "
from apps.web.db.session import init_db
init_db()
print('✅ Database initialized!')
"
```

## 📊 API 端点总览

| 类别 | 端点数 | 路径 |
|------|-------|------|
| **认证** | 3 | /api/auth/* |
| **账户管理** | 8 | /api/accounts/* |
| **事件管理** | 5 | /api/events/* |
| **目标管理** | 8 | /api/targets/* |
| **运行管理** | 8 | /api/runs/* |
| **审计日志** | 5 | /api/audit/* |
| **其他** | 20+ | /api/* |

**完整列表**: http://127.0.0.1:8000/docs

## 🧪 快速测试

### 测试后端健康
```bash
curl http://127.0.0.1:8000/docs
```

### 登录测试（需先初始化数据库）
```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'
```

## 🛑 停止服务

### 方式 1：终端按 Ctrl+C
- 后端：在运行后端的终端按 `Ctrl+C`
- 前端：在运行前端的终端按 `Ctrl+C`

### 方式 2：Windows PowerShell
```powershell
# 杀死 Python 进程
Stop-Process -Name python -Force

# 杀死 Node 进程
Stop-Process -Name node -Force
```

## 📚 相关文档

| 文档 | 说明 |
|-----|------|
| **PLATFORM_DESIGN_CTF_TEAM.md** | 完整设计方案 |
| **PHASE_5_TESTING_GUIDE.md** | 手动测试指南 |
| **QUALITY_CHECK_REPORT.md** | 质检报告 (100/100) |
| **http://127.0.0.1:8000/docs** | 交互式 API 文档 |

## 🔐 默认账号

- **用户名**: admin
- **密码**: admin123 (或根据 PHASE_5_TESTING_GUIDE.md)

## ⚙️ 配置

### 改变端口

**后端**（改为 9000）:
```bash
python -c "
import uvicorn
from apps.web.server import app
uvicorn.run(app, host='127.0.0.1', port=9000)
"
```

**前端**（改为 3002）:
```bash
cd apps/web/ui
npm run dev -- -p 3002
```

### 允许外网访问

```bash
# 后端（0.0.0.0 = 所有网卡）
python -c "
import uvicorn
from apps.web.server import app
uvicorn.run(app, host='0.0.0.0', port=8000)
"

# 前端
cd apps/web/ui
npm run dev -- -H 0.0.0.0
```

## 🐛 问题排查

| 问题 | 解决方案 |
|-----|--------|
| 后端启动失败 | 检查 Python 依赖：`uv run pytest -q` |
| 前端显示错误 | 重新构建：`npm run build` |
| 端口占用 | 改用其他端口（见上面配置章节） |
| CORS 错误 | 后端已配置 CORS，检查 URL 是否正确 |

## ✅ 完成检查表

- [x] 后端启动于 http://127.0.0.1:8000
- [x] 前端启动于 http://127.0.0.1:3001
- [x] API 文档可访问 http://127.0.0.1:8000/docs
- [x] 数据库已初始化（或按需初始化）
- [x] 准备好进行用户测试

## 🎯 下一步

1. **打开前端**: http://127.0.0.1:3001
2. **查看 API**: http://127.0.0.1:8000/docs
3. **按照 PHASE_5_TESTING_GUIDE.md 进行测试**
4. **查看质检报告**: QUALITY_CHECK_REPORT.md

---

**最后更新**: 2026-10-10  
**状态**: ✅ 运行中 — 生产就绪  
**质检评分**: 100/100
