"""Small, bounded JSON-over-Unix-socket client. No root rights required."""
from __future__ import annotations

import json
import socket

SOCKET = "/run/networklimiter/control.sock"


class ClientError(RuntimeError):
    pass


def request(op, *, socket_path=SOCKET, timeout=2.0, **kwargs):
    payload = (json.dumps({"op": op, **kwargs}) + "\n").encode("utf-8")
    if len(payload) > 32768:
        raise ClientError("Request too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(payload)
            chunks = bytearray()
            while b"\n" not in chunks:
                part = sock.recv(65536)
                if not part:
                    raise ClientError("Daemon disconnected")
                chunks.extend(part)
                if len(chunks) > 2_000_000:
                    raise ClientError("Response too large")
        response = json.loads(chunks.split(b"\n", 1)[0])
    except (OSError, ValueError) as exc:
        raise ClientError(str(exc)) from exc
    if not response.get("ok"):
        raise ClientError(response.get("error", "Request rejected"))
    return response["result"]
