# 文件 IO 分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (io_wait = 1)
- 字段: blocked_function 指示具体阻塞函数

## 领域知识
- 常见阻塞函数: folio_wait_bit_common, wait_on_page_bit, vfs_read/vfs_write
- SharedPreferences: commit() 同步写入磁盘，应在子线程使用 apply()

## 严重度标准
- P0: 主线程 IO 阻塞 > 帧预算
- P1: 主线程 IO 阻塞 > 5ms
- P2: 任何线程 IO 阻塞 > 10ms

## Metric 字段
- `file_io.blocking_events[].blocked_function`: 阻塞函数
- `file_io.blocking_events[].thread_name`: 线程名
- `file_io.blocking_events[].occurrences`: 发生次数
- `file_io.blocking_events[].total_ms`: 总阻塞时间
- `file_io.main_thread_total_ms`: 主线程总 IO 阻塞

## 优化方向
- 主线程禁止同步 IO: 使用协程或子线程
- SharedPreferences: 使用 apply() 替代 commit()
- 使用 mmap: 内存映射文件减少系统调用
