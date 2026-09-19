"""Correctness tests: brute-force reference vs. the interval-propagation solver.

The reference enumerates *every* feasible retained-peak subset (with at most
``b`` impurities) against *every* increasing subsequence of representable
values, with no pruning beyond feasibility.  It shares only the exact
one-dimensional residual minimizer with the production solver, so the
exhaustiveness and lex-ordering logic of the DFS is independently checked.
"""
from __future__ import annotations

import time
from fractions import Fraction
from itertools import combinations

import pytest

from app.representable import representable_values, witnesses
from app.solver import (
    _residual_minimizer,
    solve_indexing,
    _Pattern,
)
from app import service

VALUES = representable_values()
R = len(VALUES)


def brute_force(peaks, tol, b, domain=None):
    """Independent reference on a (possibly restricted) representable domain."""
    tvals = VALUES if domain is None else tuple(VALUES[j] for j in domain)
    tr = len(tvals)
    m = len(peaks)
    winners: list[tuple] = []
    best = None

    def rec(pos, j_start, lo, hi, peak_combo, val_idx):
        nonlocal best, winners
        if pos == len(peak_combo):
            nseq = tuple(tvals[j] for j in val_idx)
            pat = _Pattern(
                assignment=nseq,
                matched_indices=peak_combo,
                interval=(lo, hi),
                skipped=val_idx[-1] - val_idx[0] + 1 - len(val_idx),
            )
            c, rmax, rsum = _residual_minimizer(peaks, pat)
            key = (m - len(peak_combo), pat.skipped, rmax, rsum)
            if best is None or key < best:
                best = key
                winners = [(nseq, key)]
            elif key == best:
                winners.append((nseq, key))
            return
        pi = peak_combo[pos]
        from app.solver import _window_bounds

        wl, wh = _window_bounds(tvals, peaks[pi], tol, lo, hi, j_start, tr)
        for j in range(wl, wh):
            n = tvals[j]
            l = (peaks[pi] - tol) / n
            h = (peaks[pi] + tol) / n
            if l < 0:
                l = Fraction(0)
            nlo = max(lo, l)
            nhi = min(hi, h)
            if nlo > nhi or nhi <= 0:
                continue
            rec(pos + 1, j + 1, nlo, nhi, peak_combo, val_idx + [j])

    for n_match in range(max(1, m - b), m + 1):
        for peak_combo in combinations(range(m), n_match):
            rec(0, 0, Fraction(0), Fraction(10**300), peak_combo, [])

    if best is None:
        return None
    distinct = {nseq: key for nseq, key in winners}
    return best, distinct


def _keys(result):
    return {
        tuple(m.n for m in s.mappings): (
            s.impurity_count,
            s.skipped_representable,
            s.max_abs_residual,
            s.sum_abs_residual,
        )
        for s in result.solutions
    }


def assert_matches_bruteforce(peaks, tol, b, domain=None):
    restricted = None
    if domain is not None:
        restricted = tuple(VALUES[j] for j in domain)
    res = solve_indexing(peaks, tol, b, _values=restricted)
    ref = brute_force(peaks, tol, b, domain=domain)
    if ref is None:
        assert res.status == "no_solution", f"expected no solution for {peaks}"
        return
    best, distinct = ref
    expected_nseqs = set(distinct.keys())
    got = _keys(res)
    assert set(got.keys()) == expected_nseqs, (
        f"\npeaks={[str(p) for p in peaks]} tol={tol} b={b}"
        f"\nref={sorted(expected_nseqs)}\ngot={sorted(got)}"
    )
    for k in got.values():
        assert k == best
    expected_status = "ambiguous" if len(expected_nseqs) >= 2 else "unique"
    assert res.status == expected_status
    if expected_status == "ambiguous":
        assert len(res.solutions) == 2


# -- Exact constructed scenarios ------------------------------------------------

def test_simple_unit_scale():
    assert_matches_bruteforce([Fraction(n) for n in (1, 2, 3, 4)], 0, 0)


def test_rational_three_halves():
    res = solve_indexing([Fraction(3, 2), 3, Fraction(9, 2), 6], 0, 0)
    assert res.status == "unique"
    assert res.solutions[0].scale_factor == Fraction(3, 2)


def test_tolerance_residuals_exact():
    res = solve_indexing([1, 2, 3, Fraction(41, 10)], Fraction(1, 10), 0)
    s = res.solutions[0]
    assert s.max_abs_residual == Fraction(3, 70)
    assert s.sum_abs_residual == Fraction(9, 70)
    assert all(m.residual <= Fraction(1, 10) for m in s.mappings)


def test_impurity_required():
    peaks = [Fraction(1), Fraction(2), Fraction(3), Fraction(382, 127), Fraction(4)]
    res = solve_indexing(peaks, 0, 1)
    assert res.status == "unique"
    assert res.solutions[0].impurity_indices == (3,)
    assert solve_indexing(peaks, 0, 0).status == "no_solution"


def test_impurity_and_nosolution_match_bruteforce():
    peaks = [Fraction(1), Fraction(2), Fraction(3), Fraction(382, 127), Fraction(4)]
    assert_matches_bruteforce(peaks, Fraction(0), 0)
    assert_matches_bruteforce(peaks, Fraction(0), 1)
    assert_matches_bruteforce(
        [Fraction(1), Fraction(2), Fraction(3), Fraction(335)], Fraction(0), 0
    )


def test_ambiguous_pair():
    res = solve_indexing([Fraction(x) for x in (272, 274, 278, 280, 288)], 0, 0)
    assert res.status == "ambiguous"
    assert len(res.solutions) == 2
    nseqs = {tuple(m.n for m in s.mappings) for s in res.solutions}
    assert nseqs == {(136, 137, 139, 140, 144), (272, 274, 278, 280, 288)}
    for s in res.solutions:
        assert (s.impurity_count, s.skipped_representable) == (0, 3)
        assert (s.max_abs_residual, s.sum_abs_residual) == (0, 0)


def test_canonical_hkl_and_bounds():
    res = solve_indexing([Fraction(x) for x in (272, 274, 278, 280, 288)], 0, 0)
    wits = witnesses()
    for s in res.solutions:
        for m in s.mappings:
            h, k, l = m.hkl
            assert 0 <= h <= k <= l <= 12
            assert h * h + k * k + l * l == m.n
            assert m.hkl == wits[m.n]


# -- Feasibility invariant on every returned solution ---------------------------

@pytest.mark.parametrize("b", [0, 1, 2])
def test_feasibility_invariant_random(b):
    import random

    rng = random.Random(9900 + b)
    checked = 0
    for _ in range(40):
        m = rng.randint(4, 9)
        seq = sorted(rng.sample(VALUES, m))
        c = Fraction(rng.randint(1, 4), rng.randint(1, 4))
        tol = Fraction(rng.choice([0, 0, 1]), 100)
        peaks = [c * n + (Fraction(rng.randint(-1, 1), 100) if tol else 0) for n in seq]
        peaks = [p for p in peaks if p > 0]
        if len(peaks) < 4 or any(
            peaks[i] >= peaks[i + 1] for i in range(len(peaks) - 1)
        ):
            continue
        res = solve_indexing(peaks, tol, b)
        checked += 1
        for s in res.solutions:
            ns = [m.n for m in s.mappings]
            assert ns == sorted(set(ns))
            assert all(n in VALUES for n in ns)
            assert len(s.impurity_indices) <= b
            for m_ in s.mappings:
                assert abs(peaks[m_.peak_index] - s.scale_factor * m_.n) == m_.residual
                assert m_.residual <= tol
            span = [v for v in VALUES if ns[0] < v < ns[-1]]
            assert s.skipped_representable == len(span) - (len(ns) - 2)
    assert checked >= 30


# -- Brute-force equivalence over a deterministic pseudo-random corpus -----------
# Kept small (m = 4..5, exact or tight tolerance) because the unpruned reference
# is exponential by design; the production solver is performance-tested
# separately on 32-peak inputs.

def _equivalence_corpus(seed, count):
    import random

    rng = random.Random(seed)
    by_b = {0: [], 1: [], 2: []}
    made = 0
    while made < count:
        m = rng.randint(4, 5)
        seq = sorted(rng.sample(VALUES, m))
        c = Fraction(rng.randint(1, 4), rng.randint(1, 4))
        use_tol = rng.random() < 0.5
        tol = Fraction(1, 100) if use_tol else Fraction(0)
        peaks = [
            c * n + (Fraction(rng.randint(-1, 1), 100) if use_tol else 0)
            for n in seq
        ]
        if any(p <= 0 for p in peaks) or any(
            peaks[i] >= peaks[i + 1] for i in range(m - 1)
        ):
            continue
        b = rng.randint(0, 2)
        by_b[b].append((peaks, tol))
        made += 1
    return by_b


def test_bruteforce_equivalence():
    corpus = _equivalence_corpus(20260919, 70)
    total = 0
    for b, cases in corpus.items():
        for peaks, tol in cases:
            assert_matches_bruteforce(peaks, tol, b)
            total += 1
    assert total >= 60


def test_bruteforce_equivalence_large_tolerance_small_domain():
    # Large absolute tolerances create many ties; cross-check on a small
    # representable domain where the unpruned reference stays tractable.
    import random

    rng = random.Random(4242)
    domain = tuple(range(18))  # first 18 representable value indices
    checked = 0
    for _ in range(40):
        m = rng.randint(4, 5)
        seq = sorted(rng.sample(list(domain), m))
        c = Fraction(rng.randint(1, 3), rng.randint(1, 2))
        tol = Fraction(rng.choice([1, 2, 3]), rng.choice([1, 2]))
        peaks = [c * VALUES[j] + Fraction(rng.randint(-3, 3), 10) for j in seq]
        if any(x <= 0 for x in peaks) or any(
            peaks[i] >= peaks[i + 1] for i in range(m - 1)
        ):
            continue
        b = rng.randint(0, 2)
        assert_matches_bruteforce(peaks, tol, b, domain=domain)
        checked += 1
    assert checked >= 25


# -- Performance: 32 peaks must complete comfortably ----------------------------

def test_performance_32_peaks():
    # Strictly increasing rational peaks near a large-N mapping; worst-ish
    # feasible request with full peak count.
    import random

    rng = random.Random(7)
    seq = rng.sample(range(1, 200), 32)
    seq.sort()
    c = Fraction(3, 2)
    peaks = [c * n for n in seq]
    start = time.perf_counter()
    res = solve_indexing(peaks, 0, 2)
    elapsed = time.perf_counter() - start
    assert res.status in ("unique", "ambiguous", "no_solution")
    assert elapsed < 5.0, f"solver took {elapsed:.2f}s for 32 peaks"


def test_dense_overdetermined_performance():
    # 32 peaks tightly clustered forces many infeasible branches quickly.
    peaks = [Fraction(1000 + i) for i in range(32)]
    start = time.perf_counter()
    res = solve_indexing(peaks, Fraction(1, 100), 2)
    elapsed = time.perf_counter() - start
    assert res.status in ("unique", "ambiguous", "no_solution")
    assert elapsed < 5.0


# -- Service layer + certificate integration -----------------------------------

def test_service_verify_tamper():
    import base64
    import hashlib
    import hmac
    import json
    import os

    from app.certificate import DEFAULT_DEV_SECRET

    out = service.run(["3/2", "3", "9/2", "6"], "0", 0)
    assert service.verify(out["certificate"])["valid"] is True

    body, sig = out["certificate"].split(".")
    pad = "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body + pad))
    payload["solutions"][0]["scale_factor"] = "999/1"
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    nb = base64.urlsafe_b64encode(canon).rstrip(b"=").decode()
    secret = os.environ.get("CERT_SECRET", DEFAULT_DEV_SECRET).encode()
    ns = base64.urlsafe_b64encode(
        hmac.new(secret, nb.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    with pytest.raises(service.E.ApiError) as exc:
        service.verify(f"{nb}.{ns}")
    assert exc.value.code == service.E.CERTIFICATE_RECOMPUTATION_MISMATCH
