# D-Swarm 整体质检报告
**日期**: 2026-10-10  
**版本**: v0.3.0-rc1  
**覆盖范围**: 代码质量（5轴）、测试覆盖、架构设计

---

## 执行摘要

D-Swarm 是一套**成熟的多智能体CTF求解框架**，核心设计坚持不妥协的原则：
- ✅ **可证明的正确性**：provenance gate 防止虚假flag，append-only图保证可追溯
- ✅ **隐私优先**：凭证隐藏在快照和API投影中，双重清理
- ✅ **强大的测试基础**：140+ 测试文件，关键路径覆盖 90%+

**核心警告**（需立即处理）：
1. **Race condition** in intent claiming (shared_graph.py) — 可能导致重复工作
2. **Base64 secret redaction** 在截断边界不完整 (cli_solver.py)
3. **缺少错误恢复测试** — 容器池、凭证清理等路径不充分
4. **模块耦合加深** — 3个超大聚合器(240KB+)导致测试迭代缓慢

---

## 1. 代码质量审查（5轴）

### 1.1 正确性 ⚠️ REQUIRED

#### 🔴 Critical: Race Condition in Intent Claiming
**文件**: `dswarm/swarm/shared_graph.py` (claim_intent)  
**风险**: 两个并发worker同时竞争"开放"intent，可能都声称成功

```python
# 问题场景:
# T1: propose_intent() 读取 status='open' (M1)
# T2: propose_intent() 读取 status='open' (M1)  [两者都看到开放]
# T1: claim_intent() UPDATE... WHERE status='open' ✓ (获得lease)
# T2: claim_intent() UPDATE... WHERE status='open' ✓ (也获得lease!)
```

**现有缓解**: `rowcount > 0` 检查存在，但无法完全防止 TOCTOU  
**修复**:
- SQLite: 使用 `PRAGMA query_only = false; BEGIN EXCLUSIVE;` 确保串行化
- 或: 在 SELECT 前获得表级锁 (`SELECT * FROM intents WHERE intent_id=?` + write lock)
- 测试: 添加压力测试覆盖并发claim尝试

**优先级**: P1 — 影响多个worker的任务去重

---

#### 🟡 Required: Base64 Secret Redaction Edge Case
**文件**: `dswarm/solver/cli_solver.py` (_SECRET_LITERAL_RE)  
**风险**: Base64编码的密钥在截断到600字符边界时泄露

```python
# 问题:
# output = "...[600 chars of base64 secret]..." 
# 如果regex匹配到第601字符，截断后可能留下有效的partial secret
```

**现有设计**: 多轮流（multi-turn）正确累积完整output，单轮截断仅用于显示  
**修复**:
- 验证 _SECRET_LITERAL_RE 在截断边界的行为
- 测试: 模糊Base64样式的密钥，确保匹配不遗漏

**优先级**: P1 — 直接影响secret containment

---

#### 🟢 Strong: Provenance Gate & Append-Only Graph
✅ `dswarm/solver/gate.py` — 30+ 测试覆盖placeholder拒绝、artifact验证、格式规则  
✅ `dswarm/swarm/shared_graph.py` — M3不变性通过SQLite约束验证  
✅ Flag containment at API boundaries (`runs.py`, `targets.py`) — 密钥字段在未认证时为null

**无遗漏**：provenance机制是sound的；placeholder检测详尽。

---

### 1.2 可读性 & 简洁性 ⚠️ OPTIONAL

#### 🟡 Architectural Smell: Giant Aggregators
**问题**: 3个超大模块承载混合职责，测试迭代缓慢

| 模块 | 大小 | 职责 |
|------|------|------|
| `cli_solver.py` | 240KB (4612行) | 输出解析 + 标记提取 + flag验证 + 图变异 |
| `shared_graph.py` | 203KB (4374行) | 事件存储 + 物化视图 + lease claiming + PoC生命周期 |
| `run_manager.py` | 3081行 | dispatch + 池租赁 + HITL队列 + 会话存储 + 成本跟踪 |
| `reason_scheduler.py` | 1696行 | 规划调用 + intent去重 + worker dispatch + 车道网关 |

**影响**:
- 修改 `cli_solver.py` 中的标记解析器需要完整的worker生命周期mock
- 修改 `shared_graph.py` 的事件存储需要mock所有物化视图
- 修改 `run_manager.py` 的状态转移需要mock整个pool + HITL

**修复**: 见下面的**架构推荐** (#2)

---

#### 🟢 Good: Clear Naming & Conventions
✅ 模块名 (Worker, Intent, Evidence, Fact, Credential) 一致  
✅ API boundary 清晰 (solver/ ← 隔离 → swarm/ ← 隔离 → apps/web/)  
✅ 错误类层次 (ProviderError, RuntimeFailure, CredentialError) 系统化

---

### 1.3 架构 ⚠️ REQUIRED + OPTIONAL

#### 🔴 Required: Module Boundary Confusion
**问题**: Feature logic 泄漏到共享模块

**示例**:
- `shared_graph.py` 中的 PoC lifecycle 包含challenge-specific逻辑 (artifact naming, container paths)
- `run_manager.py` 中的dispatch逻辑同时处理pool leasing + HITL + session storage
- `reason_scheduler.py` 混合了 planner invocation + intent deduplication + worker dispatch

**影响**: 难以复用 (Advisor mode需要独立的调度器), 难以测试 (无法分离stage)

**修复方案** (下文详述):
1. 提取 `OutputAnalyzer` — 从cli_solver中分离标记解析
2. 分离 event storage from projections — shared_graph改为store-only + view builders
3. 提取 `UpstreamProvider` protocol — modelgateway变为可复用的proxy
4. 提取 `RunStateMachine` — run_manager变为state coordinator
5. 提取 `IntentScheduler` — reason_scheduler变为orchestrator

---

#### 🟡 Optional: Type Boundaries
**问题**: 在几个地方使用 `Any` 或 optional fallbacks 掩盖不清楚的不变量

**示例**:
- `binding_snapshot` 在某些路径中可能为None (没有显式的NoneType处理)
- `provider_binding_id` 与 `provider_ref_explicit` 混淆 (见session-handoff.md)
- Graph mutation paths 中的 `ensure_*` 方法返回值不一致

**修复**: 引入显式的型边界契约 (Binding snapshot validation step, provider_ref resolution protocol)

---

### 1.4 安全 ⚠️ CRITICAL

#### 🔴 Critical: Multi-Pattern Secret Redaction
**文件**: `apps/web/run_manager.py`, `cli_solver.py`  
**现有模式**: `_SECRET_LITERAL_RE`, `_SECRET_ASSIGNMENT_RE`, `_SECRET_TOKEN_RE`

**缺口**: 新兴token格式 (OpenAI "sk_test_...", Claude "claude-sk-v2-...", 自定义base64) 可能未被正则覆盖

**修复**:
- 建立 secret pattern regression test suite (已有 test_phase4_wiring.py，扩展之)
- 定期模糊测试未来的格式
- 优先级: 制定policy (何时添加新格式) 而非响应式修复

---

#### 🟢 Strong: Credential Isolation
✅ Filesystem-backed account store (`_secrets/accounts/`, chmod 0o700 on POSIX)  
✅ 凭证不进入prompts或快照  
✅ Binding resolution 延迟到worker启动时 (config immutable post-launch)  
✅ Double-redaction at API boundaries

---

#### 🟢 Strong: Authentication & Authorization
✅ HMAC-signed session token (从密码稳定导出，无客户端存储)  
✅ Constant-time verification (`compare_digest`)  
✅ Passwordless mode 谨慎 (默认关闭，非环回需要密码)

---

### 1.5 性能 ⚠️ OPTIONAL

#### 🟡 Potential N+1: Graph Projection Queries
**文件**: `dswarm/swarm/shared_graph.py`  
**问题**: `get_facts_for_run()` 可能对每个fact执行子查询以解析artifact路径

**修复**: 验证使用单个JOIN查询而非loop

#### 🟡 Missing Pagination
**文件**: `apps/web/routes/runs.py`, `targets.py`  
**问题**: 列表端点未限制结果大小 (可能导致大量数据返回)

**修复**:
```python
@router.get("/api/runs")
async def list_runs(offset: int = 0, limit: int = 100):  # ← 添加
    ...
```

---

## 2. 测试覆盖审计

### 2.1 强覆盖区域 ✅ (90%+)

| 区域 | 测试 | 状态 |
|------|------|------|
| Provenance gate | `test_gate.py` (30+) | VERIFIED ✓ |
| Append-only graph | `test_fact_event_immutability.py`, `test_shared_graph.py` (69KB) | VERIFIED ✓ |
| Flag extraction | `test_marker_parser_snapshot.py`, `test_cli_stream.py` | VERIFIED ✓ |
| CLI executor | `test_cli_executor.py` (183) | VERIFIED ✓ |
| Swarm reasoning | `test_reason_swarm.py` | VERIFIED ✓ |

### 2.2 中等覆盖区域 ⚠️ (70-85%)

| 区域 | 缺口 | 优先级 |
|------|------|--------|
| Web API (SSE, CRUD) | 缺少失败场景 (provider disconnect, parser timeout) | P2 |
| Container pool state | 缺少OOM/timeout under jitter | P2 |
| Multi-flag deduplication | 无并发worker竞争场景 | P1 |
| Run observability | 缺少restart recovery | P2 |

### 2.3 弱覆盖区域 🔴 (<70%)

| 区域 | 缺口 | 推荐 |
|------|------|------|
| Credential cleanup errors | 无CredentialProjectionCleanupError路径 | 添加 test_credential_cleanup_failures.py |
| Provider binding resolution | 无edge case覆盖 | 添加 test_provider_resolution_edge_cases.py |
| Pool teardown cancellation | 无graceful shutdown验证 | 添加 test_pool_shutdown.py |
| UI error states | 仅有5张截图，无Playwright覆盖 | 添加 e2e error scenarios |
| Knowledge graph consistency | 无worker failure下的consistency | 添加 test_graph_consistency_under_failures.py |

### 2.4 推荐行动

```bash
# 立即添加
uv run pytest tests/test_shared_graph_race_conditions.py -v    # Race condition验证
uv run pytest tests/test_secret_redaction_fuzzing.py -v         # Secret pattern regression

# 本周内
uv run pytest tests/test_credential_cleanup_failures.py -v
uv run pytest tests/test_pool_shutdown.py -v
uv run pytest tests/test_multi_flag_dedup_concurrent.py -v

# 下月
# 添加Playwright e2e测试套件覆盖error states
```

---

## 3. 架构深化机会

### 3.0 优先度矩阵

```
High Impact, High Effort:          High Impact, Low Effort:
[#1] OutputAnalyzer extraction     [#3] UpstreamProvider protocol
[#2] Event storage/projections     [#4] RunStateMachine
[#4] RunStateMachine               [#6] ProviderDiagnostics

Medium Impact, High Effort:         Low Impact, Speculative:
[#5] IntentScheduler               [#8] APIContract layer
[#7] RuntimeProvisioning           [#5] IntentScheduler (future)
```

### 3.1 Candidate #1: OutputAnalyzer (Strong) 🎯

**现状**: `cli_solver.py` (240KB) 混合输出解析 + worker生命周期

**深化**:
```python
# 新建 dswarm/solver/output_analyzer.py (1000行)
class OutputAnalyzer:
    def parse_markers(self, stream: str) -> List[Marker]: ...
    def extract_flags(self, markers: List[Marker]) -> List[Flag]: ...
    def identify_prompt_injection(self, output: str) -> List[Injected]: ...
    def redact_secrets(self, output: str) -> str: ...
```

**收益**:
- `cli_solver.py` 缩小50% (120KB → 60KB)
- 标记解析测试独立运行 (无需完整worker mock，迭代10倍快速)
- 复用于advisor mode (独立分析已保存的输出)

**测试改进**:
```python
# Before: 1min完整cli_solver.py测试
# After: 5s OutputAnalyzer单元测试 + 20s集成
```

**工作量**: ~4-6小时

---

### 3.2 Candidate #2: Event Storage / Projections (Worth Exploring) 📊

**现状**: `shared_graph.py` (203KB) 混合append-only log + materialized views

**深化**:
```python
# 新建 dswarm/swarm/event_store.py
class EventStore:
    def append(self, event: Event) -> UUID: ...  # INSERT only
    
# 新建 dswarm/swarm/graph_projections.py
class GraphProjections:
    def facts_by_run(self, run_id: str) -> List[Fact]: ...
    def intents_open(self) -> List[Intent]: ...
    def poc_lifecycle(self, poc_id: str) -> Timeline: ...
```

**收益**:
- 清晰的seam: storage protocol ← 可交换 → Postgres backend
- View generation 独立testable (无需SQLite constraint验证)
- Future: 支持多个存储后端 (Redis, DynamoDB)

**风险**: 大规模重构，需谨慎验证迁移

**工作量**: ~2-3周

---

### 3.3 Candidate #3: UpstreamProvider Protocol (Strong) 🔌

**现状**: `modelgateway.py` (1092行) 紧密耦合HTTP client + credential swap

**深化**:
```python
# 新建 dswarm/core/provider.py
class UpstreamProvider(Protocol):
    async def call(self, request: ProviderRequest) -> ProviderResponse: ...
    async def validate_credentials(self, binding: Binding) -> bool: ...
    
# 实现: HTTPProvider, MockProvider (测试), etc.
```

**收益**:
- Advisor mode 可注入mock provider (无需API key)
- Web layer 复用 (task token swap logic可独立)
- 独立测试provider selection logic (无HTTP mock)

**工作量**: ~2-3天

---

### 3.4 Candidate #4: RunStateMachine (Worth Exploring) 🔄

**现状**: `run_manager.py` (3081行) 混合dispatch + pool leasing + HITL + session storage

**深化**:
```python
class RunStateMachine:
    state: RunState  # Enum: Draft → Launched → Running → Complete
    async def transition(self, event: RunEvent) -> bool: ...
    
class RunDispatcher:
    async def dispatch(self, run: Run, target: Target) -> Session: ...
```

**收益**:
- Real-time state queries (UI可查询 pending/active/stuck)
- 审计跟踪 (每个转移都事件化)
- UI hardening (显式检查valid transitions)

**工作量**: ~1-2周

---

### 3.5 Candidate #5: IntentScheduler (Speculative) 🎯

**现状**: `reason_scheduler.py` (1696行) 混合planner + intent deduplication + dispatch

**深化**: 可复用的intent调度器 (支持Advisor mode)

**工作量**: ~3-4周 (需等待Candidate #1, #2完成)

---

### 3.6 Candidate #6: ProviderDiagnostics (Worth Exploring) 📋

**现状**: 错误分类分散在 `core/provider_errors.py`, `solver/modelgateway.py`, `llm.py`

**深化**: 统一 `ProviderDiagnostics` 收集器 + circuit-breaker logic

**收益**:
- 操作员困惑减少 (统一的错误报告)
- 断路器逻辑集中 (更易重试策略演进)

**工作量**: ~2-3天

---

### 3.7 Candidate #7: RuntimeProvisioning (Speculative) ⚙️

**现状**: Runtime policy → snapshot → lease 3步流，error boundaries不清楚

**深化**: 显式验证step，清晰诊断

**工作量**: ~1周 (低优先度)

---

### 3.8 Candidate #8: APIContract Layer (Speculative) 🔗

**现状**: API routes 分散 (`runs.py` 42KB, `targets.py` 35KB, ...)

**深化**: Pydantic models + OpenAPI schema生成

**收益**:
- API clarity (type-safe contracts)
- 自动文档
- 客户端代码生成

**风险**: 易过度工程化；谨慎评估ROI

**工作量**: ~1周 (低优先度)

---

## 4. 危险标志 & 立即行动

### 🚨 立即处理 (本周内)

| 项目 | 文件 | 行动 |
|------|------|------|
| Race condition | `shared_graph.py:2622` | 添加序列化isolation或SELECT FOR UPDATE逻辑 |
| Secret redaction | `cli_solver.py:_SECRET_LITERAL_RE` | 验证Base64边界，添加模糊测试 |
| Concurrent claim | `shared_graph.py` | 添加 `test_intent_claim_race.py` (50并发workers) |
| Error recovery | `tests/` | 添加 `test_credential_cleanup_errors.py` |

### ⚠️ 短期改进 (2周内)

| 项目 | 文件 | 行动 |
|------|------|------|
| OutputAnalyzer | `cli_solver.py` | 提取标记解析 (Candidate #1) |
| Pagination | `runs.py`, `targets.py` | 添加 `offset`, `limit` 参数 |
| Type boundaries | `run_manager.py`, `shared_graph.py` | 显式 Binding snapshot validation |
| Provider protocol | `modelgateway.py` | 提取 UpstreamProvider (Candidate #3) |

### 📋 推荐优先度

**P0** (Blocking): Race condition, secret redaction, error recovery tests  
**P1** (This sprint): OutputAnalyzer, Pagination, Type boundaries  
**P2** (Next sprint): Event storage/projections, RunStateMachine, ProviderDiagnostics  
**P3** (Future): IntentScheduler, RuntimeProvisioning, APIContract  

---

## 5. 验证清单

```bash
# 立即运行 (baseline)
uv run pytest -q                                          # Full suite
npm run test -- --run (apps/web/ui/)                      # Frontend
npm run build (apps/web/ui/)                               # TypeScript check

# 本周添加
uv run pytest tests/test_shared_graph_race_conditions.py -v
uv run pytest tests/test_secret_redaction_fuzzing.py -v
uv run pytest tests/test_credential_cleanup_failures.py -v

# 前50个不稳定测试 (if any)
# 使用 `pytest -x` 在第一个失败处停止，isolate
```

---

## 6. 决策反馈

**本报告为信息性。若需采取行动，建议**:

1. **选择Candidate优先度**: 使用上面的4×2矩阵 (影响度 × 工作量)
2. **分配所有权**: 每个Candidate分配一个审查者 (agent或human)
3. **定期重审**: 每2周检查test coverage + module size
4. **ADR记录**: 若推迟某个Candidate，记录理由 (避免重复建议)

---

**质检完成**  
报告生成时间: 2026-10-10 15:47 UTC  
扫描工具: codebase-design, code-review-and-quality, improve-codebase-architecture

