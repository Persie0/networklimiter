import unittest

from networklimiter.core import RuleError, choose_rule, kbps, limit, normalize_rule


class RulesTest(unittest.TestCase):
    def test_conversion_is_kib_not_kbit(self):
        self.assertEqual(kbps(100), "819")
        self.assertEqual(kbps(1024), "8389")
        self.assertEqual(kbps(None), "-1")

    def test_limits(self):
        self.assertEqual(limit(1), 1)
        for value in (-1, 0, 1.5, True, "100", 1000001):
            with self.subTest(value=value), self.assertRaises(RuleError):
                limit(value)

    def test_invalid_rules(self):
        cases = [
            {"mode": "exe", "target": "relative/brave", "download_kib": 100},
            {"mode": "name", "target": "foo;rm -rf", "download_kib": 100},
            {"mode": "name", "target": "!!!", "download_kib": 100},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RuleError):
                normalize_rule(case, owner=1000, scope="user")

    def test_unlimited_exception_rule(self):
        exception = normalize_rule(
            {"mode": "name", "target": "ssh", "download_kib": None, "upload_kib": None},
            owner=1000, scope="user",
        )
        self.assertIsNone(exception["download_kib"])
        self.assertIsNone(exception["upload_kib"])

    def test_per_executable_match(self):
        brave = normalize_rule(
            {"mode": "exe", "target": "/opt/brave.com/brave/brave",
             "download_kib": 100, "upload_kib": None},
            owner=1000, scope="user",
        )
        p1 = {"pid": 1, "name": "brave", "exe": "/opt/brave.com/brave/brave"}
        p2 = {"pid": 2, "name": "brave", "exe": "/opt/brave.com/brave/brave"}
        # Rule applies separately to both PIDs, not one pooled budget.
        self.assertEqual(choose_rule(p1, [brave], []), brave)
        self.assertEqual(choose_rule(p2, [brave], []), brave)

    def test_user_override_precedence(self):
        system = normalize_rule(
            {"mode": "name", "target": "pacman", "download_kib": 100},
            owner=0, scope="system",
        )
        user = normalize_rule(
            {"mode": "name", "target": "pacman", "download_kib": 10},
            owner=1000, scope="user",
        )
        self.assertEqual(choose_rule({"name": "pacman"}, [user], [system]), user)


if __name__ == "__main__":
    unittest.main()
