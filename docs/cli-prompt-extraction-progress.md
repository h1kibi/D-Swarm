# cli_solver Prompt 外置进度追踪

## 阶段 1：响应类 Prompt ✅ 完成（2026-09-02）

**外置 Prompt**：
- ✅ `RESPOND_ASK_PROMPT` (7行)
- ✅ `RESPOND_MARK_FALSE_PROMPT` (10行)
- ✅ `RESPOND_WRITEUP_PROMPT` (9行)
- ✅ `EXPLORE_CONCLUDE_PROMPT` (11行)

**结果**：
- cli_solver.py: 4425 → 4389行（-36行，-0.8%）
- 新增 cli_prompts.py: 50行
- 测试: ✅ 168 passed, 6 skipped

**提交**：`refactor: extract response prompts to cli_prompts module (phase 1)`

---

## 阶段 2：核心执行 Prompt（待实施）

**计划外置**：
- `EXEC_PROMPT` (47行)
- `RESUME_PROMPT` (22行)
- `KB_PROMPT` (12行)

**预期减少**：~81行

**注意事项**：
- `KB_PROMPT` 被多处使用，需保证格式化一致
- 建议逐个移动并测试

---

## 阶段 3：复杂 Prompt（待实施）

**计划外置**：
- `EXPLORE_PROMPT` (31行)
- `REVIEW_PROMPT` (132行)
- `RECON_PROMPT` (33行)
- `PENTEST_EXEC_PROMPT` (63行)
- `WEB_CTF_FOCUS_BLOCK` (特殊，被注入)

**预期减少**：~259行

**注意事项**：
- REVIEW_PROMPT 最长，需完整验证
- WEB_CTF_FOCUS_BLOCK 需要特别处理注入机制

---

## 总体进度

| 阶段 | 状态 | 行数 | 累计减少 |
|------|------|-----:|-------:|
| 阶段 1 | ✅ 完成 | -36 | -36 (-0.8%) |
| 阶段 2 | ⏳ 待实施 | -81 | -117 (-2.6%) |
| 阶段 3 | ⏳ 待实施 | -259 | -376 (-8.5%) |

**目标**：cli_solver.py 从 4425行 降至 ~4049行

---

## 下次会话

**推荐**：继续实施阶段 2（核心执行 Prompt）

**时间估算**：45-60分钟

**风险**：中（KB_PROMPT 被多处引用）

---

**更新时间**：2026-09-02  
**当前状态**：阶段 1 已完成并提交
