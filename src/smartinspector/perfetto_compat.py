"""Perfetto trace_processor compatibility fixes for macOS."""

import socket
from perfetto.trace_processor.platform import PlatformDelegate


class IPv4PlatformDelegate(PlatformDelegate):
    """Force IPv4 127.0.0.1 instead of localhost to avoid IPv6 issues on macOS."""

    def get_bind_addr(self, port: int):
        if port:
            return "127.0.0.1", port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        port = s.getsockname()[1]
        s.close()
        return "127.0.0.1", port


def patch():
    """Apply macOS IPv4 fix to perfetto trace_processor."""
    import perfetto.trace_processor.api as _api
    _api.PLATFORM_DELEGATE = IPv4PlatformDelegate
