"""Native messaging entry for independent chat store; backend host is untouched."""
import sys
from pathlib import Path
from .host import validate_origin, ExclusiveHost
from .security import WindowsProtection, check_path
from .chat_store import ChatStore
from .chat_protocol import ChatDispatcher
from .protocol import read_message, write_message


def main(base, legacy_loader):
    lock = store = dispatcher = None
    try:
        validate_origin(sys.argv[1:])
        import msvcrt, os
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        protection = WindowsProtection()
        root = check_path(Path(base) / 'runtime/chat-collector', Path(base)); root.mkdir(exist_ok=True)
        lock = ExclusiveHost(root, protection)
        store = ChatStore(root, protection); store.recover_interrupted()
        dispatcher = ChatDispatcher(store, legacy_loader)
        while True:
            request = read_message(sys.stdin.buffer)
            if request is None: break
            write_message(sys.stdout.buffer, dispatcher.call(request))
        return 0
    except Exception:
        return 1
    finally:
        if dispatcher: dispatcher.disconnect()
        if store: store.close()
        if lock: lock.close()
