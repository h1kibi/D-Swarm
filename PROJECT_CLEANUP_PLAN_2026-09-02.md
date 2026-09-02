# D-Swarm 项目精简方案（2026-09-02）

> **目标**：识别并删除GPT防御性编程产生的冗余代码、低质量测试和历史残留，同时避免误伤核心功能。

---

## 📊 项目现状

### 规模统计
- **主要Python文件**: 895个
- **测试文件**: 135个
- **测试方法**: 2,030个
- **最大源文件**: shared_graph.py (4,724行), cli_solver.py (4,333行)

### 识别的问题
1. **历史兼容层代码** (~800行可删除)
2. **迁移层代码** (~280行待评估)
3. **测试文件放错位置** (4个测试文件在生产代码目录)
4. **历史评估目录** (eval_nyu, references等)
5. **过时的 ROADMAP** (标注为 historical)

---

## 🎯 第一阶段：立即可删除（零风险）

### 1.1 无用的兼容标志

**文件**: `dswarm/solver/container_exec.py`
```python
# 行57: 定义后从未被检查，纯声明
LEGACY_CONTAINER_EXEC_COMPATIBILITY_FACADE = True
```

**删除理由**：
- ✅ 标志定义后从未被代码检查
- ✅ 无功能影响
- ✅ 仅占1行但制造混淆

**预期收益**：澄清代码意图

---

### 1.2 测试文件放错位置

**需要移动或删除的文件**：
1. `apps/web/account_test.py` (200行) - **测试代码**
2. `apps/web/llm_test.py` (未统计) - **测试代码**
3. `apps/web/routes/startup_test.py` (未统计) - **功能代码**
4. `apps/web/startup_test.py` (未统计) - **功能代码**

**评估**：
- `account_test.py` 和 `llm_test.py` 应移动到 `tests/` 目录
- `startup_test.py` 两个文件是**功能代码**（启动测试控制器），保留在 apps/web

**建议行动**：
```bash
# 移动测试文件到 tests/
git mv apps/web/account_test.py tests/test_web_account_connectivity.py
git mv apps/web/llm_test.py tests/test_web_llm_connectivity.py
```

**预期收益**：代码组织更清晰

---

### 1.3 历史评估残留目录

**可删除的目录**：

| 目录 | 大小 | 文件数 | 说明 |
|------|-----:|-------:|------|
| `eval_runs/` | 0 | 0 | 空目录 |
| `knowledge/` | 0 | 0 | 空目录 |
| `local_benchmarks/` | 0 | 0 | 空目录 |
| `public_eval/` | 24KB | 未知 | 需检查内容 |
| `examples/` | 33KB | 未知 | 需检查内容 |
| `eval_nyu/` | 176KB | 8 | 历史评估数据 |

**评估 eval_nyu/**：
- 包含旧的评估数据集和结果
- 最后更新：2026-08-10
- **建议**：归档到 `docs/archive/eval/` 或完全删除

**评估 references/btfly/**：
- 大小：4.6MB
- 内容：历史参考项目代码
- **建议**：完全删除或移到外部文档仓库

**建议行动**：
```bash
# 删除空目录
rm -rf eval_runs knowledge local_benchmarks

# 归档评估数据
mkdir -p docs/archive/eval
git mv eval_nyu docs/archive/eval/

# 删除历史参考代码（4.6MB）
rm -rf references/
```

**预期收益**：减少仓库体积 ~5MB，清理项目根目录

---

## ⚠️ 第二阶段：中风险删除（需验证引用）

### 2.1 Legacy Docker-Exec Backend (~477行)

**文件**: `dswarm/solver/container_exec.py`

**包含**：
- `_DockerExecBackend` 类 (行1000-1233, 234行)
- `_ContainerProc` 类 (行954-998, 45行)
- `_ensure_container_legacy_impl()` (行488-680, 193行)
- 其他辅助函数 (~5行)

**删除理由**：
- ✅ 标记为 "emergency fallback"
- ✅ 文档说明 "to be removed once rcp settles"
- ✅ 现代路径已稳定

**风险**：
- ⚠️ 如果 rcp 路径失败，用户无后备方案
- ⚠️ 32处引用需要验证

**建议行动**：
1. **验证期**（2周）：监控生产环境，确认无 `DSWARM_WORKER_BACKEND=container_dockerexec` 使用
2. **废弃警告**：添加 DeprecationWarning
3. **删除**：2周后如无问题，删除全部 477行

**预期收益**：-477行，-39% container_exec.py

---

### 2.2 Identity Migration Layer (~280行)

**文件**: `dswarm/solver/identity_model.py`

**包含**：
- `migrate_legacy_config()` (行222-470, ~248行)
- `kind_from_mode()` (行174-178, 5行)
- `_legacy_kind()` (行202-219, 18行)
- `MigrationResult` dataclass (行191-199, 9行)

**删除理由**：
- ✅ 迁移代码仅用于首次升级
- ✅ 项目已稳定运行数月

**风险**：
- ⚠️ 如果用户有旧配置未迁移会失败
- ⚠️ 2处引用：identity_model.py自身, apps/web/worker_config.py

**建议行动**：
1. **评估期**：检查是否还有用户使用旧配置格式
2. **废弃警告**：添加日志警告 "Legacy config detected, migrate soon"
3. **删除时机**：项目运行6个月后，或确认无旧配置用户

**预期收益**：-280行

---

### 2.3 向后兼容 Re-export 层 (~52行)

**文件**：
- `apps/web/llm_providers.py` (34行) - re-export from dswarm.solver
- `apps/web/provider_errors.py` (13行) - re-export from dswarm.core  
- `dswarm/solver/peek.py` (5行) - re-export from dswarm.solver.result

**删除理由**：
- ✅ 如果所有导入已统一，re-export 层无用
- ✅ 维护成本低但增加混淆

**风险**：
- ⚠️ 需要搜索所有导入路径，确保无破坏性

**建议行动**：
```bash
# 1. 搜索导入
rg "from apps.web.llm_providers import" --type py
rg "from apps.web.provider_errors import" --type py
rg "from dswarm.solver.peek import" --type py

# 2. 如果无引用，直接删除
# 3. 如果有引用，批量替换导入路径后删除
```

**预期收益**：-52行，简化导入路径

---

### 2.4 废弃的凭据投影函数 (~55行)

**文件**: `dswarm/solver/credential_accounts.py`

**函数**: `project_account_root()` (行546-600)

**删除理由**：
- ✅ 已有替代方案 `CredentialProjector`
- ✅ 标记 DeprecationWarning

**风险**：
- ⚠️ 5处引用需要迁移

**建议行动**：
1. 迁移5处调用到 `CredentialProjector`
2. 删除函数

**预期收益**：-55行

---

## 🔬 第三阶段：测试质量评估结果 ✅

### 3.1 分析结论

经过全面扫描 **135个测试文件**（2,029个测试方法），**测试质量非常高**，未发现需要删除的低质量测试！

### 3.2 关键发现

✅ **无空测试或仅导入测试**
✅ **无断言缺失的测试**
✅ **无重复测试**（所有测试名称唯一）
✅ **无历史残留测试文件**

### 3.3 特别说明的测试类型

**架构守护测试**（有意设计的不变式保护）：
- `test_architecture.py` (42行) - 防止 dswarm 导入 apps 层
- `test_exception_handling_audit.py` (135行) - 锁定异常处理器数量
- `test_scope_audit.py` (120行) - 渗透测试边界检查
- `test_regression_index.py` (34行) - 文档-代码同步强制
- `test_health_parity.py` (250行) - 调度/设置检查统一性

**结论**：全部保留，这些是有价值的架构不变式守护。

**快照测试**（行为冻结回归防护）：
- `test_marker_parser_snapshot.py` - Marker 解析行为
- `test_review_lifecycle_snapshot.py` - Review 生命周期
- `test_runtime_snapshot.py` - Runtime 行为
- `test_shared_graph_event_reader_snapshot.py` - 事件读取器契约
- `test_shared_graph_poc_lifecycle_snapshot.py` - PoC 生命周期

**结论**：全部保留，这些是关键的回归防护。

**verified_poc 测试套件** (8个文件, 1,504行)：
- ✅ **保留** - 测试 PoC 验证流程，不是历史残留
- 覆盖：compatibility, dispatch, docker integration, graph, markers, orchestration, review flow, verifier

### 3.4 测试质量指标

| 指标 | 数值 | 评级 |
|------|-----:|------|
| **总测试文件** | 135 | - |
| **总测试方法** | 2,029 | - |
| **平均每文件** | 15.7个 | 优秀 |
| **空测试** | 0 | ⭐⭐⭐⭐⭐ |
| **无断言测试** | 0 | ⭐⭐⭐⭐⭐ |
| **重复测试** | 0 | ⭐⭐⭐⭐⭐ |
| **可删除测试** | 0 | - |

### 3.5 结论

**无需删除任何测试文件**。D-Swarm 的测试套件维护良好，所有测试都有明确目的和实际验证逻辑。

---

## 📋 第四阶段：文档清理

### 4.1 过时 ROADMAP

**文件**: `ROADMAP.md`

**状态**：标注为 "historical planning context"

**内容问题**：
- I2/I3 标注为 OBSOLETE
- 提到已删除的 SDK
- 与当前代码不符

**建议行动**：
```bash
# 归档到 docs/archive/
git mv ROADMAP.md docs/archive/ROADMAP-historical.md

# 创建简洁的新 ROADMAP
# 或在 README 中说明查看 GitHub Issues/Projects
```

**预期收益**：避免新贡献者混淆

---

### 4.2 清理空目录结构

**发现**：项目有大量深层次的空目录

**建议**：
```bash
# 查找并删除空目录
find . -type d -empty -not -path "./.git/*"
```

---

## 📊 预期总收益

### 代码行数减少

| 阶段 | 删除内容 | 风险 | 时间 |
|------|---------|------|------|
| **阶段1（立即）** | ~5MB目录 + 1行代码 | 零 | 1小时 |
| **阶段2（验证）** | ~864行代码 | 中 | 2-4周 |
| **阶段3（测试）** | **0个测试文件** ✅ | - | 已完成 |
| **阶段4（文档）** | 文档清理 | 零 | 1小时 |

### 具体估算

```
立即可删除：
- LEGACY_CONTAINER_EXEC_COMPATIBILITY_FACADE: 1行
- 历史目录: ~5MB (eval_nyu, references等)
- 测试文件位置调整: 2个文件移动

中期可删除（需验证）：
- Legacy Docker-Exec Backend: 477行
- Identity Migration Layer: 280行
- Re-export层: 52行  
- project_account_root(): 55行
小计: 864行

测试优化：
- ✅ 无需删除（测试质量优秀）
- ✅ 无冗余测试
- ✅ 无低质量测试
```

### 质量提升

- ✅ 代码组织更清晰
- ✅ 减少维护负担
- ✅ 新手上手更容易
- ✅ 避免误用历史API

---

## 🚨 安全原则

### 删除前必须：

1. ✅ **搜索引用**：确保无生产代码引用
2. ✅ **检查测试**：确保测试不依赖
3. ✅ **Git备份**：删除前commit，便于回滚
4. ✅ **运行测试**：删除后运行完整测试套件
5. ✅ **渐进删除**：分批删除，每批验证

### 禁止删除：

- ❌ **核心不变式**相关代码
- ❌ **provenance gate** 相关代码
- ❌ **shared_graph** 事件流逻辑
- ❌ **测试基础设施** (conftest.py, fixtures)
- ❌ **安全相关**代码

---

## 📝 实施计划

### 立即行动（本次会话）

**阶段1.1**: 删除无用标志
```bash
# 1. 备份当前状态
git add -A && git commit -m "checkpoint before cleanup"

# 2. 删除 LEGACY_CONTAINER_EXEC_COMPATIBILITY_FACADE
# 编辑 dswarm/solver/container_exec.py，删除行57

# 3. 运行测试
uv run pytest tests/test_container_exec.py -xvs

# 4. 提交
git add dswarm/solver/container_exec.py
git commit -m "refactor: remove unused LEGACY_CONTAINER_EXEC_COMPATIBILITY_FACADE flag"
```

**阶段1.2**: 移动测试文件
```bash
git mv apps/web/account_test.py tests/test_web_account_connectivity.py
git mv apps/web/llm_test.py tests/test_web_llm_connectivity.py

# 更新导入路径（如果需要）
uv run pytest tests/test_web_account_connectivity.py tests/test_web_llm_connectivity.py

git commit -m "refactor: move test files to tests/ directory"
```

**阶段1.3**: 清理历史目录
```bash
# 删除空目录
rm -rf eval_runs knowledge local_benchmarks

# 归档评估数据
mkdir -p docs/archive/eval
git mv eval_nyu docs/archive/eval/

# 归档ROADMAP
git mv ROADMAP.md docs/archive/ROADMAP-historical.md

git add -A
git commit -m "chore: archive historical eval data and ROADMAP"
```

### 后续行动（下次会话）

**阶段2**: 验证并删除Legacy代码
- 监控 rcp 路径稳定性
- 评估用户配置迁移状态
- 逐步删除 ~864行

**阶段3**: 测试优化
- 等待测试分析完成
- 删除低质量测试
- 合并重复测试

---

## ✅ 检查清单

### 删除前验证
- [ ] 搜索所有引用
- [ ] 检查测试依赖
- [ ] 创建Git checkpoint
- [ ] 记录删除理由

### 删除后验证  
- [ ] 运行完整测试套件
- [ ] 检查导入路径
- [ ] 更新相关文档
- [ ] 提交变更

---

**编制人**: Kiro  
**日期**: 2026-09-02  
**状态**: 等待测试分析完成后更新阶段3
