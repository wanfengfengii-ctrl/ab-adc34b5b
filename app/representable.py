"""Table of representable values N = h^2 + k^2 + l^2.

Indices are restricted to ``0 <= h <= k <= l <= MAX_INDEX`` (canonical,
non-negative).  For every distinct representable sum we record one canonical
witness ``(h, k, l)``; the physical problem (cubic powder indexing) only
depends on the sum, so distinct index triples sharing a sum are the *same*
mapping point.
"""
from __future__ import annotations

from functools import lru_cache

MAX_INDEX = 12


def build_table() -> list[int]:
    """Return the sorted list of distinct representable sums.

    With indices in ``0..12`` and ``h <= k <= l`` the smallest positive
    representable value is ``1`` (001); ``0`` (000) is excluded because
    observed peaks are strictly positive.
    """
    values: set[int] = set()
    for h in range(0, MAX_INDEX + 1):
        for k in range(h, MAX_INDEX + 1):
            for l in range(k, MAX_INDEX + 1):
                n = h * h + k * k + l * l
                if n > 0:
                    values.add(n)
    return sorted(values)


@lru_cache(maxsize=1)
def representable_values() -> tuple[int, ...]:
    return tuple(build_table())


@lru_cache(maxsize=1)
def witnesses() -> dict[int, tuple[int, int, int]]:
    """Canonical (h, k, l) witness for each representable sum."""
    result: dict[int, tuple[int, int, int]] = {}
    for h in range(0, MAX_INDEX + 1):
        for k in range(h, MAX_INDEX + 1):
            for l in range(k, MAX_INDEX + 1):
                n = h * h + k * k + l * l
                if n > 0 and n not in result:
                    result[n] = (h, k, l)
    return result


def is_representable(n: int) -> bool:
    return n in _value_set()


@lru_cache(maxsize=1)
def _value_set() -> frozenset[int]:
    return frozenset(representable_values())
