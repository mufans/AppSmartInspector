# SQL Summarizer & Analysis Verifier 设计文档

> 基于 SmartPerfetto 对比分析，引入两个核心优化

## 一、SQL Summarizer

### 问题
当前 `perf_analyzer.py` 和 `frame_analyzer.py` 直接将 SQL 查询结果（可能数千行）传给 LLM，导致：
- Token 消耗高（一次分析可能 10k+ tokens）
- LLM 在大量数据中容易遗漏关键信息或产生幻觉
- 分析速度慢

### 方案
在 `deterministic.py` 中新增 `summarize_sql_result()` 函数，对 SQL 结果进行压缩：

```python
def summarize_sql_result(
    rows: list[dict],
    metric_col: str,
    top_n: int = 10,
    threshold_pct: float = 2.0,
) -> str:
    """将 SQL 查询结果压缩为统计摘要 + 异常采样。

    Args:
        rows: SQL 查询结果列表
        metric_col: 用于统计的数值列名
        top_n: 异常行采样数量
        threshold_pct: 异常阈值（平均值的倍数）

    Returns:
        压缩后的文本摘要
    """
```

### 压缩策略
1. **统计值**：count, min, max, avg, p95, p99（一行搞定）
2. **分布直方图**：将值分桶（<16ms, 16-32ms, 32-64ms, >64ms），统计每桶数量
3. **异常采样**：超过 avg * threshold_pct 的 top N 行
4. **去重聚合**：相同 class.method 的多行聚合为一条（总耗时、调用次数）

### 应用点
1. `perf_analyzer.analyze_perf()` — perf_json 传入前压缩
2. `frame_analyzer._build_frame_hints()` — frame_data 的 slices 列表压缩
3. `frame_analyzer._run_source_attribution()` — attributable 列表压缩
4. `deterministic.py` 的各分析函数中的数据预处理

### 预期收益
- Token 消耗降低 60-80%
- LLM 分析速度提升 30%+
- 减少幻觉（数据更聚焦）

## 二、Analysis Verifier

### 问题
当前 SI 的 LLM 分析是单次调用，没有验证机制。分析结果可能：
- 含糊其辞，没有具体数据支撑
- 遗漏重要问题
- 格式不统一

### 方案
在 `agents/` 下新增 `verifier.py`，实现分层验证：

```python
def verify_analysis(
    analysis_text: str,
    raw_hints: str,
    expected_fields: list[str] = None,
) -> VerificationResult:
    """验证 LLM 分析结果的质量。

    Returns:
        VerificationResult(score, issues, passed)
    """
```

### 验证层级（3层）

#### L1: Heuristic Check（纯规则，0 token）
- 结果是否包含具体数字（至少1个数值）
- 结果是否包含具体方法名或类名（至少1个）
- 结果长度是否合理（>100字符，<10000字符）
- 是否包含 P0/P1/P2 分级

#### L2: Consistency Check（纯规则，0 token）
- L1 预计算结论中标记为 P0 的问题，分析结果中是否提及
- L1 预计算结论中的关键数据点（如帧率、CPU使用率），分析结果中的数值是否一致（±20%）
- 异常采样中的热点方法，分析结果中是否覆盖

#### L3: Depth Check（可选，1次 LLM 调用）
- 根因是否追溯到具体原因（而非"建议进一步排查"）
- 是否给出可操作的优化建议
- 仅在 L2 不通过时触发

### 验证结果处理
- **L1+L2 全通过**：直接返回结果
- **L2 不通过**：将缺失的关键信息和原始提示词重新组装，让 LLM 补充分析（最多1次重试）
- **L1 不通过**：标记为低质量，返回结果但附带警告

### 集成点
1. `perf_analyzer.analyze_perf()` — 返回前调用 verify
2. `frame_analyzer.analyze_frame()` — 返回前调用 verify
3. `graph/nodes/reporter.py` — 报告生成前统一验证

## 三、实施计划

### Step 1: SQL Summarizer
1. 在 `deterministic.py` 中实现 `summarize_sql_result()`
2. 在 `perf_analyzer.py` 中集成：对 perf_json 中的 slices/block_events 列表应用压缩
3. 在 `frame_analyzer.py` 中集成：对 frame_data 的 slices 和 attributable 列表应用压缩
4. 测试：对比压缩前后的 token 消耗和 LLM 输出质量

### Step 2: Analysis Verifier
1. 新建 `agents/verifier.py`，实现 L1+L2 验证
2. 在 `perf_analyzer.py` 和 `frame_analyzer.py` 中集成
3. L3 暂不实现，作为后续优化项
4. 测试：构造正常和异常的 LLM 输出，验证检测准确率

### Step 3: 文档更新
1. 更新 CLAUDE.md Commands 章节
2. 更新 README.md 功能列表
3. 更新 docs/architecture-improvement-spec.md

## 四、设计约束
- **不改变现有 API 接口**：`analyze_perf()` 和 `analyze_frame()` 的签名不变
- **不引入新依赖**：纯 Python 实现，用标准库 statistics 模块
- **向后兼容**：压缩和验证都是内部优化，对外透明
- **遵循 LangGraph Pipeline Architecture Rule**：如果涉及图节点变更，复用现有链路
