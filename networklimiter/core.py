"""Rate conversion and validation for one global per-process download limit."""

MAX_KIB = 1_000_000
DEFAULT_KIB = 1024


class RateError(ValueError):
    """Invalid download rate."""


def validate_rate(value):
    """Require an integer amount of KiB/s (1 KiB = 1024 bytes)."""
    if type(value) is not int or not 1 <= value <= MAX_KIB:
        raise RateError(f"Download rate must be 1–{MAX_KIB} KiB/s")
    return value


def to_pidnl_kbps(kib_per_second):
    """pidnl takes decimal kilobits/s, not KiB/s.

    Use integer rounding, avoiding floating-point precision loss.
    """
    return str((validate_rate(kib_per_second) * 1024 * 8 + 500) // 1000)
