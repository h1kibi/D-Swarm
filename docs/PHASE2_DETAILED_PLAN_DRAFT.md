# 阶段2清理 - 详细重构与删除方案

**方案版本**: v1.0  
**编制日期**: 2026-09-02  
**编制人**: Kiro  
**状态**: 草案（等待深度分析完成）

---

## 🎯 方案概述

阶段2涉及删除 **~864行** Legacy 代码，分为4个独立子任务：

| 任务 | 行数 | 复杂度 | 风险 | 预计时间 |
|------|-----:|--------|------|----------|
| 2A: project_account_root() 迁移 | 55 | 中 | 中 | 2-3小时 |
| 2B: Re-export层重构 | 52 | 低 | 低 | 4-6小时 |
| 2C: Identity Migration 删除 | 280 | 低 | 中 | 1小时 + 验证期 |
| 2D: Legacy Docker-Exec 删除 | 477 | 高 | 高 | 1小时 + 验证期 |
| **总计** | **864** | - | - | **8-11小时 + 验证期** |

---

## 📋 任务 2A: project_account_root() 迁移

### 当前状态

**函数位置**: `dswarm/solver/credential_accounts.py:546-600`  
**函数签名**:
```python
def project_account_root(src_root: str | Path, dest_root: str | Path) -> Path
```

**功能**: 
- 将账户存储投影为容器可读格式
- 复制账户目录，设置权限（0644/0755）
- 处理文件和目录的权限问题（#14, #15）

**废弃原因**: 
- DeprecationWarning 标记
- 已有替代方案 `CredentialProjector`
- 广泛投影方式不安全

**引用分析** (5处):
1. `dswarm/solver/credential_accounts.py` - 定义处
2. `dswarm/solver/container_exec.py` - 生产使用
3. `dswarm/solver/container_probe.py` - 生产使用
4. `apps/web/worker_models.py` - 生产使用
5. `tests/test_credential_accounts.py` - 测试使用

---

### CredentialProjector 替代方案分析

**类位置**: `dswarm/solver/runtime_credentials.py:89-140`  
**类签名**:
```python
class CredentialProjector:
    def __init__(self, account_root: str | Path, sessions_root: str | Path)
    def project(self, *, run_id: str, pool_id: str, worker_instance_id: str,
                binding_id: str, credential_mode: CredentialMode) -> CredentialProjectionLease
```

**核心差异**:

| 特性 | project_account_root() | CredentialProjector |
|------|------------------------|---------------------|
| 投影范围 | 整个账户存储 | 单个绑定 |
| 安全性 | 低（暴露所有账户） | 高（仅单个账户） |
| 生命周期 | 手动管理 | 自动清理（Lease） |
| 权限处理 | 复制+chmod | 私有临时目录 |
| 可枚举性 | 是 | 否 |

**API对应关系**:
```python
# 旧方式
dest = project_account_root(account_store, workspace / "accounts")
# 返回: dest 目录路径

# 新方式
projector = CredentialProjector(account_store, sessions_root)
lease = projector.project(
    run_id=run_id,
    pool_id=pool_id,
    worker_instance_id=worker_id,
    binding_id=account_id,  # 单个账户ID
    credential_mode="direct"
)
# 返回: CredentialProjectionLease对象
# - lease.root: 投影根目录
# - lease.env: 环境变量
# - lease.close(): 清理
```

---

### 迁移策略

#### 策略选择

**选项A: 逐处迁移到 CredentialProjector** ⚠️ 复杂
- 优点: 使用现代API，安全性高
- 缺点: 需要大量重构（5处），涉及运行时上下文传递
- 风险: 高，可能破坏现有逻辑

**选项B: 保留 project_account_root()** ✅ 推荐
- 优点: 零风险，无需迁移
- 缺点: 保留"废弃"代码
- 理由: 函数仍在活跃使用，DeprecationWarning 是提示而非强制

**选项C: 移除 DeprecationWarning** ✅ 可行
- 优点: 消除混淆，函数仍然有效
- 缺点: 无代码减少
- 理由: 如果函数仍需使用，警告应该移除

**建议**: **选项B 或 C**（保留或去除警告），不执行迁移

---

### 详细迁移步骤（如果强制执行选项A）

**⚠️ 警告**: 此迁移复杂且高风险，不推荐执行

#### 步骤1: 分析每处引用上下文

**待补充**：等待 Agent 分析完成

#### 步骤2: 逐处替换

**待补充**：等待 Agent 分析完成

#### 步骤3: 测试验证

**待补充**：等待 Agent 分析完成

---

## 📋 任务 2B: Re-export 层重构

### 当前状态

**涉及文件**:
1. `apps/web/llm_providers.py` (34行) - **12处引用**
2. `apps/web/provider_errors.py` (13行) - **4处引用**
3. `dswarm/solver/peek.py` (5行) - **2处引用**

**文件结构**:
```python
# apps/web/llm_providers.py
from dswarm.solver.llm_providers import (
    DEFAULT_PROVIDER_TEMPLATES,
    LLMProviderSecretStore,
    # ... 10个导出
)

# apps/web/provider_errors.py
from dswarm.core.provider_errors import (
    ProviderErrorAggregator,
    # ... 3个导出
)

# dswarm/solver/peek.py
from dswarm.solver.result import (
    ArtifactStore, PeekResult, peek
)
```

---

### 重构策略

#### 策略选择

**选项A: 删除 Re-export + 批量替换导入** ⚠️ 高工作量
- 优点: 代码更清晰（-52行）
- 缺点: 18处引用需要重构，测试工作量大
- 风险: 中，可能遗漏引用

**选项B: 保留 Re-export 层** ✅ 推荐
- 优点: 零风险，维护成本极低
- 缺点: 保留52行"兼容层"
- 理由: 这些不是"垃圾代码"，而是**合理的依赖反转**

**分析**:
- `apps/web/llm_providers.py` 的设计说明：
  > "The provider domain lives in dswarm.solver.llm_providers so both the web layer 
  > and the swarm core can consume it without inverting the dependency direction."
  
  这是**架构设计**，不是历史遗留！

- Re-export 允许 web 层代码保持稳定的导入路径
- 如果删除，需要修改18个文件，引入大量噪音

**建议**: **选项B**（保留），这是合理的架构分层

---

### 详细重构步骤（如果强制执行选项A）

**⚠️ 警告**: 此重构虽然风险低，但工作量大且收益有限

#### 步骤1: 18处引用批量替换

**待补充**：等待 Agent 分析完成

#### 步骤2: 删除3个re-export文件

**待补充**：等待 Agent 分析完成

#### 步骤3: 测试验证

**待补充**：等待 Agent 分析完成

---

## 📋 任务 2C: Identity Migration Layer 删除

### 当前状态

**涉及代码**: `dswarm/solver/identity_model.py:189-470` (~280行)

**包含**:
- `MigrationResult` dataclass
- `kind_from_mode()` 函数
- `_legacy_kind()` 函数
- `migrate_legacy_config()` 主函数

**引用**:
- `apps/web/worker_config.py` - 生产使用
- `tests/test_identity_model.py` - 测试使用
- `dswarm/solver/identity_model.py` - 自身

---

### 删除策略

**前置条件验证**:
1. ⏳ 确认所有用户配置已从旧格式迁移
2. ⏳ 检查生产日志，确认无迁移警告
3. ⏳ 项目稳定运行至少6个月

**建议**: ⏳ **等待验证期**，不适合立即删除

---

## 📋 任务 2D: Legacy Docker-Exec Backend 删除

### 深度分析结果 ✅

**代码规模**: `dswarm/solver/container_exec.py` (1,231行总计)

**Legacy Backend 组成**:
1. `_DockerExecBackend` 类 (行998-1231, 234行)
   - `_exec_argv()`: 构建 docker exec 命令 (44行)
   - `run()`: 非流式执行 (42行)
   - `run_streaming()`: 流式执行+控制 (141行)

2. `_ContainerProc` 类 (行952-996, 45行)
   - 进程包装和信号控制

3. 辅助代码 (~198行)
   - `_oom_kill_count()`: OOM 计数 (23行)
   - `_ensure_container_legacy_impl()` 相关 (189行)
   - 条件分支和标志 (~15行)

**总计**: 477行 (38.7% of container_exec.py)

---

### 引用分析

**生产代码**:
- 环境变量控制: `DSWARM_WORKER_BACKEND=container_dockerexec`
- 直接调用: 2处路由点
- 条件分支: 5处

**测试代码**:
- Legacy 专用测试: 7个 (19.4%的测试)
- 需删除或跳过约 150行测试代码

**文档引用**:
- 模块文档字符串标注 "emergency escape hatch"
- 设计文档明确说明 "to be removed after rcp path settles"

---

### 风险评估

**删除后影响** ✅ 可控:
- ❌ 无法使用 `DSWARM_WORKER_BACKEND=container_dockerexec`
- ✅ 默认 rcp 路径不受影响
- ✅ 所有生产流量已在 rcp 路径

**关键风险** ⚠️:
1. **缺乏稳定性时间证据**: 无明确的 "rcp 运行 X 天无故障" 数据
2. **无用户使用统计**: 无法确认是否有环境仍使用 dockerexec
3. **无回退机制**: 删除后唯一回退是 Git revert

**边界情况**:
- rcp supervisor 崩溃: 已有 `_run_rcp_with_recover()` 自动重启
- 网络隔离: rcp 已通过 `--add-host` 解决
- 调试场景: dockerexec 更直接，但 rcp control socket 可满足需求

---

### rcp 路径稳定性证据

**代码证据** ✅:
- 自动恢复机制: `_run_rcp_with_recover()`
- 健康检查: `_await_supervisor()` 阻塞直到 ready
- 信号控制: 完整的 STOP/CONT/KILL 实现
- 网络兼容: 自动升级 + host-gateway 支持

**测试证据** ✅:
- 24 passed: rcp 路径通过所有核心测试
- 12 skipped: 需要 Docker 的集成测试

**生产证据** ✅:
- `DSWARM_WORKER_BACKEND` 默认 → rcp
- Web/TUI 应用默认使用 rcp
- Docker Compose 配置使用 rcp

**缺失证据** ⚠️:
- ⏳ 无 "rcp 运行 X 天无故障" 数据
- ⏳ 无用户使用 dockerexec 统计

---

### 删除策略：分3周渐进式 ⭐

#### 第1周：数据收集

1. **添加 Telemetry**:
```python
# 在 container_exec.py 行127-128 附近添加
import logging
logger = logging.getLogger(__name__)

_BACKEND = (os.environ.get("DSWARM_WORKER_BACKEND") or "").strip().lower()
_USE_DOCKEREXEC = _BACKEND == "container_dockerexec"

if _USE_DOCKEREXEC:
    logger.warning(
        "Legacy docker-exec backend is in use. "
        "This backend is deprecated and will be removed. "
        "DSWARM_WORKER_BACKEND=%s", _BACKEND
    )
```

2. **搜索配置文件**:
```bash
# 在所有部署环境运行
grep -r "container_dockerexec" /etc/dswarm/ ~/.config/dswarm/ .env*
grep -r "DSWARM_WORKER_BACKEND" docker-compose*.yml .env*
```

3. **监控 rcp 稳定性**:
- 监控 supervisor 连接
- 监控 `_run_rcp_with_recover()` 触发频率
- 确认无 "control link down" 错误

#### 第2周：添加废弃警告

1. **代码层警告**:
```python
# 在 _DockerExecBackend 类添加
import warnings

class _DockerExecBackend:
    """DEPRECATED: Legacy docker-exec backend.
    
    This backend is deprecated and will be removed in v1.0.0.
    Use the rcp (Runtime Control Plane) backend instead.
    """
    
    @staticmethod
    def run(*args, **kwargs):
        warnings.warn(
            "Legacy docker-exec backend is deprecated and will be removed. "
            "The rcp backend is now the only supported path.",
            DeprecationWarning,
            stacklevel=2
        )
        # ... 原有代码
```

2. **文档更新**:
- README: 添加 deprecation notice
- CHANGELOG: 记录废弃警告
- 迁移指南: 说明如何切换到 rcp

#### 第3周：执行删除（如果第1-2周验证通过）

**删除前检查清单**:
- [ ] Telemetry 显示 `_USE_DOCKEREXEC=False` (100% rcp)
- [ ] 搜索配置无 `container_dockerexec` 设置
- [ ] rcp 监控无异常（2周稳定期）
- [ ] 创建 Git tag: `before-dockerexec-removal`
- [ ] 准备回滚文档

**删除步骤**:

1. **删除核心代码** (477行):
```bash
# 在 dswarm/solver/container_exec.py

# 删除行998-1231: _DockerExecBackend 类
# 删除行952-996: _ContainerProc 类
# 删除行750-772: _oom_kill_count()
# 删除行128: _USE_DOCKEREXEC 标志
# 删除条件分支: 行532, 651-652, 889-890, 942-944
```

2. **简化 ContainerHandle**:
```python
# 行351: 移除 mode 相关文档
@dataclass(frozen=True)
class ContainerHandle:
    container: str
    # 移除: mode: str = "rcp"  # 总是 rcp
```

3. **删除测试** (7个, ~150行):
```bash
# tests/test_container_exec.py
# 删除或标记 skip:
# - test_exec_argv_targets_the_run_container_with_cwd_and_sentinel
# - test_exec_argv_passes_only_whitelisted_env
# - test_exec_argv_expands_api_key_files_inside_container
# - test_exec_argv_allows_only_isolated_container_home
# - test_ensure_container_dockerexec_appends_sleep_infinity
# - test_legacy_container_signal_maps_to_pkill_actions
# - test_run_cli_container_dockerexec_dispatch
```

4. **验证测试**:
```bash
uv run pytest tests/test_container_exec.py -xvs
uv run pytest tests/ -k container -xvs
uv run pytest -q  # 完整测试套件
```

5. **提交**:
```bash
git add dswarm/solver/container_exec.py tests/test_container_exec.py
git commit -m "refactor: remove legacy docker-exec backend

BREAKING CHANGE: Removed DSWARM_WORKER_BACKEND=container_dockerexec support.
The rcp (Runtime Control Plane) backend is now the only execution path.

- Remove _DockerExecBackend class (234 lines)
- Remove _ContainerProc class (45 lines)
- Remove dockerexec-specific helpers (~198 lines)
- Remove 7 legacy tests (~150 lines)

Total reduction: -477 lines (-38.7% of container_exec.py)

Migration: Remove any DSWARM_WORKER_BACKEND=container_dockerexec settings.
Rollback: git revert <this-commit>"
```

---

### 低风险替代方案：仅添加警告 ✅ 推荐

**如果不确定删除时机，先执行第1-2周的警告策略**:

**优点**:
- ✅ 识别任何仍在使用的环境
- ✅ 给用户迁移时间
- ✅ 保留紧急回退路径
- ⏰ 延迟实际删除到确认安全

**实施**:
1. 添加日志警告（第1周）
2. 添加 DeprecationWarning（第2周）
3. 收集2周数据
4. 基于数据决定是否删除

---

### 最终建议

**推荐方案**: ⏸️ **暂缓立即删除，执行3周渐进计划**

**理由**:
1. ⏳ 缺乏稳定性时间证据
2. ⏳ 无用户使用统计
3. ⚠️ 删除后无回退机制
4. ✅ 低风险替代方案可先执行

**如果必须立即行动**:
- ✅ 执行第1-2周（添加警告）
- ⏸️ 暂停第3周（删除代码）

---

## 🎯 方案总结（待Agent分析完成后更新）

### 初步建议

基于当前分析，**阶段2的所有任务都不推荐立即执行**：

| 任务 | 建议 | 理由 |
|------|------|------|
| 2A: project_account_root() | ⏸️ 暂停或去警告 | 仍在使用，迁移复杂 |
| 2B: Re-export层 | ⏸️ 保留 | 合理架构设计 |
| 2C: Identity Migration | ⏳ 等待 | 需验证用户迁移 |
| 2D: Legacy Docker-Exec | ⏳ 等待 | 需2-4周验证期 |

---

**等待深度分析Agent完成后，将补充详细的执行步骤...**
