# Java/Kotlin/XML 源码搜索策略

## 核心搜索流程

### 三步搜索法: Glob -> Grep -> Read

1. **Glob 定位文件**: 按类名查找文件路径
2. **Grep 获取行号**: 在文件中搜索方法签名
3. **Read 精准读取**: 读取方法体（limit=40 行）

## Java 类搜索

### 标准搜索
```
glob(pattern="**/{ClassName}.java")
# 未找到时尝试 .kt
glob(pattern="**/{ClassName}.kt")
# 仍未找到 -> grep 搜索是否是内部类
grep(pattern="{ClassName}", path=".")
```

### 内部类搜索
- 匿名内部类 `$N` (如 `CpuBurnWorker$1`): Glob 搜索外部类 `CpuBurnWorker`
- 在结果中 grep 搜索内部类定义或 `context_method`
- Kotlin object companion: 搜索外部类名

### 父类方法查找
- 方法在当前类未找到时，搜索 `extends` 或 ` : ` (Kotlin) 找父类
- 父类是系统类 (android.*/androidx.*/java.*/appcompat.*) -> 标记 system_class
- 父类是项目类 -> Glob 搜索父类文件并在父类中 Grep 方法
- Kotlin 继承语法: `class Foo : Bar()`，grep `class.*:.*\(` 可帮助找到父类

## Kotlin 搜索

### 特殊规则
- 伴生对象方法: 搜索包含 `companion object` 的外部类
- 扩展函数: grep `fun ClassName.` 模式
- 顶层函数: 搜索文件名匹配函数所在文件（Kotlin 不强制类名=文件名）
- 属性委托: 搜索 `by lazy`, `by viewModels` 等

## Layout XML 搜索

### 标准搜索
```
glob(pattern="**/{layout_name}.xml")
# 找到后 Read 完整文件
read(file_path="{path}", offset=1, limit=200)
```

### 分析要点
- 嵌套层级深度
- include 标签数量
- 重度组件 (WebView, VideoView, 大图 ImageView)
- ConstraintLayout vs LinearLayout 层级对比

## 搜索优化

### 调用链上下文利用
- 调用链格式: `[帧渲染] -> RV#recycler_orders#OrderAdapter.onCreateViewHolder`
- 当 Glob 搜索到多个同名文件时，优先选择与调用链上下文匹配的文件
- 当 Grep 搜索到方法在多个位置出现时，优先选择与调用链描述一致的调用点

### 堆栈采样信息利用
- BlockMonitor 堆栈格式: `at com.example.DetailFragment$1.run(DetailFragment.java:123)`
- 直接用 `Read(offset=行号, limit=40)` 读取该方法
- 匿名内部类搜索外部类文件

### 缓存策略
- 同一类文件只需 Glob 一次
- 同一文件只需 Read 一次（不同 offset 除外）
- 多个方法属于同一类时，Glob 一次后分别 Grep + Read

## 约束

- 每个方法最多读取 40 行
- read() 的 file_path 参数不能为空字符串
- 不要搜索 VH、ViewHolder、Holder 等泛化内部类名
- 不要搜索 dispatchLayoutStep、onLayoutChildren 等 RecyclerView 框架方法
- 父类是系统类时直接标记 system_class，不要继续搜索
