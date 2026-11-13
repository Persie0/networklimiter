import tempfile
import unittest
from pathlib import Path

from networklimiter.core import normalize_rule
from networklimiter.daemon import Daemon


class FakeBackend:
    def active_pids(self):
        return set()

    def set_limit(self, pid, upload, download):
        raise AssertionError("No kernel traffic expected in unit tests")

    def unset_limit(self, pid):
        raise AssertionError("No kernel traffic expected in unit tests")


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.daemon = Daemon(state_path=self.path, backend=FakeBackend())
        self.proc = {
            "pid": 1234, "start": 50.0, "uid": 1000, "privileged": False,
            "name": "curl", "exe": "/usr/bin/curl",
        }
        self.admin = True

    def tearDown(self):
        self.temp.cleanup()

    def rule(self, name="curl", down=100, owner=1000, scope="user"):
        return normalize_rule(
            {"mode": "name", "target": name, "download_kib": down},
            owner=owner, scope=scope,
        )

    def test_default_is_off(self):
        self.assertIsNone(self.daemon._desired(self.proc))

    def test_global_default_applies_individually(self):
        result = self.daemon.handle(
            {"op": "set_global", "enabled": True,
             "download_kib": 100, "upload_kib": None},
            uid=1000, admin=True,
        )
        self.assertEqual(result["global_default"]["download_kib"], 100)
        self.assertEqual(self.daemon._desired(self.proc), (100, None))
        second = {**self.proc, "pid": 1235, "start": 50.5}
        self.assertEqual(self.daemon._desired(second), (100, None))

    def test_global_requires_admin(self):
        with self.assertRaises(PermissionError):
            self.daemon.handle({"op": "set_global", "enabled": True,
                                "download_kib": 100}, 1000, False)

    def test_rule_precedence_and_exception(self):
        self.daemon.global_default = {"download_kib": 100, "upload_kib": None}
        self.daemon.system = [self.rule("curl", 50, 0, "system")]
        self.assertEqual(self.daemon._desired(self.proc), (50, None))
        self.daemon.users["1000"] = [self.rule("curl", 10)]
        self.assertEqual(self.daemon._desired(self.proc), (10, None))
        self.daemon.users["1000"] = [self.rule("curl", None)]
        self.assertIsNone(self.daemon._desired(self.proc))

    def test_pause_user_does_not_override_global_system_default(self):
        self.daemon.global_default = {"download_kib": 100, "upload_kib": None}
        self.daemon.users["1000"] = [self.rule("curl", 10)]
        self.daemon.paused_users.add("1000")
        self.assertEqual(self.daemon._desired(self.proc), (100, None))
        self.daemon.paused_system = True
        self.assertIsNone(self.daemon._desired(self.proc))

    def test_unprivileged_cannot_replace_system_rules(self):
        self.daemon.system = [self.rule("curl", 5, 0, "system")]
        with self.assertRaises(PermissionError):
            self.daemon.handle({"op": "upsert_rule", "rule": {
                "scope": "system", "mode": "name",
                "target": "curl", "download_kib": 1,
            }}, uid=1000, admin=False)

    def test_limit_journal_recovers_after_abrupt_restart(self):
        class TrackingBackend:
            def __init__(self):
                self.set_calls = []
                self.unset_calls = []

            def active_pids(self):
                return {1234}

            def set_limit(self, pid, upload, download):
                self.set_calls.append((pid, upload, download))

            def unset_limit(self, pid):
                self.unset_calls.append(pid)

        backend = TrackingBackend()
        self.daemon.backend = backend
        self.daemon._proc = lambda pid: dict(self.proc) if pid == 1234 else None
        self.daemon.users["1000"] = [self.rule(down=100)]
        self.daemon.scan()
        self.assertEqual(backend.set_calls, [(1234, None, 100)])
        self.assertIn((1234, 50.0), self.daemon.applied)

        # Simulate a crash: a new daemon inherits the saved PID/start journal.
        replacement = Daemon(state_path=self.path, backend=backend)
        replacement._proc = self.daemon._proc
        self.assertIn((1234, 50.0), replacement.applied)
        replacement.users["1000"] = []
        replacement.scan()
        self.assertEqual(backend.unset_calls, [1234])
        self.assertEqual(replacement.applied, {})

    def test_journal_does_not_apply_to_recycled_pid(self):
        class TrackingBackend:
            def active_pids(self):
                return {1234}

            def set_limit(self, *args):
                raise AssertionError("Should not limit a new process")

            def unset_limit(self, *args):
                raise AssertionError("Should not unset a reused PID")

        self.daemon.applied[(1234, 50.0)] = (100, None)
        self.daemon._save()
        restored = Daemon(state_path=self.path, backend=TrackingBackend())
        restored._proc = lambda pid: {**self.proc, "start": 100.0}
        restored.scan()
        self.assertEqual(restored.applied, {})

    def test_settings_persist_across_restarts(self):
        self.daemon.handle({"op": "set_global", "enabled": True,
                            "download_kib": 128, "upload_kib": None},
                           1000, True)
        restored = Daemon(state_path=self.path, backend=FakeBackend())
        self.assertEqual(restored.global_default["download_kib"], 128)


if __name__ == "__main__":
    unittest.main()
