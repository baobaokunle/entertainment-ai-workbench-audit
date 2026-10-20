# AUDIT-ONLY: sanitized copy; not a production runtime or authorization.
"""Fixed-origin Chrome native host. No web listener, arbitrary path or credential API."""
from pathlib import Path
import sys
from .security import CollectorError, WindowsProtection, check_path, private_root
from .protocol import Dispatcher, read_message, write_message
from .store import Store

EXTENSION_ID = "a" * 32  # audit-only synthetic deployment ID
ORIGIN = f"chrome-extension://{EXTENSION_ID}/"


def validate_origin(argv):
    if not 1 <= len(argv) <= 2 or argv[0] != ORIGIN:
        raise CollectorError("EXTENSION_ORIGIN_REJECTED")
    if len(argv) == 2 and (not argv[1].startswith("--parent-window=") or not argv[1][16:].isdigit()):
        raise CollectorError("HOST_ARGUMENT_REJECTED")


class ExclusiveHost:
    def __init__(self, base, protection):
        import msvcrt
        root = private_root(base, protection)
        path = check_path(root / "host.lock", base)
        if path.exists():
            protection.verify_acl(path)
        self.handle = path.open("a+b")
        try:
            protection.verify_acl(path)
            self.handle.seek(0)
            # Windows byte-range file lock applies across console/RDP sessions.
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            self.handle.close()
            self.handle = None
            raise CollectorError("HOST_ALREADY_OPEN") from None

    def close(self):
        if self.handle:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


def serve(base, argv, input_stream, output_stream):
    # Origin rejected BEFORE opening or decrypting any store.
    validate_origin(argv)
    protection = WindowsProtection()
    lock = ExclusiveHost(base, protection)
    store = dispatcher = None
    try:
        app = check_path(base / "runtime" / "collector-app", base)
        protection.verify_acl(app, protected=True)
        for entry in app.rglob("*"):
            check_path(entry, base)
            protection.verify_acl(entry)
        store = Store(base, protection)
        store.recover_interrupted()
        dispatcher = Dispatcher(store)
        while True:
            request = read_message(input_stream)
            if request is None:
                break
            write_message(output_stream, dispatcher.call(request))
    finally:
        if dispatcher:
            dispatcher.disconnect()
        if store:
            store.close()
        lock.close()


def main(base):
    import msvcrt, os
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        serve(Path(base), sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        # Never log native/source payloads, paths, exception repr or stack traces.
        return 1
    return 0
