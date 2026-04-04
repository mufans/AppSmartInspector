"""CLI entry: main() REPL loop."""

from smartinspector.commands import handle_slash_command
from smartinspector.graph.builder import create_graph
from smartinspector.graph.streaming import _stream_run


def main():
    """Run the interactive chat loop."""
    import argparse
    import subprocess
    import pathlib

    from smartinspector.config import get_source_dir, set_source_dir
    from smartinspector.ws.server import SIServer

    parser = argparse.ArgumentParser(description="SmartInspector CLI")
    parser.add_argument("--source-dir", default="", help="Source code directory for attribution search")
    args, _ = parser.parse_known_args()

    if args.source_dir:
        set_source_dir(args.source_dir)

    print("SmartInspector v0.5.0")
    if args.source_dir:
        print(f"Source dir: {get_source_dir()}")
    else:
        print(f"Source dir: {get_source_dir()} (use --source-dir or /config source_dir <path> to change)")
    print("Type /help for commands, 'quit' or Ctrl+C to exit\n")

    # Auto-start WS server + adb reverse so app can connect on launch
    server = SIServer.get(port=9876)
    server.start()
    try:
        subprocess.run(
            ["adb", "reverse", "tcp:9876", "tcp:9876"],
            capture_output=True, text=True, timeout=5,
        )
        print("  WS server ready on :9876, adb reverse set")
    except Exception as e:
        print(f"  WS server ready on :9876 (adb reverse failed: {e})")
    print()

    graph = create_graph()
    state = {
        "messages": [],
        "perf_summary": "",
        "perf_analysis": "",
        "attribution_data": "",
        "attribution_result": "",
        "_trace_path": "",
    }

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    session = PromptSession(history=FileHistory(str(pathlib.Path.home() / ".smartinspector_history")))

    while True:
        try:
            user_input = session.prompt("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("bye!")
            break

        # Slash commands bypass the LLM graph
        if user_input.startswith("/"):
            state = handle_slash_command(user_input, state)
            continue

        state["messages"] = state["messages"] + [
            {"role": "user", "content": user_input}
        ]

        state = _stream_run(graph, state)
