"""Pure rule validation and matching for NetworkLimiter."""
from __future__ import annotations

import os
import re
import uuid

MAX_KIB = 1_000_000
_NAME = re.compile(r"^[a-zA-Z0-9_.+@-]{1,128}$")


class RuleError(ValueError):
    pass


def limit(value):
    """None means unlimited; otherwise an integer KiB/s."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_KIB:
        raise RuleError(f"Rate must be 1–{MAX_KIB} KiB/s or unlimited")
    return value


def kbps(value):
    """Convert KiB/s to upstream pidnl's decimal kilobits/second."""
    if value is None:
        return "-1"
    return str(round(limit(value) * 1024 * 8 / 1000))


def normalize_rule(raw, *, owner, scope, id_override=None):
    if not isinstance(raw, dict):
        raise RuleError("Expected an object")
    if scope not in ("user", "system"):
        raise RuleError("Invalid scope")
    mode = raw.get("mode")
    target = raw.get("target")
    if mode == "exe":
        if not isinstance(target, str) or not target.startswith("/") or len(target) > 4096:
            raise RuleError("Executable must be an absolute path")
        target = os.path.realpath(target)
    elif mode == "name":
        if not isinstance(target, str) or not _NAME.fullmatch(target):
            raise RuleError("Process name contains invalid characters")
    else:
        raise RuleError("Mode must be exe or name")
    down, up = limit(raw.get("download_kib")), limit(raw.get("upload_kib"))
    if down is None and up is None:
        raise RuleError("At least one direction must have a limit")
    return {
        "id": id_override or uuid.uuid4().hex,
        "owner": int(owner),
        "scope": scope,
        "mode": mode,
        "target": target,
        "download_kib": down,
        "upload_kib": up,
    }


def matches(rule, proc):
    if rule["mode"] == "exe":
        return bool(proc.get("exe")) and os.path.realpath(proc["exe"]) == rule["target"]
    return proc.get("name") == rule["target"]


def choose_rule(proc, own_rules, system_rules):
    """User-specific rules take precedence over system rules."""
    for rule in own_rules:
        if matches(rule, proc):
            return rule
    for rule in system_rules:
        if matches(rule, proc):
            return rule
    return None
