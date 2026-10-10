# D-Swarm Web UI 改进计划

## 现状分析

### ✓ 已有的功能
- 登录门禁（LoginGate）
- 基础的命令行界面（CommandPalette）
- 运行观察界面（Conversation）
- 决策时间线（DecisionTimeline）

### ✗ 缺失的关键功能

根据《D-Swarm CTF Team Collaboration Platform 设计方案》，前端需要实现：

| 功能模块 | 优先级 | 状态 | 说明 |
|---------|--------|------|------|
| **Admin Dashboard** | P0 | ❌ 缺失 | 账户管理、事件管理、审计日志 |
| **Member Dashboard** | P0 | ❌ 缺失 | 事件列表、目标列表、概览 |
| **Event Management** | P1 | ❌ 缺失 | 创建/编辑/查看竞赛事件 |
| **Target Library** | P1 | ❌ 缺失 | 题目列表、题目详情、创建题目 |
| **Target Workspace** | P1 | ❌ 缺失 | 编辑题目、查看快照、解题状态 |
| **Run Launcher** | P1 | ❌ 缺失 | 启动新运行、选择目标和事件 |
| **Run List** | P1 | ❌ 缺失 | 查看所有运行、过滤、排序 |
| **Run Details** | P1 | ❌ 部分 | 查看运行结果、Token统计、Flag/Fact |
| **Audit Dashboard** | P2 | ❌ 缺失 | 操作日志、账户活动、统计 |
| **Token Tracker** | P2 | ❌ 缺失 | Token 使用统计（个人） |
| **User Settings** | P2 | ❌ 缺失 | 修改密码、API密钥管理 |

---

## 改进方案

### 第一阶段：核心数据展示界面 (P0)

#### 1. 首页/仪表板 (Home Dashboard)

**主要内容**:
- 当前登录用户信息
- 最近活动概览
- 快速操作按钮

**根据用户角色**:

**Admin 视图**:
```
┌─────────────────────────────────┐
│  D-Swarm Admin Dashboard        │
├─────────────────────────────────┤
│ 👥 账户管理                      │
│   • 创建账户                     │
│   • 禁用/启用账户                │
│   • 查看所有账户                 │
├─────────────────────────────────┤
│ 🎯 Event 管理                   │
│   • 创建新 Event                │
│   • 查看所有 Event              │
│   • Event 统计                  │
├─────────────────────────────────┤
│ 📊 审计日志                      │
│   • 操作日志查询                 │
│   • 用户活动追踪                 │
│   • 系统统计                     │
└─────────────────────────────────┘
```

**Member 视图**:
```
┌─────────────────────────────────┐
│  D-Swarm Dashboard              │
├─────────────────────────────────┤
│ 🎯 当前竞赛事件                  │
│   • Event 列表                  │
│   • Event 选择器                │
├─────────────────────────────────┤
│ 📋 题目                          │
│   • 未解题目列表                 │
│   • 已解题目列表                 │
├─────────────────────────────────┤
│ ⚙️  我的运行                      │
│   • 进行中的 Run                │
│   • 已完成的 Run                │
├─────────────────────────────────┤
│ 💡 团队成果                      │
│   • 已发现的 Flag               │
│   • 已发现的 Fact               │
└─────────────────────────────────┘
```

#### 2. Event 管理页面 (Events Page)

**URL**: `/events`

**功能**:
- 列表视图：所有 Event（带搜索、过滤）
- 创建 Event（Admin only）
- Event 详情模态框
- Event 编辑表单
- Event 删除确认

**组件结构**:
```
EventsPage
├── EventsList
│   ├── SearchBar
│   ├── FilterBar (status: all/draft/active/archived)
│   ├── EventCard (点击进入详情)
│   │   ├── EventName
│   │   ├── Description
│   │   ├── Status Badge
│   │   ├── TargetCount
│   │   └── ActionMenu (edit/delete/archive)
│   └── CreateEventButton (Admin only)
├── EventDetailsModal
│   ├── EventInfo
│   ├── TargetsList
│   ├── EditForm (Admin only)
│   └── DeleteButton (Admin only)
```

#### 3. Target 管理页面 (Targets Page)

**URL**: `/targets`

**功能**:
- 目标库：所有题目列表
- 按 Event 分组/筛选
- 创建新题目
- 题目详情模态框
- 编辑题目
- 删除题目（创建者或 Admin）
- 查看快照历史

**组件结构**:
```
TargetsPage
├── EventSelector (选择要查看的 Event)
├── TargetsList
│   ├── SearchBar
│   ├── FilterBar (category, status: solved/unsolved)
│   ├── TargetCard
│   │   ├── TargetName
│   │   ├── Category Badge
│   │   ├── SolvedStatus
│   │   ├── SolvedBy
│   │   ├── FlagCount
│   │   ├── FactCount
│   │   └── ActionMenu
│   └── CreateTargetButton
├── TargetDetailsModal
│   ├── TargetInfo
│   ├── Attachments
│   ├── Flags/Facts List
│   ├── SnapshotHistory
│   ├── EditForm
│   └── DeleteButton
```

#### 4. Run 管理页面 (Runs Page)

**URL**: `/runs`

**功能**:
- 列表：所有 Run（自己的 + Admin 可看他人）
- 启动新 Run
- Run 详情页面
- Token 统计
- Flag/Fact 管理

**组件结构**:
```
RunsPage
├── RunLauncher (启动新 Run)
│   ├── TargetSelector
│   ├── EventSelector
│   └── LaunchButton
├── RunsList
│   ├── FilterBar (status: running/completed/failed/stopped)
│   ├── SortOptions
│   ├── RunCard
│   │   ├── RunID
│   │   ├── TargetName
│   │   ├── Status Badge
│   │   ├── CreatedBy
│   │   ├── CreatedAt
│   │   ├── TokenUsage
│   │   ├── FlagCount
│   │   └── ActionMenu
├── RunDetailsPage
│   ├── RunInfo
│   ├── TokenStatistics
│   ├── FlagsPanel
│   ├── FactsPanel
│   ├── LogsPanel
│   └── DeleteButton
```

#### 5. 账户管理页面 (Accounts Page) - Admin Only

**URL**: `/admin/accounts`

**功能**:
- 账户列表
- 创建账户
- 编辑账户信息
- 启用/禁用账户
- 删除账户
- 查看账户活动

**组件结构**:
```
AccountsPage (Admin only)
├── CreateAccountForm
│   ├── UsernameInput
│   ├── DisplayNameInput
│   ├── PasswordInput
│   ├── RoleSelector (admin/member)
│   └── CreateButton
├── AccountsList
│   ├── SearchBar
│   ├── AccountCard
│   │   ├── Username
│   │   ├── DisplayName
│   │   ├── Role Badge
│   │   ├── Status (active/inactive)
│   │   ├── CreatedAt
│   │   ├── LastLogin
│   │   └── ActionMenu (edit/disable/delete)
├── AccountDetailsModal
│   ├── AccountInfo
│   ├── EditForm
│   ├── ActivityLog (user 的操作)
│   ├── DisableButton
│   └── DeleteButton
```

#### 6. 审计日志页面 (Audit Page) - Admin Only

**URL**: `/admin/audit`

**功能**:
- 操作日志查询
- 用户活动追踪
- 资源变更历史
- 系统统计

**组件结构**:
```
AuditPage (Admin only)
├── AuditFilters
│   ├── DateRangeFilter
│   ├── ActionFilter (create/update/delete/start/stop)
│   ├── ActorFilter (用户)
│   ├── ResourceTypeFilter (target/run/event/account)
│   └── StatusFilter (success/failure)
├── AuditLogsList
│   ├── AuditLogRow
│   │   ├── Timestamp
│   │   ├── Action Badge
│   │   ├── Actor
│   │   ├── ResourceType & ID
│   │   ├── Details
│   │   ├── Status (success/failure)
│   │   └── IPAddress
├── StatisticsPanel
│   ├── OperationCount (by action)
│   ├── UserActivity (by actor)
│   ├── FailureRate
│   └── TimeSeriesChart
```

#### 7. 用户设置页面 (Settings Page)

**URL**: `/settings`

**功能**:
- 修改密码
- API 密钥管理（生成/删除）
- 用户信息编辑
- 登出

**组件结构**:
```
SettingsPage
├── UserInfoSection
│   ├── DisplayNameInput
│   ├── SaveButton
├── PasswordSection
│   ├── OldPasswordInput
│   ├── NewPasswordInput
│   ├── ConfirmPasswordInput
│   └── ChangeButton
├── ApiKeysSection (如果需要)
│   ├── KeysList
│   ├── GenerateKeyButton
│   └── DeleteKeyButton
├── LogoutButton
```

---

### 第二阶段：交互与实时更新 (P1)

1. **实时同步**
   - WebSocket 连接管理
   - 多标签页同步
   - 自动刷新

2. **权限验证**
   - 每个页面/操作的权限检查
   - 无权限时显示 403
   - 降级处理

3. **错误处理**
   - API 错误提示
   - 重试机制
   - 离线检测

4. **用户反馈**
   - Toast 通知
   - Loading 状态
   - 确认对话框

---

### 第三阶段：高级功能 (P2)

1. **搜索与过滤**
   - 全局搜索
   - 高级过滤
   - 保存搜索条件

2. **统计与分析**
   - Token 消耗趋势
   - 解题速度统计
   - 团队贡献排名

3. **导出与报告**
   - 审计日志导出
   - 竞赛报告生成
   - 数据备份

---

## 实现优先级

### MVP (最小可行产品)

**必需** (第 1-2 周):
1. Home Dashboard (区分 Admin/Member)
2. Event Management
3. Target Library & Workspace
4. Run Launcher & List
5. Basic User Settings

**需要** (第 3-4 周):
6. Account Management (Admin)
7. Audit Dashboard (Admin)
8. Token Tracker
9. Error Handling & Notifications

**可选** (第 5+ 周):
10. Advanced Analytics
11. Bulk Operations
12. Export & Reporting

---

## 技术实现建议

### 前端架构

```
apps/web/ui/
├── app/
│   ├── page.tsx (Home Dashboard)
│   ├── events/
│   │   ├── page.tsx (Events List)
│   │   └── [id]/page.tsx (Event Details)
│   ├── targets/
│   │   ├── page.tsx (Targets Library)
│   │   └── [id]/page.tsx (Target Details)
│   ├── runs/
│   │   ├── page.tsx (Runs List)
│   │   └── [id]/page.tsx (Run Details)
│   ├── admin/
│   │   ├── accounts/page.tsx
│   │   ├── audit/page.tsx
│   │   └── events/page.tsx
│   ├── settings/page.tsx
│   └── layout.tsx (主导航)
├── components/
│   ├── Dashboard/
│   │   ├── AdminDashboard.tsx
│   │   ├── MemberDashboard.tsx
│   ├── Events/
│   │   ├── EventsList.tsx
│   │   ├── EventCard.tsx
│   │   ├── EventForm.tsx
│   ├── Targets/
│   │   ├── TargetsList.tsx
│   │   ├── TargetCard.tsx
│   │   ├── TargetForm.tsx
│   ├── Runs/
│   │   ├── RunsList.tsx
│   │   ├── RunCard.tsx
│   │   ├── RunLauncher.tsx
│   ├── Audit/
│   │   ├── AuditLog.tsx
│   │   ├── AuditFilters.tsx
│   ├── Admin/
│   │   ├── AccountsList.tsx
│   │   ├── AccountForm.tsx
│   ├── Navigation/
│   │   ├── MainNav.tsx
│   │   ├── Sidebar.tsx
├── lib/
│   ├── useEvents.ts
│   ├── useTargets.ts
│   ├── useRuns.ts
│   ├── useAccounts.ts (Admin)
│   ├── useAudit.ts (Admin)
│   ├── permissions.ts
│   ├── api-client.ts
│   └── types.ts
```

### 关键库

- **UI Framework**: React 18 + TypeScript
- **Routing**: Next.js App Router (已有)
- **State**: React Context / Zustand
- **Data Fetching**: React Query / SWR
- **Forms**: React Hook Form
- **Modals**: Headless UI / Radix
- **Notifications**: Toast (现有)
- **Tables**: TanStack React Table
- **Charts**: Recharts (统计)

---

## 迁移策略

### 从现有 CLI 模式到面板模式

1. **保留现有功能**
   - 保留命令行界面作为 "Advanced Mode"
   - 现有的 Conversation 和 Timeline 用于 Run Details

2. **新增页面导航**
   - 添加主导航菜单
   - 实现路由转换
   - 保持登录状态

3. **数据层改造**
   - 创建统一的 API 客户端
   - 实现权限检查 Hook
   - 管理缓存和同步

---

## 后端支持检查表

- ✅ `/api/auth/login` - 认证
- ✅ `/api/accounts/*` - 账户管理 (需要权限修复)
- ✅ `/api/events/*` - Event CRUD
- ✅ `/api/targets/*` - Target CRUD
- ✅ `/api/runs/*` - Run 管理 (需要权限修复)
- ✅ `/api/audit/*` - 审计日志

**需要修复**:
- 认证一致性 (当前使用 Mock)
- Run/Target 端点的权限验证
- 错误消息标准化
- 分页、排序、过滤实现

---

## 时间估算

| 阶段 | 任务 | 工作量 | 优先级 |
|------|------|--------|--------|
| P0 | Home Dashboard | 2 天 | P0 |
| P0 | Event Management | 3 天 | P0 |
| P0 | Target Management | 4 天 | P0 |
| P0 | Run Management | 3 天 | P0 |
| P1 | Admin Pages (Accounts, Audit) | 4 天 | P1 |
| P1 | User Settings | 2 天 | P1 |
| P1 | Error Handling & UX | 3 天 | P1 |
| P2 | Advanced Features | 5+ 天 | P2 |

**总计**: ~3-4 周可交付完整平台

