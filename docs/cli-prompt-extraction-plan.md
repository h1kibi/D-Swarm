# cli_solver Prompt 外置 - 低风险渐进式实施方案

## 背景

cli_solver.py 包含 11 个大型 prompt 常量（~386行），建议外置到独立模块以提升可维护性。

## 风险评估

**上次失败原因**：
1. 一次性移动所有 prompt，测试期望不匹配
2. Prompt 之间有格式依赖（如 `_WEB_CTF_FOCUS_BLOCK` 被注入到其他 prompt）
3. 测试用例检查特定字符串存在

**风险等级**：低-中（需要仔细处理依赖关系）

## 渐进式实施策略

### 阶段 1：创建模块并移动独立 prompt（低风险）✅

**目标**：先移动没有被其他代码格式化/注入的独立 prompt

**选择**：
- `_RESPOND_ASK_PROMPT` (8行) - 最简单
- `_RESPOND_MARK_FALSE_PROMPT` (11行)
- `_RESPOND_WRITEUP_PROMPT` (15行)
- `_EXPLORE_CONCLUDE_PROMPT` (12行)

**总计**：~46行，零依赖

**步骤**：
1. 创建 `dswarm/solver/cli_prompts.py`
2. 移动 4 个独立 prompt
3. 更新 cli_solver.py import
4. 替换引用
5. 运行测试验证

### 阶段 2：移动核心执行 prompt（中风险）

**目标**：移动主要执行模式 prompt

**选择**：
- `_EXEC_PROMPT` (47行)
- `_RESUME_PROMPT` (22行)
- `_KB_PROMPT` (12行)

**注意事项**：
- `_KB_PROMPT` 被多处使用，需要保证格式化一致

**步骤**：
1. 逐个移动并测试
2. 每个 prompt 移动后立即运行相关测试

### 阶段 3：移动 Explore 和 Review prompt（中风险）

**选择**：
- `_EXPLORE_PROMPT` (需要检查参数依赖)
- `_REVIEW_PROMPT` (132行，最长)

**注意事项**：
- Review prompt 非常长，需要确保完整性
- Explore prompt 可能有特定参数要求

### 阶段 4：移动 Pentest 相关 prompt（中风险）

**选择**：
- `_RECON_PROMPT` (33行)
- `_PENTEST_EXEC_PROMPT` (63行)
- `_WEB_CTF_FOCUS_BLOCK` (特殊：被注入到其他 prompt)

**注意事项**：
- `_WEB_CTF_FOCUS_BLOCK` 需要最后处理，因为它被动态注入

## 推荐方案：分 3 次 PR

### PR #1: 移动响应类 prompt（最安全）✅

**范围**：4 个最简单的 prompt（46行）

**影响**：
- 最小风险
- 快速验证流程
- 建立信心

**验收标准**：
```bash
uv run pytest tests/test_cli_executor.py -k "respond" -xvs
```

### PR #2: 移动核心执行 prompt（次安全）

**范围**：EXEC + RESUME + KB (81行)

**测试重点**：
- `test_cli_solver_offline_flag_threads_through`
- `test_kb_*` 相关测试

### PR #3: 移动剩余 prompt（需谨慎）

**范围**：EXPLORE + REVIEW + RECON + PENTEST + WEB_CTF_FOCUS_BLOCK (259行)

**特别注意**：
- REVIEW_PROMPT (132行) 需要完整迁移
- WEB_CTF_FOCUS_BLOCK 需要验证注入机制

## 立即可实施：阶段 1（46行）

### 创建 cli_prompts.py（第一版）

```python
"""Prompt templates for CliSolver response modes.

This module contains prompt constants for CliSolver's response and
exploration conclusion modes. These are the simplest, standalone prompts
with no formatting dependencies.

Gradually migrating from cli_solver.py to improve maintainability.
Phase 1: Response prompts (2026-09-02).
"""

RESPOND_ASK_PROMPT = (
    "The operator has a follow-up about the challenge you just worked. Answer it "
    "directly and concretely, drawing on what you already confirmed this session. "
    "If answering needs a quick check, you may run a command — but do not start a "
    "long new investigation; this is a conversation, not a fresh solve.\n\n"
    "Operator: {text}"
)

RESPOND_MARK_FALSE_PROMPT = (
    "IMPORTANT: the flag you reported — {flag} — is a FALSE POSITIVE (the operator "
    "verified it does not work). Treat it as a dead-end: do NOT report it again. "
    "Resume solving from the facts you already confirmed and find the REAL flag.\n"
    "{note}\n"
    "Actually RUN commands against the real target/files. When you recover the TRUE "
    "flag from REAL output, print it on its own line exactly as:\n  FOUND_FLAG=<flag>\n"
    "It must appear verbatim in your shell output. Also print VERIFIED_FACT=<...> / "
    "DEADEND=<...> lines as you go so the team's board stays current."
)

RESPOND_WRITEUP_PROMPT = (
    "Write a concise CTF WRITEUP for the challenge you just solved, in Chinese. "
    "Base it ONLY on what you actually confirmed this session — do not invent steps. "
    "Structure it as:\n"
    "  ## 漏洞点  (the root cause / vulnerability)\n"
    "  ## 利用步骤  (numbered, reproducible — the real commands/requests you used)\n"
    "  ## Flag  (the flag and where it came from)\n"
    "Keep it tight and technical. Output ONLY the markdown writeup, nothing else."
)

EXPLORE_CONCLUDE_PROMPT = (
    "Summarize your exploration attempt.\n\n"
    "What did you try? What worked? What failed? Should teammates retry this angle or "
    "abandon it?\n\n"
    "If this direction is exhausted, emit:\n"
    "  DEADEND=<concise explanation why this is ruled out>\n"
)
```

### 修改步骤

1. **创建模块**：`dswarm/solver/cli_prompts.py`（如上）
2. **添加 import**：在 cli_solver.py 顶部添加 `from dswarm.solver import cli_prompts`
3. **替换引用**：
   - `_RESPOND_ASK_PROMPT` → `cli_prompts.RESPOND_ASK_PROMPT`
   - `_RESPOND_MARK_FALSE_PROMPT` → `cli_prompts.RESPOND_MARK_FALSE_PROMPT`
   - `_RESPOND_WRITEUP_PROMPT` → `cli_prompts.RESPOND_WRITEUP_PROMPT`
   - `_EXPLORE_CONCLUDE_PROMPT` → `cli_prompts.EXPLORE_CONCLUDE_PROMPT`
4. **删除原始定义**：删除 cli_solver.py 中这 4 个 prompt 的定义
5. **运行测试**：
   ```bash
   uv run pytest tests/test_cli_executor.py -xvs
   ```

### 预期结果

- cli_solver.py: 4425 → 4379行（-46行，-1.0%）
- 新增 cli_prompts.py: ~60行（含文档）
- 所有测试通过
- 零功能变更

### 回滚计划

如果测试失败：
```bash
git checkout HEAD -- dswarm/solver/cli_solver.py
rm dswarm/solver/cli_prompts.py
```

## 时间估算

- **阶段 1**：30 分钟（-46行）✅ 推荐立即实施
- **阶段 2**：45 分钟（-81行）
- **阶段 3**：60 分钟（-259行）

**总计**：~2-3 小时完成全部外置（-386行）

## 决策点

**是否现在实施阶段 1？**

**优点**：
- ✅ 最低风险（46行独立 prompt）
- ✅ 快速验证流程
- ✅ 立即减少 1% 行数
- ✅ 30 分钟完成

**缺点**：
- ⚠️ 需要额外提交
- ⚠️ 收益相对较小

**推荐**：✅ **现在实施阶段 1**，验证流程后再继续后续阶段

---

**评估人**：Kiro  
**日期**：2026-09-02  
**推荐**：渐进式 3 阶段迁移，从最简单的 46 行开始
