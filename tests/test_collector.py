"""Test PerfettoCollector with synthetic trace."""

import os
import tempfile
import pytest

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import Trace, TracePacket

from smartinspector.collector.perfetto import PerfettoCollector


def create_synthetic_trace(path: str) -> str:
    """Create a synthetic Perfetto trace with scheduling data."""
    trace = Trace()

    procs = [
        ("myapp", 1001, "surfaceflinger", 200),
        ("surfaceflinger", 200, "myapp", 1001),
        ("myapp", 1001, "kworker", 5),
        ("kworker", 5, "myapp", 1001),
        ("myapp", 1001, "system_server", 100),
        ("system_server", 100, "myapp", 1001),
    ]

    for i, (prev_comm, prev_pid, next_comm, next_pid) in enumerate(procs * 3):
        pkt = TracePacket()
        pkt.timestamp = (i * 10_000_000) + 1_000_000_000_000
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


@pytest.mark.skip(reason="Perfetto SQL MODE() WITHIN GROUP not supported by local SQLite")
def test_collector():
    tmp = os.path.join(tempfile.gettempdir(), "collector_test_trace.pb")
    create_synthetic_trace(tmp)
    size = os.path.getsize(tmp)
    print(f"Trace: {tmp} ({size} bytes)")

    collector = PerfettoCollector(tmp)

    # Test 1: Scheduling analysis
    print("\n=== Scheduling ===")
    sched = collector.collect_sched()
    for t in sched["hot_threads"][:5]:
        print(f"  {t['comm']}(tid={t['tid']}): {t['switches']} switches, {t['total_dur_ms']}ms")

    # Test 2: Thread list
    print("\n=== Threads ===")
    threads = collector.collect_threads()
    for t in threads:
        print(f"  tid={t['tid']}, name={t['name']}")

    # Test 3: Full summary
    print("\n=== Summary JSON ===")
    summary = collector.summarize()
    json_str = summary.to_json()
    print(json_str[:1500])
    print(f"\n... ({len(json_str)} bytes total)")

    collector.close()
    os.unlink(tmp)
    print("\nAll collector tests passed!")


if __name__ == "__main__":
    test_collector()
