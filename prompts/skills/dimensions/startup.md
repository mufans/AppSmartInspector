# 冷启动分析

## 数据源
- SQL 表: `android_thread_slices_for_all_startups` + `slice`

## 领域知识
- 冷启动阶段: pre-main -> Application.onCreate -> Activity.onCreate -> 首帧渲染
- 热启动: Activity 从后台恢复，无进程创建

## 严重度标准
- P0: 冷启动 > 5s
- P1: 冷启动 > 2s
- P2: 冷启动 > 1s

## 优化方向
- 延迟初始化: 非必要组件延迟到首帧后
- 减少 Application.onCreate 耗时
- 使用 App Startup 库
- 避免主线程 IO 和锁等待
