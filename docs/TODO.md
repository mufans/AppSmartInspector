# SmartInspector 优化待办

> 自动生成于 2026-04-14，由代码分析扫描得出

## 🔴 高优先级

- [ ] **测试覆盖率严重不足**：26个模块中仅有3个有测试（collector、perfetto、high_priority_fixes），测试覆盖率约12%。优先补充核心模块测试：token_tracker、orchestrator、attributor、reporter（来源：2026-04-14）
  - 优先级：高
  - 模块：全项目

- [ ] **collector/perfetto.py 过大（1340行）**：单文件承担了trace采集、SQL查询、数据解析、格式化等多项职责，需要拆分为多个子模块（来源：2026-04-14）
  - 优先级：高
  - 模块：collector/perfetto.py

- [ ] **commands/attribution.py 过大（789行）**：归因命令逻辑复杂，建议拆分为handler、formatter、presenter等子模块（来源：2026-04-14）
  - 优先级：高
  - 模块：commands/attribution.py

## 🟡 中优先级

- [ ] **graph/nodes/__init__.py 缺少模块导出**：analyzer、android、attributor、collector、explorer、orchestrator 均未在 __init__.py 中导出，影响模块可发现性（来源：2026-04-14）
  - 优先级：中
  - 模块：graph/nodes/__init__.py

- [ ] **缺少错误处理统一策略**：119个except块，但没有统一的错误处理框架。建议引入自定义异常类层次结构，区分可恢复错误和致命错误（来源：2026-04-14）
  - 优先级：中
  - 模块：全项目

- [ ] **Reporter token估算精度低**：使用 `len(content) / 1.5` 粗略估算CJK token，误差较大。建议使用tiktoken或模型自带的token计数器（来源：2026-04-14）
  - 优先级：中
  - 模块：graph/nodes/reporter/__init__.py

- [ ] **Reporter输出token估算粗糙**：`len(full_content) // 3` 估算输出token不够准确，建议从response.usage_metadata直接获取（来源：2026-04-14）
  - 优先级：中
  - 模块：graph/nodes/reporter/generator.py

- [ ] **TokenTracker缺少reset确认机制**：全局单例reset没有安全检查，多线程场景下可能误操作导致统计数据丢失（来源：2026-04-14）
  - 优先级：中
  - 模块：token_tracker.py

- [ ] **缺少配置验证**：config.py（170行）没有对API Key、模型名称等配置项做格式和有效性校验（来源：2026-04-14）
  - 优先级：中
  - 模块：config.py

## 🟢 低优先级

- [ ] **缺少类型注解覆盖率检查**：部分函数缺少返回值类型注解，建议加入mypy pre-commit hook（来源：2026-04-14）
  - 优先级：低
  - 模块：全项目

- [ ] **缺少日志分级策略**：debug_log函数存在但没有统一的日志级别配置，生产环境可能输出过多调试信息（来源：2026-04-14）
  - 优先级：低
  - 模块：tools/debug_log（如存在）

- [ ] **ws/server.py（340行）缺少WebSocket连接状态管理**：建议增加连接池管理和断线重连机制（来源：2026-04-14）
  - 优先级：低
  - 模块：ws/server.py

- [ ] **CLI缺少命令补全和帮助文档**：graph/cli.py 没有实现shell自动补全和详细的命令帮助文档（来源：2026-04-14）
  - 优先级：低
  - 模块：graph/cli.py

- [ ] **agents/deterministic.py（321行）和agents/attributor.py（464行）可抽取公共基类**：两个Agent有相似的状态管理逻辑，可抽象为BaseAgent（来源：2026-04-14）
  - 优先级：低
  - 模块：agents/

---

*共 14 条待办：高 3 / 中 6 / 低 5*
*生成工具：OpenClaw 自动代码扫描*
