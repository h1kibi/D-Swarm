# PostgresBoard 评估与决策（2026-09-02）

## 现状

**PostgresBoard** 是可选的 PostgreSQL + pgvector 黑板实现（538行），与 `MemoryBoard` 共同实现 `Board` Protocol。

### 使用场景
- **开发/测试**：默认使用 `MemoryBoard`（内存实现）
- **生产部署**：`docker-compose.yml` 配置 `DSWARM_BOARD_DSN`，使用 `PostgresBoard`

### 调用路径
1. **Swarm 启动**：`swarm.py:1338` — 读取 `DSWARM_BOARD_DSN`，有值则实例化 `PostgresBoard`，否则用 `MemoryBoard`
2. **Run 清理**：`apps/web/run_manager.py:593` — 删除 run 时调用 `PostgresBoard.drop_schema()` 清理 schema

### 技术细节
- 每个 run 独立 schema（`run_{challenge_id}`）
- pgvector 扩展用于 embedding 相似度搜索
- 实现完整的 finding/pheromone 持久化
- 有测试覆盖：`tests/test_postgres_board_contract.py`

## 评估

### ✅ 保留理由
1. **生产在用**：`docker-compose.yml` 明确配置，移除会破坏生产部署
2. **架构清晰**：通过 `Board` Protocol 解耦，不侵入核心逻辑
3. **可选设计**：本地开发不需要 PostgreSQL，降级到 `MemoryBoard`
4. **功能完整**：pgvector 相似度搜索在大规模 finding 场景下有价值
5. **测试覆盖**：有专门的契约测试

### ⚠️ 潜在问题
1. **文档缺失**：架构规范未明确说明 Board 层的作用
2. **可观测性**：Board 写入失败的观测性不明确
3. **依赖重**：引入 psycopg + pgvector，但只在生产用

## 决策：**保留并文档化** ✅

PostgresBoard 是生产必需组件，不应移除。应做的改进：

### 1. 文档化 Board 层（优先级：高）
在 `docs/00-architecture-spec.md` 补充：
- Board 层在架构图中的位置
- MemoryBoard vs PostgresBoard 的选择逻辑
- DSWARM_BOARD_DSN 配置说明

### 2. 观测性改进（优先级：中）
- Board 写入失败是否需要 blackboard delta？
- 当前有 `except Exception: pass` 吗？检查 `docs/exception-handling.md` 分类

### 3. 依赖优化（优先级：低）
- psycopg 是否已在 pyproject.toml 的可选依赖组？
- 考虑把 PostgresBoard 相关依赖放入 `[postgres]` extra

## 行动项

- [x] 评估 PostgresBoard 现状
- [ ] 更新 `docs/00-architecture-spec.md` 补充 Board 层说明
- [ ] 检查 `postgres_board.py` 是否有未分类的静默异常
- [ ] 验证 pyproject.toml 依赖分组

## 结论

**PostgresBoard 是合理的生产组件，通过 Protocol 解耦设计良好，应保留并补充文档。**

---

**评估人**：Kiro  
**日期**：2026-09-02  
**决策**：保留 + 文档化
