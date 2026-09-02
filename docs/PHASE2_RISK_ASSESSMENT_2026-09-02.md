# 阶段2清理 - 风险评估报告

**评估时间**: 2026-09-02  
**评估人**: Kiro  

---

## ⚠️ 重要结论

经过详细检查，**阶段2的所有项目都不适合立即删除**。

---

## 🔍 详细评估结果

### 1. Legacy Docker-Exec Backend (477行) ❌ 不可删除

**检查结果**:
- ✅ 代码存在：`_DockerExecBackend` 类及相关函数
- ✅ 测试引用：4个测试方法使用
- ✅ 生产代码：通过 `DSWARM_WORKER_BACKEND=container_dockerexec` 启用
- ⚠️ 文档说明："emergency escape hatch (to be removed after rcp path settles)"

**删除条件**：
- ⏳ 需要验证 rcp 路径在生产环境稳定运行 2-4周
- ⏳ 需要确认无用户设置 `DSWARM_WORKER_BACKEND=container_dockerexec`

**建议**: ⏳ **等待验证期**

---

### 2. Identity Migration Layer (280行) ❌ 不可删除

**检查结果**:
- ✅ 被3个文件引用：
  - `apps/web/worker_config.py` - 生产代码
  - `tests/test_identity_model.py` - 测试
  - `dswarm/solver/identity_model.py` - 自身

**删除条件**：
- ⏳ 需要确认所有用户完成从旧配置格式迁移
- ⏳ 建议项目稳定运行6个月后删除

**建议**: ⏳ **等待用户迁移完成**

---

### 3. project_account_root() (55行) ❌ 不可删除

**检查结果**:
- ✅ 被5个文件引用：
  - `apps/web/worker_models.py` - 生产代码
  - `dswarm/solver/container_probe.py` - 生产代码
  - `dswarm/solver/container_exec.py` - 生产代码
  - `dswarm/solver/credential_accounts.py` - 定义处
  - `tests/test_credential_accounts.py` - 测试

**删除条件**：
- ⏳ 需要先将5处引用迁移到 `CredentialProjector`
- ⏳ 需要测试迁移后的功能

**建议**: ⏳ **需要先完成迁移工作**（估计2-3小时）

---

### 4. Re-export层 (52行) ❌ 不可删除

**检查结果**:
- ❌ **仍在生产使用中**：
  - `apps.web.llm_providers` - **12处引用**
  - `apps.web.provider_errors` - **4处引用**
  - `dswarm.solver.peek` - **2处引用**

**引用文件（部分）**:
- `apps/web/reason_llm.py`
- `apps/web/run_manager.py`
- `apps/web/startup_test.py`
- `apps/web/worker_config.py`
- `apps/web/worker_settings.py`
- `apps/web/routes/*` (多个)
- `tests/*` (多个)

**删除条件**：
- ⏳ 需要先将所有18处引用改为直接导入
- ⏳ 需要测试所有导入路径正确

**建议**: ⏳ **需要大量重构工作**（估计4-6小时）

---

## 📊 阶段2总结

| 项目 | 行数 | 状态 | 工作量 | 风险 |
|------|-----:|------|--------|------|
| Legacy Docker-Exec | 477 | ⏳ 需验证 | 0小时 | 高 |
| Identity Migration | 280 | ⏳ 需确认 | 0小时 | 中 |
| project_account_root() | 55 | ⏳ 需迁移 | 2-3小时 | 中 |
| Re-export层 | 52 | ⏳ 需重构 | 4-6小时 | 低 |
| **总计** | **864** | **不可立即删除** | **6-9小时** | - |

---

## 💡 最终建议

### 推荐方案：分阶段执行 ⭐

**阶段2A（本次会话可选，2-3小时）**:
- 迁移 `project_account_root()` 的5处引用
- 删除函数（-55行）
- 风险：中，但可回退

**阶段2B（需要4-6小时）**:
- 重构18处 re-export 引用
- 删除3个 re-export 文件（-52行）
- 风险：低，纯重构

**阶段2C（需要2-4周验证）**:
- 删除 Legacy Docker-Exec Backend（-477行）
- 删除 Identity Migration Layer（-280行）
- 风险：高，需要生产验证

### 本次会话建议：⏸️ 暂停阶段2

**理由**：
1. 已完成阶段1（-66,827行，-4.8MB）✅
2. 阶段2需要额外6-9小时工作
3. 高风险项目需要2-4周验证期
4. 会话已经很长（9+小时）

**建议**：
- ✅ 保留当前成果
- ✅ 生成风险评估报告
- ✅ 下次会话再执行阶段2

---

## 🎯 结论

**阶段2清理不适合立即执行**，原因：
1. ⏳ 所有4个项目都需要额外工作或验证期
2. ⏰ 需要6-9小时额外工作量
3. ⚠️ 高风险项目需要生产验证

**推荐**：
- 本次会话到此为止
- 等待2-4周验证期
- 下次会话执行阶段2A/2B
