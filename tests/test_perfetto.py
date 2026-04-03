"""Verify Perfetto trace_processor SQL queries with a synthetic trace."""

import tempfile
import os

from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
from perfetto.trace_processor.platform import PlatformDelegate
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import Trace, TracePacket

# Path to trace_processor_shell binary
SHELL_BIN = os.path.join(os.path.dirname(__file__), "..", "bin", "trace_processor_shell")


class FixedPlatformDelegate(PlatformDelegate):
    """Force IPv4 127.0.0.1 instead of localhost to avoid IPv6 issues on macOS."""

    def get_bind_addr(self, port: int):
        if port:
            return "127.0.0.1", port
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        port = s.getsockname()[1]
        s.close()
        return "127.0.0.1", port


# Monkey-patch the default platform delegate
import perfetto.trace_processor.api as _api
_api.PLATFORM_DELEGATE = FixedPlatformDelegate


def create_test_trace(path: str):
    """Create a synthetic Perfetto trace with scheduling data."""
    trace = Trace()

    events = []
    # Simulate 10 sched_switch events
    procs = [
        ("myapp", 1001, "myapp", 1001),
        ("myapp", 1001, "surfaceflinger", 200),
        ("surfaceflinger", 200, "myapp", 1001),
        ("myapp", 1001, "kworker", 5),
        ("kworker", 5, "myapp", 1001),
    ]

    for i, (prev_comm, prev_pid, next_comm, next_pid) in enumerate(procs * 2):
        pkt = TracePacket()
        pkt.timestamp = (i * 10_000_000) + 1_000_000_000_000  # 10ms apart
        bundle = pkt.ftrace_events
        bundle.cpu = 0
        evt = bundle.event.add()
        evt.timestamp = pkt.timestamp
        evt.pid = prev_pid
        sched = evt.sched_switch
        sched.prev_comm = prev_comm
        sched.prev_pid = prev_pid
        sched.prev_prio = 120
        sched.next_comm = next_comm
        sched.next_pid = next_pid
        sched.next_prio = 120
        trace.packet.append(pkt)

    with open(path, "wb") as f:
        f.write(trace.SerializeToString())

    return path


def test_sql_queries():
    """Run SQL queries against the synthetic trace."""
    tmp = os.path.join(tempfile.gettempdir(), "test_trace.pb")
    create_test_trace(tmp)
    size = os.path.getsize(tmp)
    print(f"Trace file: {tmp} ({size} bytes)")

    config = TraceProcessorConfig(
        bin_path=os.path.abspath(SHELL_BIN),
        load_timeout=10,
    )
    tp = TraceProcessor(trace=tmp, config=config)

    # Query 1: List all sched_switch events
    print("\n=== Sched Switch Events ===")
    result = tp.query("SELECT * FROM sched")
    rows = list(result)
    print(f"  Found {len(rows)} rows")
    for r in rows[:5]:
        print(f"  ts={r.ts}, cpu={r.cpu}, dur={r.dur}, end_state={r.end_state}")

    # Query 2: Check sched table columns
    print("\n=== Sched Table Schema ===")
    # Use the first row to discover column names
    if rows:
        cols = [attr for attr in dir(rows[0]) if not attr.startswith("_")]
        print(f"  Columns: {cols}")

    # Query 2b: Try getting columns from column_count table
    try:
        schema = tp.query("SELECT column_name, column_type FROM __sched_column")
        for s in schema:
            print(f"  col={s.column_name}, type={s.column_type}")
    except Exception:
        pass

    # Query 3: Check available tables
    print("\n=== Available Tables (sched) ===")
    tables = tp.query("SELECT name FROM perfetto_tables WHERE name LIKE '%sched%'")
    for t in tables:
        print(f"  {t.name}")

    # Query 3: Thread info
    print("\n=== Thread Info ===")
    threads = tp.query("SELECT * FROM thread")
    for t in threads:
        print(f"  tid={t.tid}, name={t.name}")

    print("\nAll SQL queries passed!")
    tp.close()
    os.unlink(tmp)


if __name__ == "__main__":
    test_sql_queries()
