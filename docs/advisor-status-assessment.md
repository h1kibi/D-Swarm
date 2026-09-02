# Advisor 生产状态评估与决策（2026-09-02）

## 现状

**Advisor** 是离线实验框架（~3078行），用于评估 Reason 建议是否改善意图质量。

### 模块组成
| 模块 | 行数 | 职责 |
|------|-----:|------|
| `advisor_experiment.py` | 628 | 冻结 fixture、建议 block 构造 |
| `advisor_sidecar.py` | 874 | 离线证据收集 sidecar |
| `advisor_runner.py` | 588 | baseline vs Advisor arm 对比实验 |
| `advisor_report.py` | 632 | 实验报告生成 |
| `advisor_benchmark.py` | 356 | 基准测试工具 |
| **总计** | **3078** | |

### 架构边界（docs/00-architecture-spec.md §4.6）
> Advisor 建议（M8）**仅限离线实验收集，生产永久 No-Go（污染风险）**。子系统引入在线规划/联网检索需另立 RFC。

### 实现状态（docs/10 §M8）
- ✅ 已实现：离线 baseline vs Advisor 对比框架
- ✅ 已隔离：生产代码不导入任何 M8 模块
- ✅ 已测试：`test_advisor_experiment.py` + `test_advisor_sidecar.py`
- ❌ 未接线：reason_scheduler / swarm 无 Advisor 调用路径
- ❌ 无环境变量：`.env.example` 无 `DSWARM_ADVISOR_*` 配置

## 评估

### ✅ 保留理由
1. **研究价值**：提供离线能力评估框架，可对比 baseline
2. **架构清晰**：完全隔离，不污染生产路径
3. **实现完整**：有完整的 fixture/runner/report 流程
4. **测试覆盖**：2个专门测试文件

### ⚠️ 移除理由
1. **未使用**：生产永久 No-Go，无接线计划
2. **维护成本**：3078行代码需要随内核演进同步
3. **依赖膨胀**：可能引入额外的依赖
4. **混淆风险**：新贡献者可能误以为 Advisor 可用

## 决策：**保留但明确标注** ⚠️

Advisor 是合法的研究工具，不应移除，但需要更清晰的标注防止误用。

### 行动项

#### 1. 添加模块级文档字符串警告（优先级：高）
在 `advisor_*.py` 顶部添加：
```python
"""
⚠️ EXPERIMENTAL OFFLINE RESEARCH FRAMEWORK ONLY ⚠️

This module is part of the M8 Advisor offline evidence collection experiment.
It is NOT wired into production and MUST NOT be imported by swarm.py or
reason_scheduler.py. Production Advisor remains permanently No-Go per
docs/00-architecture-spec.md §4.6 (contamination risk).

For research use only. See docs/10 §M8 for experiment protocol.
"""
```

#### 2. 添加 .advisorignore 或移到 research/ 目录（优先级：中）
选项 A：创建 `research/advisor/` 目录，移动所有 `advisor_*.py`  
选项 B：在 `dswarm/swarm/advisor_*.py` 顶部加 `# type: ignore[unused]` 注释

#### 3. 更新 README（优先级：中）
在 README 添加 "Research Modules" 节，明确列出 Advisor 的边界。

#### 4. 添加 lint 规则（优先级：低）
在 CI 中添加检查：`swarm.py` / `reason_scheduler.py` 不得 import `advisor_*`

## 对比建议

| 方案 | 优点 | 缺点 |
|------|------|------|
| **保留（标注）** | 保留研究能力；成本低 | 代码库膨胀 |
| **移到 research/** | 边界更清晰；主代码更干净 | 需要调整 import |
| **彻底移除** | 代码库最小；无维护成本 | 丢失研究能力；未来重建成本高 |

**推荐**：**保留 + 添加警告文档字符串** （最小改动，最大透明度）

## 结论

**Advisor 是合法的离线研究框架，应保留但添加明确的 No-Go 警告，防止误用。**

---

**评估人**：Kiro  
**日期**：2026-09-02  
**决策**：保留 + 标注  
**下一步**：添加模块级警告文档字符串
