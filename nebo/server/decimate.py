"""Read-path decimation policy for accumulating metric series.

One implementation, two callers: `daemon.downsample_series` applies it to
series already resident in RAM, and `cache._metrics_for` applies it to rows
coming out of SQL. Keeping the *policy* here means the two paths cannot
drift — a run returns the same points whether or not it has been evicted.

Line series keep the minimum and maximum of each bucket, because a uniform
stride steps straight over the spikes people are looking for. Scatter takes
a uniform stride instead: its values aren't scalar, so there is no
meaningful extreme to preserve.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

ACCUMULATING_TYPES = ("line", "scatter")


def numeric(value: Any) -> Optional[float]:
    """Best-effort scalar for bucket comparison; None if not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def keep_indices(
    values: Sequence[Optional[float]], stype: str, points: int,
) -> Optional[list[int]]:
    """Indices to keep from a series of `len(values)` entries.

    Returns None when nothing should be dropped, so callers can skip
    rebuilding the list entirely. `values` is only read for line series;
    scatter and snapshots need the length alone.
    """
    total = len(values)
    if points <= 0 or total <= points or stype not in ACCUMULATING_TYPES:
        return None

    if stype == "scatter":
        stride = -(-total // points)  # ceil
        keep = list(range(0, total, stride))
        if keep[-1] != total - 1:
            keep.append(total - 1)
        return keep

    n_buckets = max(1, points // 2)
    keep_set: set[int] = {0, total - 1}
    for b in range(n_buckets):
        lo = (b * total) // n_buckets
        hi = max(lo + 1, ((b + 1) * total) // n_buckets)
        imin = imax = lo
        vmin = vmax = None
        for i in range(lo, min(hi, total)):
            v = values[i]
            if v is None:
                continue
            if vmin is None or v < vmin:
                vmin, imin = v, i
            if vmax is None or v > vmax:
                vmax, imax = v, i
        keep_set.add(imin)
        keep_set.add(imax)
    return sorted(keep_set)
