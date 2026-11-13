"""Privileged per-process limiter daemon.

A local unprivileged client may manage only its own UID's rules/PIDs.
The networklimiter-admin group may additionally manage system-wide rules.
No client-supplied shell command or executable is ever run.
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

from .core import RuleError, choose_rule, kbps, limit, normalize_rule

LOG = logging.getLogger("networklimiter")
STATE = Path("/var/lib/networklimiter/state.json")
SOCKET = Path("/run/networklimiter/control.sock")
PIDNL = "/usr/bin/pidnl"
PID_RE = re.compile(r"^\s*(\d+)\s")
MAX_REQUEST = 32768
MAX_RULES = 100


def peer_has_admin_group(peer_pid, peer_uid):
    if peer_uid == 0:
        return True
    try:
        admin_gid = grp.getgrnam("networklimiter-admin").gr_gid
        text = Path(f"/proc/{peer_pid}/status").read_text()
        groups = next(line for line in text.splitlines() if line.startswith("Groups:"))
        return admin_gid in {int(gid) for gid in groups.split()[1:]}
    except (KeyError, OSError, StopIteration, ValueError):
        return False


class Backend:
    def __init__(self, binary=PIDNL):
        self.binary = binary

    def call(self, *args):
        result = subprocess.run(
            [self.binary, *map(str, args)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[:400] or "pidnl failed")
        return result.stdout

    def active_pids(self):
        output = self.call("list")
        found = set()
        for line in output.splitlines():
            match = PID_RE.match(line)
            if match:
                found.add(int(match.group(1)))
        return found

    def set_limit(self, pid, upload_kib, download_kib):
        self.call("limit", "set", pid, kbps(upload_kib), kbps(download_kib))

    def unset_limit(self, pid):
        self.call("limit", "unset", pid)


class Daemon:
    def __init__(self, state_path=STATE, backend=None):
        self.path = Path(state_path)
        self.backend = backend or Backend()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.users = {}
        self.system = []
        self.global_default = None
        self.paused_users = set()
        self.paused_system = False
        self.overrides = {}  # (pid, start_time) -> (uid, download_kib, upload_kib)
        self.applied = {}    # (pid, start_time) -> (download_kib, upload_kib)
        self.processes = []
        self.backend_error = None
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOG.error("Cannot load state (rules not applied): %s", exc)
            self.backend_error = f"Invalid state file: {exc}"
            return
        self.users = data.get("users", {})
        self.system = data.get("system", [])
        default = data.get("global_default")
        if isinstance(default, dict):
            try:
                down, up = limit(default.get("download_kib")), limit(default.get("upload_kib"))
                if down is not None or up is not None:
                    self.global_default = {"download_kib": down, "upload_kib": up}
            except RuleError:
                LOG.error("Ignoring malformed global default in state file")
        self.paused_users = set(data.get("paused_users", []))
        self.paused_system = bool(data.get("paused_system", False))

    def _save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = json.dumps({
            "users": self.users,
            "system": self.system,
            "global_default": self.global_default,
            "paused_users": sorted(self.paused_users),
            "paused_system": self.paused_system,
        }, indent=2)
        fd, temp = tempfile.mkstemp(prefix=".state-", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @staticmethod
    def _proc(pid):
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                uid = p.uids()
                return {
                    "pid": pid,
                    "uid": uid.real,
                    "privileged": len({uid.real, uid.effective, uid.saved}) != 1,
                    "name": p.name(),
                    "exe": p.exe(),
                    "start": p.create_time(),
                }
        except (psutil.Error, OSError, ValueError):
            return None

    def _desired(self, proc):
        key = (proc["pid"], proc["start"])
        paused_user = str(proc["uid"]) in self.paused_users
        override = self.overrides.get(key)
        if not paused_user and override is not None and (override[0] == proc["uid"] or override[0] == 0):
            return override[1:]
        own = [] if paused_user else self.users.get(str(proc["uid"]), [])
        sys_rules = [] if self.paused_system else self.system
        rule = choose_rule(proc, own, sys_rules)
        if rule:
            # Explicit unlimited rule is an exception to the global cap.
            down, up = rule["download_kib"], rule["upload_kib"]
            return (down, up) if down is not None or up is not None else None
        if self.global_default and not self.paused_system:
            return (self.global_default["download_kib"], self.global_default["upload_kib"])
        return None

    def scan(self):
        try:
            network_pids = self.backend.active_pids()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            with self.lock:
                self.backend_error = f"Backend unavailable: {exc}"
            LOG.warning("Backend scan failed: %s", exc)
            return

        with self.lock:
            known = {key[0] for key in self.applied}
        pids = network_pids | known
        catalog = [info for pid in sorted(pids) if (info := self._proc(pid)) and pid != os.getpid()]
        # Snapshot desired limits. The reentrant lock permits concurrent UI edits.
        with self.lock:
            wanted = [(p, self._desired(p), self.applied.get((p["pid"], p["start"]))) for p in catalog]
            self.backend_error = None

        for proc, desired, previous in wanted:
            pid, start = proc["pid"], proc["start"]
            key = (pid, start)
            if desired == previous:
                continue
            if desired is not None and pid not in network_pids:
                # pidnl requires an active network socket before attaching.
                continue
            # Do not act on a PID reused while running an external command.
            current = self._proc(pid)
            if current is None or current["start"] != start:
                continue
            try:
                if desired is None:
                    if previous is not None:
                        self.backend.unset_limit(pid)
                else:
                    self.backend.set_limit(pid, desired[1], desired[0])
                with self.lock:
                    if desired is None:
                        self.applied.pop(key, None)
                    else:
                        self.applied[key] = desired
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                with self.lock:
                    self.backend_error = f"PID {pid}: {exc}"
                LOG.warning("Unable to limit PID %d: %s", pid, exc)

        live = {(p["pid"], p["start"]) for p in catalog}
        with self.lock:
            for key in list(self.applied):
                if key not in live:
                    self.applied.pop(key)
            for key in list(self.overrides):
                if key not in live:
                    # An override may be waiting for its process to open a socket.
                    current = self._proc(key[0])
                    if current is None or current["start"] != key[1]:
                        self.overrides.pop(key)
            self.processes = []
            for proc in catalog:
                desired = self._desired(proc)
                key = (proc["pid"], proc["start"])
                proc["download_kib"] = desired[0] if desired else None
                proc["upload_kib"] = desired[1] if desired else None
                proc["limited"] = desired is not None and self.applied.get(key) == desired
                proc["override"] = key in self.overrides
                self.processes.append(proc)

    def scan_loop(self):
        while not self.stop.is_set():
            try:
                self.scan()
            except Exception:
                LOG.exception("Unexpected scan failure")
            self.wake.wait(timeout=3)
            self.wake.clear()

    def _authorize_process(self, pid, uid, admin):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RuleError("Invalid PID")
        proc = self._proc(pid)
        if proc is None or proc["pid"] == os.getpid():
            raise RuleError("Process not found or protected")
        if not admin and (proc["uid"] != uid or proc["privileged"]):
            raise PermissionError("Cannot control processes owned by other users")
        return proc

    def handle(self, request, uid, admin):
        if not isinstance(request, dict):
            raise RuleError("Expected a request object")
        op = request.get("op")
        with self.lock:
            if op == "status":
                visible = [
                    proc for proc in self.processes
                    if admin or (proc["uid"] == uid and not proc["privileged"])
                ]
                return {
                    "rules": self.users.get(str(uid), []) + self.system,
                    "processes": visible[:600],
                    "paused": str(uid) in self.paused_users,
                    "paused_system": self.paused_system,
                    "global_default": self.global_default,
                    "admin": admin,
                    "backend_error": self.backend_error,
                }

            if op == "set_global":
                if not admin:
                    raise PermissionError("Global default requires networklimiter-admin")
                enabled = request.get("enabled")
                if not isinstance(enabled, bool):
                    raise RuleError("Enabled must be a boolean")
                if enabled:
                    down = limit(request.get("download_kib"))
                    up = limit(request.get("upload_kib"))
                    if down is None and up is None:
                        raise RuleError("Global default must limit at least one direction")
                    self.global_default = {"download_kib": down, "upload_kib": up}
                else:
                    self.global_default = None
                self._save()
                self.wake.set()
                return {"global_default": self.global_default}

            if op == "upsert_rule":
                raw = request.get("rule")
                if not isinstance(raw, dict):
                    raise RuleError("Missing rule")
                scope = raw.get("scope", "user")
                if scope == "system" and not admin:
                    raise PermissionError("System rules require networklimiter-admin")
                if scope not in ("system", "user"):
                    raise RuleError("Invalid scope")
                rules = self.system if scope == "system" else self.users.setdefault(str(uid), [])
                original = None
                rule_id = raw.get("id")
                if rule_id is not None:
                    original = next((r for r in rules if r["id"] == rule_id), None)
                    if original is None:
                        raise RuleError("Rule not found in this scope")
                rule = normalize_rule(raw, owner=0 if scope == "system" else uid,
                                      scope=scope, id_override=rule_id)
                if original is None:
                    original = next((r for r in rules if r["mode"] == rule["mode"]
                                     and r["target"] == rule["target"]), None)
                if original is None:
                    if len(rules) >= MAX_RULES:
                        raise RuleError("Too many rules")
                    rules.append(rule)
                else:
                    rule["id"] = original["id"]
                    rules[rules.index(original)] = rule
                self._save()
                self.wake.set()
                return {"rule": rule}

            if op == "delete_rule":
                rule_id = request.get("id")
                for rules in [self.users.get(str(uid), [])] + ([self.system] if admin else []):
                    for rule in list(rules):
                        if rule["id"] == rule_id:
                            rules.remove(rule)
                            self._save()
                            self.wake.set()
                            return {"deleted": rule_id}
                raise RuleError("Rule not found or permission denied")

            if op == "set_pid":
                proc = self._authorize_process(request.get("pid"), uid, admin)
                down, up = limit(request.get("download_kib")), limit(request.get("upload_kib"))
                if down is None and up is None:
                    raise RuleError("At least one limit is required")
                self.overrides[(proc["pid"], proc["start"])] = (uid, down, up)
                self.wake.set()
                return {"pid": proc["pid"], "temporary": True}

            if op == "clear_pid":
                proc = self._authorize_process(request.get("pid"), uid, admin)
                key = (proc["pid"], proc["start"])
                old = self.overrides.get(key)
                if old is None or (old[0] != uid and not admin):
                    raise RuleError("No accessible PID override exists")
                del self.overrides[key]
                self.wake.set()
                return {"pid": proc["pid"], "cleared": True}

            if op == "pause":
                enabled = request.get("enabled")
                scope = request.get("scope", "user")
                if not isinstance(enabled, bool):
                    raise RuleError("Pause state must be a boolean")
                if scope == "system":
                    if not admin:
                        raise PermissionError("System pause requires networklimiter-admin")
                    self.paused_system = enabled
                elif scope == "user":
                    if enabled:
                        self.paused_users.add(str(uid))
                    else:
                        self.paused_users.discard(str(uid))
                else:
                    raise RuleError("Invalid scope")
                self._save()
                self.wake.set()
                return {"paused": enabled, "scope": scope}
        raise RuleError("Unknown operation")

    def serve_client(self, conn):
        with conn:
            try:
                conn.settimeout(4)
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                peer_pid, peer_uid, _ = struct.unpack("3i", creds)
                data = bytearray()
                while b"\n" not in data:
                    piece = conn.recv(4096)
                    if not piece:
                        raise RuleError("Incomplete request")
                    data.extend(piece)
                    if len(data) > MAX_REQUEST:
                        raise RuleError("Request too large")
                request = json.loads(bytes(data).split(b"\n", 1)[0])
                reply = {"ok": True, "result": self.handle(
                    request, peer_uid, peer_has_admin_group(peer_pid, peer_uid),
                )}
            except (RuleError, PermissionError, OSError, ValueError, json.JSONDecodeError) as exc:
                reply = {"ok": False, "error": str(exc)}
            except Exception:
                LOG.exception("Unhandled client error")
                reply = {"ok": False, "error": "Internal service error"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except OSError:
                pass

    def cleanup(self):
        """Release only this daemon's managed PIDs on graceful exit."""
        with self.lock:
            active = list(self.applied)
        for pid, start in active:
            proc = self._proc(pid)
            if proc is None or proc["start"] != start:
                continue
            try:
                self.backend.unset_limit(pid)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                LOG.warning("Unable to remove limit for PID %d at shutdown: %s", pid, exc)

    def serve(self, socket_path=SOCKET):
        if os.geteuid() != 0:
            raise PermissionError("Daemon must run as root (systemd service)")
        path = Path(socket_path)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if path.exists():
            raise RuntimeError(f"Socket already exists: {path} (check for a running service)")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            os.chmod(path, 0o666)  # Authorize by SO_PEERCRED, not filesystem mode.
            server.listen(16)
            server.settimeout(1)
            thread = threading.Thread(target=self.scan_loop, daemon=True)
            thread.start()
            try:
                while not self.stop.is_set():
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()
            finally:
                self.stop.set()
                self.wake.set()
                thread.join(timeout=5)
                self.cleanup()
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="NetworkLimiter privileged daemon")
    parser.add_argument("--verbose", action="store_true")
    options = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if options.verbose else logging.INFO)
    daemon = Daemon()
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop.set())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop.set())
    daemon.serve()


if __name__ == "__main__":
    main()
