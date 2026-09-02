# cli_solver.py 拆分评估（2026-09-02）

## 现状

`cli_solver.py` 是 CliSolver 的核心实现（4425行），包含：
- Worker 生命周期管理
- Marker 解析与 flag 收割
- 多模式 prompt 管理（11个大型 prompt 常量）
- 执行流程控制

## Prompt 常量分析

| Prompt | 行范围 | 行数 | 用途 |
|--------|--------|-----:|------|
| `_EXEC_PROMPT` | 145-191 | 47 | 执行模式（通用 CTF） |
| `_RECON_PROMPT` | 192-224 | 33 | 侦察模式 |
| `_PENTEST_EXEC_PROMPT` | 225-287 | 63 | Pentest 执行模式 |
| `_KB_PROMPT` | 288-299 | 12 | 知识库查询提示 |
| `_RESUME_PROMPT` | 300-321 | 22 | 恢复/继续提示 |
| `_EXPLORE_PROMPT` | 322-352 | 31 | 探索模式 |
| `_EXPLORE_CONCLUDE_PROMPT` | 353-364 | 12 | 探索结论 |
| `_REVIEW_PROMPT` | 365-496 | 132 | Review 模式 |
| `_RESPOND_ASK_PROMPT` | 497-504 | 8 | 响应询问 |
| `_RESPOND_MARK_FALSE_PROMPT` | 505-515 | 11 | 标记错误响应 |
| `_RESPOND_WRITEUP_PROMPT` | 516-530 | 15 | Writeup 响应 |
| **总计** | | **386** | |

**拆分潜力**：外置 prompt 可减少 386行（-8.7%），剩余 4039行。

## 拆分建议

### 选项 A：外置 Prompt 模块（推荐）✅

**创建** `dswarm/solver/cli_prompts.py` (约 400行)

**优点**：
- 减少 cli_solver.py 约 400行
- Prompt 修改不触及核心逻辑
- 便于 prompt 工程迭代
- 可以为每个 prompt 添加文档/示例

**缺点**：
- 新增一个模块
- Prompt 与逻辑分离可能降低局部性

**实施成本**：低（移动常量 + 更新 import）

---

### 选项 B：拆分执行器（高风险）⚠️

将执行逻辑按模式拆分（exec/recon/pentest/explore/review）

**优点**：
- 职责更清晰
- 每个模式独立演进

**缺点**：
- 高风险：执行流程紧密耦合
- 可能引入循环依赖
- 需要大量测试验证

**实施成本**：高（需要重构核心流程）

---

### 选项 C：保持现状（最安全）✅

**理由**：
- `cli_solver.py` 是单一职责（CliSolver 实现）
- Prompt 是该职责的一部分
- 4425行虽大，但结构清晰
- 已有 `marker_parser.py` 外置

**建议**：
- 添加内部注释划分章节
- 保持现有测试覆盖

## 决策矩阵

| 方案 | 行数减少 | 风险 | 成本 | 维护性 | 推荐度 |
|------|----------|------|------|--------|--------|
| A: 外置 Prompt | -386 (-8.7%) | 低 | 低 | ++ | ⭐⭐⭐⭐ |
| B: 拆分执行器 | -1000+ | 高 | 高 | +? | ⭐⭐ |
| C: 保持现状 | 0 | 无 | 无 | = | ⭐⭐⭐ |

## 最终建议：**选项 A（外置 Prompt）** ✅

### 理由
1. **收益明显**：减少 400行，提升可读性
2. **风险可控**：只移动常量，不改逻辑
3. **实施简单**：1-2小时完成，测试回归快
4. **未来友好**：便于 prompt 工程和多语言支持

### 实施步骤
1. 创建 `dswarm/solver/cli_prompts.py`
2. 移动 11 个 prompt 常量
3. 为每个 prompt 添加文档字符串
4. 更新 `cli_solver.py` import
5. 运行完整测试套件验证

### 验收标准
- [ ] `cli_solver.py` 减少 ~380行
- [ ] 所有测试通过（特别是 `test_cli_executor.py`）
- [ ] Prompt 有清晰的文档注释

---

## 结论

**推荐外置 Prompt 常量**，作为 cli_solver.py 的第一次拆分。这是一个**低风险、高收益**的改进，
与 shared_graph.py 拆分策略一致（逐步外置、保持接口）。

---

**评估人**：Kiro  
**日期**：2026-09-02  
**推荐**：选项 A（外置 Prompt）
