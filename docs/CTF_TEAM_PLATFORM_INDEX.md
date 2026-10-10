# D-Swarm CTF 团队协作平台 — 完整设计文档索引

**项目代号**: D-Swarm Team Edition  
**创建日期**: 2026-10-10  
**状态**: 设计完成，待实现

---

## 📋 文档导航

本项目的完整设计分为 **4 份核心文档**，循序渐进地介绍需求、架构、实现细节和快速启动指南。

### 1. **[PLATFORM_DESIGN_CTF_TEAM.md](PLATFORM_DESIGN_CTF_TEAM.md)** — 平台设计方案
**适合**: 产品经理、架构师、项目经理

**内容**：
- 需求概述与约束条件
- 核心数据模型（Account, Event, Target, Run, Snapshot, AuditLog）
- 系统架构与组件分离
- 权限模型与安全边界
- 完整 API 设计（认证、管理、Event/Target/Run、外部 API、审计）
- 数据库 Schema（含 SQL 定义）
- 分阶段实现计划（6 周，14 个单元）

**关键产物**：
- 数据模型设计（Pydantic schemas）
- 权限矩阵
- 完整 API 文档（请求/响应示例）
- 阶段里程碑定义

---

### 2. **[IMPLEMENTATION_GUIDE_CTF_TEAM.md](IMPLEMENTATION_GUIDE_CTF_TEAM.md)** — 实现指南与技术决策
**适合**: 后端开发、技术主管

**内容**：
- 技术栈确认（FastAPI, SQLite, JWT, bcrypt, Fernet）
- 与现有 D-Swarm 的集成点（不能动 vs 可以扩展 vs 新增）
- 社区代码复用清单（GZCTF, CTFd, 现有 D-Swarm）
- 实现中的常见坑（6 个关键陷阱+解决方案）
  - 认证与授权混淆
  - API Key 与 Session Token 混用
  - Target 编辑并发冲突（FIFO 队列方案）
  - Flag 验证的两重检查
  - Token 消耗追踪的延迟
  - API Key 加密存储
- 验收测试清单（功能、安全、性能、集成、一致性）
- 部署检查清单（环境变量、首次部署、安全检查）

**关键产物**：
- 技术选型决策
- 集成策略细节
- 代码示例与反面教材
- 完整测试矩阵

---

### 3. **[QUICK_REFERENCE_CTF_TEAM.md](QUICK_REFERENCE_CTF_TEAM.md)** — 快速参考与代码结构
**适合**: 开发人员、新手入门

**内容**：
- 新增目录结构（apps/web/accounts/, events/, audit/, external_api/）
- 数据库初始化流程（Alembic）
- 关键模块导入速查
- **按周详细的实现顺序**（第 1-6 周详细代码框架）
  - 第 1 周：基础认证（Account 模型、JWT token、登录 API）
  - 第 2 周：Admin 账户管理（创建、禁用、查询）
  - 第 3 周：Event & Target 重组织（权限、级联删除、快照）
  - 第 4 周：Run 隐私与 Token 追踪（隐私隔离、并发限制、Token 统计）
  - 第 5 周：本地 API & 外部集成（API token、外部端点）
  - 第 6 周：审计与 Admin 面板（AuditLog、查询接口）
- 项目管理建议（依赖顺序、测试覆盖、代码审查点）
- FAQ

**关键产物**：
- 开发周计划表
- 目录结构（可直接创建）
- 代码框架（快速复制粘贴）
- 验收标准（每周检查点）

---

## 🎯 核心需求速览

| 功能 | 说明 |
|------|------|
| **多账户管理** | Admin 创建账户，队员登录；无注册功能 |
| **Event 与 Target 组织** | Admin 创建 Event，所有人可创建 Target，权限隔离 |
| **Run 隐私** | Run 创建者私有，Flag/Fact 全队可见 |
| **Token 追踪** | 个人隐私，个人可见，Admin 看总数 |
| **本地 API** | 外部 Agent 获取题目信息和上传成果 |
| **审计日志** | Admin 可查看所有操作 |
| **编辑防冲突** | Target 编辑排队处理，完整快照 |

---

## 🛠️ 技术栈一览

| 组件 | 选择 | 原因 |
|------|------|------|
| Web 框架 | FastAPI | 与现有 D-Swarm 一致，异步支持 |
| 认证 | JWT + Refresh | 适合多端（Web + 本地 API）|
| 数据库 | SQLite + SQLAlchemy | 单机部署；后期可迁 Postgres |
| 密码 | bcrypt | 行业标准 |
| 加密 | Fernet | 简单、内置 |
| 前端 | Next.js（现有） | 保持一致性 |

---

## 📊 实现时间表

```
Week 1   ├─ 基础认证 (Account, JWT, 登录)
         └─ Blocker: 无 ✓
         
Week 2   ├─ Admin 账户管理 (创建、禁用)
         ├─ 依赖: Week 1 ✓
         └─ Blocker: 无 ✓
         
Week 3   ├─ Event/Target 重组织
         ├─ 依赖: Week 2 ✓
         └─ Blocker: 数据库迁移脚本需审查
         
Week 4   ├─ Run 隐私 + Token 追踪
         ├─ 依赖: Week 3 ✓
         └─ Blocker: CliSolver 集成需协调
         
Week 5   ├─ 本地 API & 外部集成
         ├─ 依赖: Week 4 ✓
         └─ Blocker: API token 加密方案需确认
         
Week 6   ├─ 审计 + Admin 面板
         ├─ 依赖: 全部 ✓
         └─ Blocker: 无 ✓
         
Week 7   └─ 缓冲 / 优化 / UI 打磨
```

**总工期**: 6 周实现 + 1 周缓冲 = 7 周（充足）

---

## 🔗 文档间的关系

```
PLATFORM_DESIGN_CTF_TEAM.md (全景)
    ↓
    ├─→ 需求明确 → IMPLEMENTATION_GUIDE_CTF_TEAM.md (技术决策)
    ├─→ 数据模型定义 → QUICK_REFERENCE_CTF_TEAM.md (代码框架)
    └─→ API 设计确定 → 后续代码实现

QUICK_REFERENCE_CTF_TEAM.md (操作指南)
    ├─→ 每周代码框架 → 开发人员逐行实现
    ├─→ 测试检查点 → 集成测试设计
    └─→ 验收标准 → QA 验收
```

---

## 📝 使用指南

### 我是 PM / 架构师
1. 阅读 **PLATFORM_DESIGN_CTF_TEAM.md** 全文（1-2 小时）
2. 关键决策点：
   - ✓ 权限模型（Admin/Member）
   - ✓ Event 作为隔离单位
   - ✓ Run 创建者隐私
   - ✓ Token 追踪个人隐私
   - ✓ 编辑排队处理

### 我是后端开发
1. 阅读 **PLATFORM_DESIGN_CTF_TEAM.md** — 数据模型部分
2. 阅读 **IMPLEMENTATION_GUIDE_CTF_TEAM.md** 全文（技术细节）
3. 按 **QUICK_REFERENCE_CTF_TEAM.md** 中的周计划编码

### 我是前端开发
1. 快速浏览 **PLATFORM_DESIGN_CTF_TEAM.md** — API 设计部分
2. 参考 **QUICK_REFERENCE_CTF_TEAM.md** — 关键模块导入
3. 从 API 文档开始（POST /api/auth/login, GET /api/events, 等）

### 我是 QA / 测试
1. 阅读 **IMPLEMENTATION_GUIDE_CTF_TEAM.md** — 验收测试清单
2. 按 **QUICK_REFERENCE_CTF_TEAM.md** 中的周计划，每周提取测试案例
3. 关键测试类别：
   - 权限隔离（跨 Account/Event/Run）
   - 安全（Token、密钥、认证）
   - 并发（Target 编辑、Run 创建）

### 我在评审这个设计
**检查清单**：
- [ ] 数据模型完整（Account, Event, Target, Run, Snapshot, AuditLog）
- [ ] API 端点覆盖所有 CRUD 操作
- [ ] 权限检查在每个敏感端点都有
- [ ] 隐私边界清晰（Run 私有、Token 追踪隐私）
- [ ] 并发问题已解决（编辑队列）
- [ ] 与现有 D-Swarm 的集成点明确
- [ ] 社区参考合适（GZCTF, CTFd）
- [ ] 测试覆盖充分

---

## 🚀 立即可用的产物

### 数据库 Schema
位置: PLATFORM_DESIGN_CTF_TEAM.md § 数据库 Schema

直接复制到 `alembic/versions/xxx.py` 创建迁移。

### API 文档
位置: PLATFORM_DESIGN_CTF_TEAM.md § API 设计

可直接粘贴为 OpenAPI/Swagger 定义。

### 代码框架
位置: QUICK_REFERENCE_CTF_TEAM.md § 实现顺序

每个 Week 的代码框架可直接 copy-paste 开始编码。

### 测试用例
位置: IMPLEMENTATION_GUIDE_CTF_TEAM.md § 验收测试清单

可作为 pytest/unittest 的测试方法名。

---

## ❓ 常见问题

**Q: 这些文档会不会过时？**  
A: 文档遵循"不变性"原则。核心设计（权限、数据模型、API）是稳定的。实现细节（HTTP 状态码、字段名）可在代码审查时调整。

**Q: 能否跳过某个阶段？**  
A: 不建议。Week 2-6 都有依赖关系。可以在某个 Week 内并行（如前端+后端），但不能跳过。

**Q: 如何处理不可预见的问题？**  
A: IMPLEMENTATION_GUIDE_CTF_TEAM.md 的"常见坑"部分已列出 6 个主要陷阱。遇到新问题时，遵循文档中的决策原则（优先权限隐私）。

**Q: 设计后能改吗？**  
A: 可以。但 Week 1-3 的改动成本高（数据库 schema）。Week 4-6 改动成本低（新 API 端点）。建议每周代码审查时冻结上周设计。

---

## 📚 引用资料

### 社区参考

- **GZCTF**: https://github.com/GZTimeWalker/GZCTF
  - 多队伍、多题目架构
  - 题目权限管理
  - 提交与验证流程

- **CTFd**: https://github.com/CTFd/CTFd
  - 插件系统（可选未来）
  - 动态评分（可选未来）
  - 队伍与用户分离

### 现有 D-Swarm 代码

- `apps/web/auth.py` — 认证基础
- `apps/web/targets/` — Target 模型（扩展基础）
- `apps/web/run_manager.py` — Run 管理（隐私字段）
- `dswarm/swarm/shared_graph.py` — 不变（保留）
- `dswarm/solver/gate.py` — 不变（保留）

---

## 📞 反馈与讨论

如有设计问题或改进建议，建议在以下优先顺序讨论：

1. **PLATFORM_DESIGN_CTF_TEAM.md** — 核心设计决策
2. **IMPLEMENTATION_GUIDE_CTF_TEAM.md** — 技术可行性
3. **QUICK_REFERENCE_CTF_TEAM.md** — 编码细节

---

**下一步**：选择上述某份文档开始阅读，或联系项目负责人讨论实现计划。

