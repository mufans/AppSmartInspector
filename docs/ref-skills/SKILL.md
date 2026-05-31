---
name: perfetto-trace-analysis
description: Analyze Android performance traces using Perfetto/TraceProcessor. Supports Systrace, Perfetto traces, and Android Studio CPU/Memory profiles. Provides SQL-based trace analysis, flamegraph generation, and performance bottleneck identification.
---

# Perfetto Trace Analysis Skill

## Overview
This skill enables deep analysis of Android performance traces using the Perfetto open-source trace analysis framework. It supports:
- **Perfetto traces** (.perfetto-trace, .ctrace)
- **Systrace** (.html, .json)
- **Android Studio CPU profiles** (.trace, .cpuprofile)
- **Ftrace / atrace** kernel traces

## Core Concepts

### TraceProcessor SQL Interface
Perfetto's TraceProcessor allows SQL queries against trace data:

```sql
-- Top CPU consumers
SELECT process.name, thread.name, sum(dur)/1e9 as cpu_sec
FROM sched JOIN thread USING (utid) JOIN process USING (upid)
GROUP BY upid ORDER BY cpu_sec DESC LIMIT 20;

-- UI thread jank (frame misses)
SELECT slice.name, ts, dur/1e6 as dur_ms
FROM slice JOIN track ON slice.track_id = track.id
WHERE track.name GLOB '*ui*'
AND dur > 16e6
ORDER BY dur DESC;

-- Lock contention
SELECT slice.name, count(*) as cnt, sum(dur)/1e6 as total_ms
FROM slice
WHERE slice.name LIKE '%lock%'
GROUP BY slice.name ORDER BY total_ms DESC;

-- Memory allocation hotspots
SELECT slice.name, count(*) as allocs, sum(dur)/1e6 as total_ms
FROM slice
WHERE slice.name LIKE '%alloc%' OR slice.name LIKE '%malloc%'
GROUP BY slice.name ORDER BY total_ms DESC;
```

### Key Tables
| Table | Description |
|-------|-------------|
| `sched` | CPU scheduling events |
| `slice` | Trace events (begin/end) |
| `counter` | Counter values (CPU freq, memory) |
| `thread_track` | Thread-level tracks |
| `process_track` | Process-level tracks |
| `actual_frame` | Frame timing |
| `memory_snapshot` | Heap snapshots |
| `heap_graph` | Object graphs |

### Analysis Dimensions

#### 1. CPU Performance
- Per-thread CPU usage
- Scheduling latency (time from runnable to running)
- CPU frequency analysis
- Wakeup analysis

#### 2. UI Performance (Jank Analysis)
- Frame timeline (expected vs actual)
- Choreographer frame drops
- Inflate/layout/measure/draw breakdown
- RecyclerView bind times

#### 3. Memory Analysis
- Java heap growth patterns
- Native allocation patterns
- GC event frequency and duration
- OOM risk assessment
- Memory leak indicators

#### 4. I/O Performance
- File read/write latency
- Disk I/O patterns
- SharedPreferences commit times
- Database query times

#### 5. Network Performance
- Request latency breakdown
- Connection setup time
- TLS handshake duration
- Data transfer rates

#### 6. Power/Battery
- Wakelock analysis
- Alarm frequency
- GPS usage patterns
- Network radio state transitions

## Tools & Integration

### trace_processor_shell
```bash
# Interactive SQL queries
trace_processor_shell trace.perfetto-trace

# Batch query
trace_processor_shell -q query.sql trace.perfetto-trace

# JSON output
trace_processor_shell -q query.sql --json trace.perfetto-trace
```

### Perfetto UI (ui.perfetto.dev)
- Upload traces for visual analysis
- Flamegraph views
- Counter tracks
- Slice analysis

### Integration with AppSmartInspector
- Parse trace files and extract key metrics
- Generate structured analysis reports
- Identify top N bottlenecks
- Compare traces before/after optimization
- Automated regression detection

## Analysis Workflow
1. **Collect trace** - `adb shell perfetto -c - -o /data/misc/perfetto-traces/trace.pb`
2. **Pull trace** - `adb pull /data/misc/perfetto-traces/trace.pb`
3. **Load into TraceProcessor** - Run SQL queries
4. **Identify bottlenecks** - Sort by impact (duration × frequency)
5. **Generate report** - Structured markdown with metrics and recommendations
6. **Compare baselines** - Track regressions across builds

## Spec Document Template
When generating analysis specs, include:
1. Executive Summary (top findings)
2. Methodology (trace collection config)
3. Detailed Analysis (per dimension)
4. SQL Queries Used
5. Metrics & Thresholds
6. Recommendations (priority-sorted)
7. Baseline for Regression Detection
