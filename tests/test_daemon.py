"""Global-only daemon tests with an in-memory kernel backend."""
import json
import tempfile
import unittest
from pathlib import Path

from networklimiter.daemon import Backend, Daemon


class FakeBackend:
    def __init__(self, pids=()):
        self.pids = set(pids)
        self.set_calls = []
        self.unset_calls = []

    def active_pids(self):
        return set(self.pids)

    def set_limit(self, pid, download_kib):
        self.set_calls.append((pid, download_kib))

    def unset_limit(self, pid):
        self.unset_calls.append(pid)


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.backend = FakeBackend((11111, 11112))
        self.daemon = Daemon(state_path=self.path, backend=self.backend)
        self.starts = {11111: 10.1, 11112: 20.2}
        self.daemon._process_start = lambda pid: self.starts.get(pid)

    def tearDown(self):
        self.tmp.cleanup()

    def set_global(self, enabled=True, rate=100):
        return self.daemon.handle(
            {"op": "set_global", "enabled": enabled, "download_kib": rate},
            admin=True,
        )

    def test_backend_limits_download_only(self):
        backend = Backend()
        calls = []
        backend.call = lambda *args: calls.append(args)
        backend.set_limit(11111, 1024)
        self.assertEqual(calls, [("limit", "set", 11111, "-1", "8389")])

    def test_defaults_off_but_rate_remembered(self):
        status = self.daemon.handle({"op": "status"}, admin=False)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["download_kib"], 1024)
        self.assertFalse(status["admin"])
        self.assertEqual(self.backend.set_calls, [])

    def test_each_pid_gets_its_own_identical_cap(self):
        self.set_global(rate=100)
        self.daemon.scan()
        self.assertCountEqual(self.backend.set_calls, [(11111, 100), (11112, 100)])
        status = self.daemon.handle({"op": "status"}, admin=False)
        self.assertEqual(status["active_count"], 2)
        self.assertEqual(status["limited_count"], 2)

    def test_rate_change_updates_all_existing_pids(self):
        self.set_global(rate=100)
        self.daemon.scan()
        self.set_global(rate=1024)
        self.daemon.scan()
        self.assertEqual(self.backend.set_calls[-2:], [(11111, 1024), (11112, 1024)])

    def test_disable_unsets_all_and_retains_speed(self):
        self.set_global(rate=100)
        self.daemon.scan()
        self.set_global(False, 100)
        self.daemon.scan()
        self.assertCountEqual(self.backend.unset_calls, [11111, 11112])
        self.assertFalse(self.daemon.enabled)
        self.assertEqual(self.daemon.download_kib, 100)
        self.assertEqual(self.daemon.applied, {})

    def test_reject_legacy_per_pid_and_per_app_endpoints(self):
        for operation in ("set_pid", "clear_pid", "upsert_rule", "delete_rule", "pause"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.daemon.handle({"op": operation}, admin=True)

    def test_only_admin_can_edit(self):
        with self.assertRaises(PermissionError):
            self.daemon.handle(
                {"op": "set_global", "enabled": True, "download_kib": 100},
                admin=False,
            )

    def test_set_requires_boolean_toggle_and_valid_rate(self):
        for enabled in (1, None, "true"):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                self.daemon.handle(
                    {"op": "set_global", "enabled": enabled, "download_kib": 100},
                    admin=True,
                )
        with self.assertRaises(ValueError):
            self.daemon.handle(
                {"op": "set_global", "enabled": True, "download_kib": 0},
                admin=True,
            )

    def test_persistence_across_daemon_restart(self):
        self.set_global(rate=2048)
        restored = Daemon(state_path=self.path, backend=FakeBackend())
        self.assertTrue(restored.enabled)
        self.assertEqual(restored.download_kib, 2048)

    def test_legacy_app_rules_are_discarded_on_upgrade(self):
        legacy = {
            "users": {"1000": [{"target": "/usr/bin/curl", "download_kib": 10}]},
            "system": [{"target": "pacman", "download_kib": 50}],
            "global_default": {"download_kib": 100, "upload_kib": None},
            "paused_system": False,
            "applied": [],
        }
        self.path.write_text(json.dumps(legacy))
        upgraded = Daemon(state_path=self.path, backend=FakeBackend())
        self.assertTrue(upgraded.enabled)
        self.assertEqual(upgraded.download_kib, 100)
        upgraded._save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 2)
        self.assertNotIn("users", data)
        self.assertNotIn("system", data)

    def test_migration_old_per_process_limit_is_released(self):
        legacy = {
            "users": {"1000": [{"target": "curl", "download_kib": 10}]},
            "system": [],
            "global_default": None,
            "applied": [
                {"pid": 11111, "start": 10.1,
                 "download_kib": 10, "upload_kib": None},
            ],
        }
        self.path.write_text(json.dumps(legacy))
        upgraded = Daemon(state_path=self.path, backend=self.backend)
        upgraded._process_start = self.daemon._process_start
        self.assertFalse(upgraded.enabled)
        upgraded.scan()
        self.assertEqual(self.backend.unset_calls, [11111])

    def test_restarted_journal_keeps_pid_identity(self):
        self.set_global(rate=100)
        self.daemon.scan()
        recovered = Daemon(state_path=self.path, backend=self.backend)
        recovered._process_start = self.daemon._process_start
        recovered.handle(
            {"op": "set_global", "enabled": False, "download_kib": 100},
            admin=True,
        )
        recovered.scan()
        self.assertCountEqual(self.backend.unset_calls, [11111, 11112])

    def test_recycled_pid_is_not_touched(self):
        self.set_global(rate=100)
        self.daemon.scan()
        self.set_global(False, 100)
        self.starts[11111] = 45.1
        self.backend.pids = {11111}
        self.daemon.scan()
        self.assertNotIn(11111, self.backend.unset_calls)

    def test_new_pid_discovered_after_first_scan(self):
        self.set_global(rate=100)
        self.daemon.scan()
        self.backend.pids.add(11113)
        self.starts[11113] = 30.3
        self.daemon.scan()
        self.assertIn((11113, 100), self.backend.set_calls)


if __name__ == "__main__":
    unittest.main()
