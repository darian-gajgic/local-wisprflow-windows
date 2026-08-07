#!/usr/bin/env python3
"""Loopback IPC for local-wisprflow on Windows.

The Linux original used an AF_UNIX socket in $XDG_RUNTIME_DIR, chmod 0600 — filesystem
permissions were the access control. CPython on Windows does not expose ``socket.AF_UNIX``,
so this port uses a TCP socket on **127.0.0.1** instead, with two safeguards that replace
what the socket file mode used to give us:

* the port is **ephemeral** (bind to port 0) — nothing to collide with, and not guessable;
* every request must carry a **random 128-bit token** that the daemon writes to
  ``%LOCALAPPDATA%\\wisprflow\\daemon.json``, a per-user directory other users cannot read.

Without the token any other process on the machine that guessed the port could trigger
dictation and have text typed into the foreground window, so the check is not decorative.

Wire format is one line, ``<token> <command>\\n``; the reply is a plain UTF-8 string.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading

from wf_paths import ensure_dirs, runtime_path

HOST = "127.0.0.1"
TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def read_runtime() -> dict | None:
    try:
        return json.loads(runtime_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


class NotRunning(RuntimeError):
    pass


def send(cmd: str, timeout: float = TIMEOUT) -> str:
    """Send one command to the daemon and return its reply.

    Raises NotRunning when there is no daemon (no runtime file, or nothing listening).
    """
    rt = read_runtime()
    if not rt or not rt.get("port"):
        raise NotRunning("daemon is not running (no runtime file)")
    try:
        with socket.create_connection((HOST, int(rt["port"])), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"{rt.get('token', '')} {cmd}\n".encode("utf-8"))
            chunks = []
            while True:
                b = s.recv(4096)
                if not b:
                    break
                chunks.append(b)
                if b.endswith(b"\n"):
                    break
            return b"".join(chunks).decode("utf-8", "replace").strip()
    except (ConnectionRefusedError, OSError) as e:
        raise NotRunning(f"daemon is not running ({e.__class__.__name__})") from e


def try_send(cmd: str, timeout: float = 2.0) -> str:
    """send() that returns '' instead of raising — for best-effort callers (overlay, tray)."""
    try:
        return send(cmd, timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""


def daemon_alive(timeout: float = 2.0) -> bool:
    return try_send("ping", timeout=timeout).startswith("pong")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class Server:
    """Accept-loop for daemon commands. `handler(cmd) -> reply` runs on the accept thread."""

    def __init__(self, handler, log=print):
        self.handler = handler
        self.log = log
        self.token = secrets.token_hex(16)
        self.sock: socket.socket | None = None
        self.port = 0
        self._stop = threading.Event()

    def bind(self) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # deliberately NOT SO_REUSEADDR: on Windows that flag allows two sockets to bind the
        # same port, which would silently split clients between a stale and a live daemon.
        srv.bind((HOST, 0))
        srv.listen(8)
        self.sock, self.port = srv, srv.getsockname()[1]
        self.publish()
        return self.port

    def publish(self) -> None:
        ensure_dirs()
        p = runtime_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}),
                       encoding="utf-8")
        os.replace(tmp, p)   # atomic: a client never reads a half-written runtime file

    def serve_forever(self) -> None:
        assert self.sock is not None
        self.log(f"listening on {HOST}:{self.port}")
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                if self._stop.is_set():
                    break
                continue
            with conn:
                try:
                    conn.settimeout(TIMEOUT)
                    data = conn.recv(8192).decode("utf-8", "replace").strip()
                    token, _, cmd = data.partition(" ")
                    if not secrets.compare_digest(token, self.token):
                        conn.sendall(b"unauthorized\n")
                        self.log("rejected a command with a bad token")
                        continue
                    conn.sendall((self.handler(cmd.strip()) + "\n").encode("utf-8"))
                except Exception as e:  # noqa: BLE001
                    self.log(f"conn error: {e!r}")

    def close(self) -> None:
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        try:
            runtime_path().unlink(missing_ok=True)
        except OSError:
            pass
