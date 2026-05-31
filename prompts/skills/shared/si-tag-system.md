# SI$ 标签格式定义和解析规则

## 概述

Android Hook 层通过 `android.os.Trace.beginSection()` / `endSection()` 向 Perfetto trace 注入自定义切片，所有自定义标签以 `SI$` 前缀标识。

## 标签模式

### SI$RV# — RecyclerView 管线
- **格式**: `SI$RV#[viewId]#[Adapter].[method]`
- **示例**: `SI$RV#recycler_view#DemoAdapter.onBindViewHolder`
- **包含方法**: `onBindViewHolder`, `onCreateViewHolder`, `onCreate`, `onMeasure`, `onLayout`
- **viewId**: XML 中定义的 RecyclerView 控件 ID

### SI$block# — 主线程卡顿
- **格式**: `SI$block#[stack]#[duration]`
- **示例**: `SI$block#worker.CpuBurnWorker$1#129ms`
- **来源**: BlockMonitor 检测到主线程卡顿（超过阈值）时自动采集
- **注意**: trace 中 beginSection+endSection 相邻导致 dur 约为 0，真实耗时从后缀 `#NNNms` 提取
- **堆栈**: 通过 logcat `SIBlock` 标签输出堆栈信息

### SI$inflate# — Layout 布局加载
- **格式**: `SI$inflate#[layout]#[parent]`
- **示例**: `SI$inflate#item_complex#recycler_view`
- **layout**: XML 布局文件名（不含扩展名）
- **parent**: 父容器 viewId

### SI$view# — View 遍历
- **格式**: `SI$view#[class].[method]`
- **示例**: `SI$view#HeavyDrawView.onDraw`
- **包含方法**: `onMeasure`, `onLayout`, `onDraw`
- **class**: 自定义 View 的简单类名

### SI$handler# — Handler 消息分发
- **格式**: `SI$handler#[msg_class]`
- **示例**: `SI$handler#ScrollRunnable`
- **msg_class**: Runnable/Message 的类名

### SI$Activity. — Activity 生命周期
- **格式**: `SI$Activity.[lifecycle]`
- **示例**: `SI$Activity.onResume`
- **生命周期**: `onCreate`, `onStart`, `onResume`, `onPause`, `onStop`, `onDestroy`

### SI$Fragment. — Fragment 生命周期
- **格式**: `SI$Fragment.[lifecycle]`
- **示例**: `SI$Fragment.onCreateView`
- **生命周期**: `onCreateView`, `onViewCreated`, `onResume`, `onPause`, `onDestroyView`

### SI$db# — 数据库操作
- **格式**: `SI$db#[operation]`
- **归入**: `collect_io_slices()` 的 IO 切片归因

### SI$net# — 网络操作
- **格式**: `SI$net#[operation]`
- **归入**: `collect_io_slices()` 的 IO 切片归因

### SI$img# — 图片加载
- **格式**: `SI$img#[operation]`
- **归入**: `collect_io_slices()` 的 IO 切片归因

### SI$touch# — 触摸输入事件
- **格式**: `SI$touch#[Activity]#[ACTION]`
- **示例**: `SI$touch#MainActivity#ACTION_MOVE`
- **注意**: 排除在线程状态分析之外

### SI$compose# — Jetpack Compose
- **格式**: `SI$compose#[Composable].[phase]`
- **phases**: composition, layout, drawing

## 解析规则

### 类名提取
- `SI$RV#viewId#Adapter.method` -> class_name = Adapter, method = method
- `SI$view#Class.method` -> class_name = Class, method = method
- `SI$block#pkg.Class$method$N#NNms` -> class_name = pkg.Class (含匿名内部类 $N)

### 匿名内部类
- 格式: `OuterClass$innerMethod$1`
- 提取外部类名用于 Glob 搜索: `OuterClass`
- `context_method` 字段记录外层方法名（实际代码所在）
- `_extract_method_from_anonymous()` 处理解析

### 搜索类型分类
- `java`: SI$RV#, SI$view#, SI$handler#, SI$block#, SI$Activity., SI$Fragment.
- `xml`: SI$inflate#
- `system`: is_custom=false 的系统切片

### 归因优先级
1. SI$ 用户代码切片优先归因
2. 系统级事件 (doFrame, Choreographer, Display HAL) 不归因
3. IO 切片 (SI$db#, SI$net#, SI$img#) 归入 IO 维度分析
