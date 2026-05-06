"""Shared helper functions for collector sub-modules.

Extracted from perfetto.py to avoid circular imports between
the main module and mixin modules.
"""


def _parse_siblock_msg(msg: str) -> list[str]:
    """Parse SIBlock logcat message into stack trace frames.

    Input format: "MsgClass|250ms|at com.example.Foo.run(Foo.java:123)|at com.example.Bar.doX(Bar.java:45)"
    Output: ["at com.example.Foo.run(Foo.java:123)", "at com.example.Bar.doX(Bar.java:45)"]
    """
    if not msg:
        return []

    parts = msg.split("|")
    frames = []
    for part in parts[2:]:
        part = part.strip()
        if part and part.startswith("at "):
            frames.append(part)
    return frames


def _map_state_label(raw_state: str) -> str:
    """Map kernel thread state to human-readable label."""
    mapping = {
        "Running": "Running",
        "R": "Running",
        "R+": "Running",
        "S": "Sleeping",
        "S+": "Sleeping",
        "D": "DiskSleep",
        "D+": "DiskSleep",
        "T": "Stopped",
        "t": "Traced",
        "X": "Dead",
        "Z": "Zombie",
    }
    return mapping.get(raw_state, raw_state)
