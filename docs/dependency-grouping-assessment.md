# 依赖分组评估（2026-09-02）

## 现状

`pyproject.toml` 有两个依赖组：
- `dependencies` — 主依赖（30+ 包）
- `optional-dependencies.dev` — 开发/测试依赖（3包）

## PostgresBoard 依赖分析

### PostgreSQL 相关依赖

| 包 | 位置 | 用途 | 状态 |
|-------|------|------|------|
| `psycopg[binary]>=3.2` | 主依赖 | PostgreSQL 驱动 | ✅ 已有 |
| `psycopg-pool>=3.2` | 主依赖 | 连接池 | ✅ 已有 |
| `pgvector` | — | PostgreSQL 扩展（非 Python 包） | ✅ 数据库侧 |

### pgvector 说明

**pgvector** 是 PostgreSQL 扩展，不是 Python 包：
- 通过 SQL `CREATE EXTENSION IF NOT EXISTS vector` 创建（`postgres_board.py:85`）
- Python 侧只需 `psycopg` 驱动，无需额外包
- 生产部署时需在 PostgreSQL 数据库安装 pgvector 扩展

## 评估结论

### ✅ 当前依赖分组合理

**理由**：
1. **psycopg 必需性**：不仅 PostgresBoard 使用，其他组件也可能用（会话存储等）
2. **可选降级**：缺少 `DSWARM_BOARD_DSN` 时自动降级到 `MemoryBoard`
3. **分组成本**：移到可选依赖组收益不明显（psycopg 是常见依赖）

### 可选优化方案（不推荐）

**创建 `[project.optional-dependencies.postgres]`**：
```toml
postgres = [
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
]
```

**缺点**：
- 增加安装复杂度（用户需 `uv sync --extra postgres`）
- PostgresBoard 是生产标配，分离意义不大
- psycopg 体积不大（~1MB），分离收益有限

## 其他依赖观察

### 重型依赖（可能值得可选化）

| 包 | 用途 | 大小估算 | 可选性 |
|-------|------|---------|--------|
| `numpy>=2.4.6` | 数值计算 | ~20MB | 可能 |
| `scipy>=1.17.1` | 科学计算 | ~30MB | 可能 |
| `pillow>=12.2.0` | 图像处理 | ~5MB | 可能 |
| `gmpy2>=2.3.0` | 高精度数学 | ~1MB | 可能（crypto专用？） |
| `sympy>=1.14.0` | 符号计算 | ~10MB | 可能（crypto专用？） |
| `scapy>=2.7.0` | 网络包解析 | ~5MB | 可能（pwn专用？） |

**建议**：
- 如果这些是 track/category 特定的（crypto/pwn/forensics），可考虑拆分
- 需要先调研使用位置（是否只在特定 solver/challenge 类型用）
- 拆分后需要明确文档说明安装方式

### 当前可选依赖组

只有 `dev` 组（pytest 相关），建议保持。

## 建议行动

### 短期（本次会话）
- ✅ **保持现状**：psycopg 在主依赖中是合理的
- ✅ **文档化**：在 `README.md` 说明 PostgresBoard 需要数据库侧安装 pgvector 扩展

### 中期（下次重构）
- 🔍 **调研重型依赖使用**：确认 numpy/scipy/sympy 是否可按 track 拆分
- 📝 **补充依赖说明**：在 `pyproject.toml` 注释每个依赖的用途

### 长期（可选）
- 🎯 **Track 特定依赖组**：如果依赖按 challenge 类型聚类明显，可拆分：
  ```toml
  [project.optional-dependencies]
  crypto = ["gmpy2>=2.3.0", "sympy>=1.14.0", "pycryptodome>=3.23.0"]
  forensics = ["pillow>=12.2.0", "pyzbar>=0.1.9", "qrcode>=8.2"]
  pwn = ["scapy>=2.7.0", "ropgadget>=7.7", "capstone>=5.0.9"]
  ```

## 结论

**当前 psycopg 依赖分组合理，无需调整。**

PostgresBoard 依赖管理符合最佳实践：
- ✅ Python 驱动在主依赖
- ✅ 扩展安装文档化（部署指南）
- ✅ 降级机制完善（MemoryBoard）

**建议关闭本项，转而调研重型依赖（numpy/scipy/sympy）的使用分布。**

---

**评估人**：Kiro  
**日期**：2026-09-02  
**决策**：保持现状，psycopg 在主依赖中合理
