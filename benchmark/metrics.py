"""Honest statistics helpers for the SYNK 2.0 benchmark runner.

Only what runner.py actually uses: mean and percentiles over measured
samples. No scores, no invented constants.
"""
from __future__ import annotations


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def percentile(xs: list[float], p: float) -> float:
    """Nearest-rank percentile of measured samples."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    k = max(0, min(len(ordered) - 1, int(round(p / 100.0 * (len(ordered) - 1)))))
    return ordered[k]
