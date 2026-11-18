"""NetworkLimiter privileged daemon: ONE shared setting, a separate cap per PID.

Every network-owning process receives the same configured maximum download rate.
Only networklimiter-admin members or root may change the global setting.
No per-process rules or PID overrides are accepted by the control protocol.
"""
from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

import psutil

from .core import DEFAULT_KIB, RateError, to_pidnl_kbps, validate_rate

LOG = logging.getLogger("networklimiter")
STATE = Path("/var/lib/networklimiter/state.json")
SOCKET = Path("/run/networklimiter/control.sock")
PIDNL = "/usr/bin/pidnl"
PID_RE = re.compile(r"^\s*(\d+)\s")
MAX_REQUEST = 4096


def peer_is_admin(peer_pid, peer_uid):
    """Check supplementary groups of the actual Unix-socket peer."""
    if peer_uid == 0:
        return True
    try:
        admin_gid = grp.getgrnam("networklimiter-admin").gr_gid
        status = Path(f"/proc/{peer_pid}/status").read_text(encoding="utf-8")
        groups = next(line for line in status.splitlines() if line.startswith("Groups:"))
        return admin_gid in {int(group) for group in groups.split()[1:]}
    except (KeyError, ValueError, OSError, StopIteration):
        return False


class Backend:
    """Fixed executable + fixed argument structure; never execute client data."""

    def __init__(self, binary=PIDNL):
        self.binary = binary

    def call(self, *args):
        result = subprocess.run(
            [self.binary, *map(str, args)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if result.returncode:
            raise RuntimeError(
                (result.stderr or result.stdout).strip()[:400] or "pidnl command failed"
            )
        return result.stdout

    def active_pids(self):
        pids = set()
        for line in self.call("list").splitlines():
            match = PID_RE.match(line)
            if match:
                pids.add(int(match.group(1)))
        return pids

    def set_limit(self, pid, download_kib):
        # Upload is deliberately unlimited. Every PID has its OWN download cap.
        self.call("limit", "set", pid, "-1", to_pidnl_kbps(download_kib))

    def unset_limit(self, pid):
        self.call("limit", "unset", pid)


class Daemon:
    def __init__(self, state_path=STATE, backend=None):
        self.path = Path(state_path)
        self.backend = backend or Backend()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.client_slots = threading.BoundedSemaphore(16)
        self.enabled = False
        self.download_kib = DEFAULT_KIB
        # (pid, process_create_time) -> (download_kib, upload_kib).
        # Track old-schema limits too so upgrades remove obsolete per-app rules.
        self.applied = {}
        self.active_count = 0
        self.limited_count = 0
        self.backend_error = None
        self._load()

    @staticmethod
    def _optional_rate(value):
        return None if value is None else validate_rate(value)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("State must be a JSON object")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOG.error("Cannot load settings: %s", exc)
            self.backend_error = f"Could not load settings: {exc}"
            return

        # v2: only a toggle and one speed. Old per-app rules are ignored.
        if data.get("version") == 2:
            rate = data.get("download_kib", DEFAULT_KIB)
            enabled = data.get("enabled", False)
        else:
            # Upgrade v1's global cap but intentionally discard all individual rules.
            legacy = data.get("global_default")
            rate = legacy.get("download_kib") if isinstance(legacy, dict) else DEFAULT_KIB
            enabled = bool(
                isinstance(legacy, dict)
                and rate is not None
                and not data.get("paused_system", False)
            )
            if rate is None:
                rate = DEFAULT_KIB

        try:
            self.download_kib = validate_rate(rate)
            self.enabled = enabled is True
        except RateError as exc:
            self.enabled = False
            LOG.error("Invalid saved speed, disabling limiter: %s", exc)

        for entry in data.get("applied", []):
            if not isinstance(entry, dict):
                continue
            try:
                pid, start = entry["pid"], entry["start"]
                if type(pid) is not int or pid < 1:
                    continue
                if type(start) not in (int, float) or not 0 < start < float("inf"):
                    continue
                down = self._optional_rate(entry.get("download_kib"))
                up = self._optional_rate(entry.get("upload_kib"))
                if down is not None or up is not None:
                    self.applied[(pid, float(start))] = (down, up)
            except (ValueError, TypeError, KeyError, RateError):
                LOG.warning("Discarded invalid PID journal entry")

    def _save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        contents = json.dumps({
            "version": 2,
            "enabled": self.enabled,
            "download_kib": self.download_kib,
            "applied": [
                {"pid": pid, "start": start,
                 "download_kib": rates[0], "upload_kib": rates[1]}
                for (pid, start), rates in sorted(self.applied.items())
            ],
        }, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _process_start(pid):
        try:
            return psutil.Process(pid).create_time()
        except (psutil.Error, OSError, ValueError):
            return None

    def scan(self):
        try:
            network_pids = self.backend.active_pids()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            with self.lock:
                self.backend_error = f"Network backend unavailable: {exc}"
            LOG.warning("Could not enumerate network processes: %s", exc)
            return

        with self.lock:
            prior = dict(self.applied)
            enabled = self.enabled
            download_kib = self.download_kib

        known_pids = {pid for pid, _ in prior}
        observed = network_pids | known_pids
        live = {}
        for pid in observed:
            if pid == os.getpid():
                continue
            start = self._process_start(pid)
            if start is not None:
                live[(pid, start)] = pid

        desired = (download_kib, None) if enabled else None
        error = None
        for (pid, start), _ in sorted(live.items()):
            if self.stop.is_set():
                break
            key = (pid, start)
            previous = prior.get(key)
            if desired == previous:
                continue
            if desired is not None and pid not in network_pids:
                # pidnl needs a network-active PID to attach initially.
                continue
            if self._process_start(pid) != start:
                continue  # PID reuse or exit: don't touch an unrelated process.
            try:
                if desired is None:
                    if previous is not None:
                        self.backend.unset_limit(pid)
                else:
                    self.backend.set_limit(pid, download_kib)
                with self.lock:
                    if desired is None:
                        self.applied.pop(key, None)
                    else:
                        self.applied[key] = desired
                    self._save()
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                error = f"PID {pid}: {exc}"
                LOG.warning("Unable to update PID %s: %s", pid, exc)

        with self.lock:
            stale = [key for key in self.applied if key not in live]
            for key in stale:
                self.applied.pop(key, None)
            if stale:
                self._save()
            self.active_count = len(network_pids)
            current = (self.download_kib, None) if self.enabled else None
            self.limited_count = sum(
                1 for key in live if current is not None and self.applied.get(key) == current
            )
            self.backend_error = error

    def scan_loop(self):
        while not self.stop.is_set():
            try:
                self.scan()
            except Exception:
                LOG.exception("Unexpected background scan failure")
            self.wake.wait(timeout=3)
            self.wake.clear()

    def handle(self, request, *, admin):
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        operation = request.get("op")
        with self.lock:
            if operation == "status":
                return {
                    "enabled": self.enabled,
                    "download_kib": self.download_kib,
                    "active_count": self.active_count,
                    "limited_count": self.limited_count,
                    "admin": admin,
                    "backend_error": self.backend_error,
                }
            if operation == "set_global":
                if not admin:
                    raise PermissionError(
                        "Change settings requires networklimiter-admin; sign out and back in"
                    )
                enabled = request.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Enabled must be true or false")
                rate = validate_rate(request.get("download_kib"))
                self.enabled = enabled
                self.download_kib = rate
                self._save()
                self.wake.set()
                return {"enabled": enabled, "download_kib": rate}
            # The old per-PID, per-application and pause endpoints are removed.
            raise ValueError("Unsupported operation: only status and set_global are available")

    def serve_client(self, conn):
        with conn:
            try:
                conn.settimeout(4)
                credentials = conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                peer_pid, peer_uid, _ = struct.unpack("3i", credentials)
                data = bytearray()
                while b"\n" not in data:
                    block = conn.recv(1024)
                    if not block:
                        raise ValueError("Request must be newline-terminated")
                    data.extend(block)
                    if len(data) > MAX_REQUEST:
                        raise ValueError("Request too large")
                request = json.loads(bytes(data).split(b"\n", 1)[0])
                reply = {"ok": True, "result": self.handle(
                    request, admin=peer_is_admin(peer_pid, peer_uid)
                )}
            except (ValueError, TypeError, PermissionError, OSError) as exc:
                reply = {"ok": False, "error": str(exc)}
            except Exception:
                LOG.exception("Unexpected control socket error")
                reply = {"ok": False, "error": "Internal service error"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except OSError:
                pass

    def _client_worker(self, conn):
        try:
            self.serve_client(conn)
        finally:
            self.client_slots.release()

    def cleanup(self):
        """Release only this service's known limits when stopping gracefully."""
        with self.lock:
            existing = list(self.applied)
        for pid, start in existing:
            if self._process_start(pid) != start:
                continue
            try:
                self.backend.unset_limit(pid)
                with self.lock:
                    self.applied.pop((pid, start), None)
                    self._save()
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                LOG.warning("Failed to remove limit from PID %d: %s", pid, exc)

    def serve(self, socket_path=SOCKET):
        if os.geteuid() != 0:
            raise PermissionError("Daemon must be started by systemd as root")
        path = Path(socket_path)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if path.exists():
            raise RuntimeError(f"Socket already exists: {path}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            # Authorization is checked by SO_PEERCRED, not socket permissions.
            os.chmod(path, 0o666)
            server.listen(16)
            server.settimeout(1)
            worker = threading.Thread(target=self.scan_loop, daemon=True)
            worker.start()
            try:
                while not self.stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    if not self.client_slots.acquire(blocking=False):
                        conn.close()
                        continue
                    try:
                        threading.Thread(
                            target=self._client_worker, args=(conn,), daemon=True,
                        ).start()
                    except RuntimeError:
                        self.client_slots.release()
                        conn.close()
                        raise
            finally:
                self.stop.set()
                self.wake.set()
                worker.join()
                self.cleanup()
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="NetworkLimiter background service")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    service = Daemon()
    signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    signal.signal(signal.SIGINT, lambda *_: service.stop.set())
    service.serve()


if __name__ == "__main__":
    main()
