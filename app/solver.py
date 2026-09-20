"""Exact cubic powder-pattern indexer.

Given strictly increasing positive rational peak positions ``P_i`` and a
positive absolute rational tolerance ``T``, retained peaks are mapped, in
order, to *distinct* representable values

    N = h^2 + k^2 + l^2,   0 <= h <= k <= l <= L,   N > 0,

with a single unknown strictly positive proportionality factor ``lambda``
such that ``|P_i - lambda * N_i| <= T``.  Up to ``impurity_quota`` peaks
may be left unindexed (impurities).

The search is a complete depth first enumeration with interval
propagation and sound bound pruning — it is **not** greedy and never uses
floating point: every bound, comparison and score is an exact rational.
A separate bounded feasibility search produces an incumbent used only to
tighten pruning; it never decides the answer, which always comes from the
exhaustive search.

Solutions are ranked lexicographically by

1. number of impurity peaks (minimum),
2. number of representable values strictly between the first and last
   assigned value that the mapping does not use (minimum),
3. maximum absolute residual (minimum),
4. sum of absolute residuals (minimum).

For a fixed assignment the strips are ``a_i - w_i*r <= lambda <= a_i +
w_i*r`` with ``a_i = P_i/N_i`` and ``w_i = 1/N_i``.  By the one-dim
Helly property the smallest feasible radius is

    r* = max_{i,j: a_i > a_j} (a_i - a_j) / (w_i + w_j);

moreover ``r* <= R`` is equivalent, for *any* R, to the radius-R strips
sharing a point (``max(a_i - R*w_i) <= min(a_i + R*w_j)``).  The latter
is maintained incrementally in O(1) per node and used, with a feasible
incumbent radius, as an exact residual gate.

The exhaustive hot path carries strip endpoints as reduced integer
pairs ``(numerator, denominator)`` and compares them by cross
multiplication; :class:`~fractions.Fraction` is used only for input, for
the cheap incumbent search and when materialising a leaf solution.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

#: Upper index limit fixed by the service specification.
INDEX_LIMIT = 12
#: Hard safety bound on explored DFS nodes.  Real patterns (peaks near a
#: common lambda*N ratio with small experimental error) solve in a few
#: hundred nodes; the bound only guards pathological, unstructured wide
#: tolerance inputs.  Exceeding it is reported with a stable code, never
#: as a partial or merely-feasible answer.
NODE_BUDGET = 1_500_000
#: Node cap for the bounded feasibility search that builds the incumbent.
SEED_NODE_CAP = 40_000

Pair = tuple[int, int]


class SolverBudgetExceeded(RuntimeError):
    """Raised if the exhaustive search exceeds :data:`NODE_BUDGET`."""


def _build_representables(
    limit: int,
) -> tuple[list[int], dict[int, tuple[int, int, int]]]:
    """Sorted distinct representable N values and a canonical triple each.

    The canonical witness of a value shared by several triples is the
    lexicographically smallest ``(h, k, l)`` with ``h <= k <= l``.
    """

    witnesses: dict[int, tuple[int, int, int]] = {}
    for h in range(limit + 1):
        for k in range(h, limit + 1):
            h2k2 = h * h + k * k
            for l in range(k, limit + 1):
                n = h2k2 + l * l
                if n == 0:
                    # The direct beam (0,0,0) is not a diffraction line
                    # and cannot be reached by a positive factor anyway.
                    continue
                triple = (h, k, l)
                old = witnesses.get(n)
                if old is None or triple < old:
                    witnesses[n] = triple
    return sorted(witnesses), witnesses


VALUES, WITNESSES = _build_representables(INDEX_LIMIT)
_VALUE_SET = frozenset(VALUES)


@dataclass(frozen=True)
class AssignedPeak:
    peak_index: int
    n: int
    hkl: tuple[int, int, int]
    residual: Fraction


@dataclass(frozen=True)
class Solution:
    """One fully specified indexing."""

    scale_factor: Fraction
    assignments: tuple[AssignedPeak, ...]
    impurity_indices: tuple[int, ...]
    omitted_representables: int
    max_abs_residual: Fraction
    sum_abs_residual: Fraction

    def aligned_key(self) -> tuple[int, ...]:
        """Per-peak token: 0 for impurity, otherwise the assigned N."""
        tokens = [0] * (len(self.assignments) + len(self.impurity_indices))
        for a in self.assignments:
            tokens[a.peak_index] = a.n
        return tuple(tokens)


@dataclass
class _Best:
    score: tuple[int, int, Fraction, Fraction]
    solutions: list[Solution] = field(default_factory=list)

    def consider(self, sol: Solution) -> None:
        key = (
            len(sol.impurity_indices),
            sol.omitted_representables,
            sol.max_abs_residual,
            sol.sum_abs_residual,
        )
        if self.solutions and key > self.score:
            return
        if self.solutions and key < self.score:
            self.solutions = []
        if not self.solutions:
            self.score = key
            self.solutions = [sol]
            return
        self.score = key
        # The bounded seed and exhaustive search may find the same
        # mapping; dedupe by the aligned (per-peak N) mapping.
        if any(s.aligned_key() == sol.aligned_key() for s in self.solutions):
            return
        # Keep the two lexicographically smallest aligned mappings as
        # the canonical ambiguity witnesses.
        self.solutions.append(sol)
        self.solutions.sort(key=Solution.aligned_key)
        self.solutions = self.solutions[:2]


def is_representable(n: int) -> bool:
    return n in _VALUE_SET


def canonical_triple(n: int) -> tuple[int, int, int]:
    return WITNESSES[n]


def _gt(x: Pair, y: Pair) -> bool:
    """Strict order on positive rationals given as reduced integer pairs."""
    return x[0] * y[1] > y[0] * x[1]


def _pair(fr: Fraction) -> Pair:
    return fr.numerator, fr.denominator


def _window_bounds(
    p: Fraction,
    t: Fraction,
    lo: Optional[Fraction],
    hi: Optional[Fraction],
) -> tuple[Fraction, Fraction]:
    """Fractional lower/upper bounds (values of N) for the next strip."""
    low_value = Fraction(1)
    high_value = Fraction(VALUES[-1])
    if hi is not None:
        bound = (p - t) / hi
        if bound > 1:
            low_value = bound
    if lo is not None and lo > 0:
        high_value = (p + t) / lo
    return low_value, high_value


def _exact_radius(peaks: list[Fraction], assignment: list[Optional[int]]) -> Fraction:
    """r* = max over indexed pairs of |a_i - a_j| / (w_i + w_j)."""
    pts = [
        (peaks[i] / VALUES[rank], Fraction(1, VALUES[rank]))
        for i, rank in enumerate(assignment)
        if rank is not None
    ]
    r = Fraction(0)
    for x in range(len(pts)):
        ax, wx = pts[x]
        for y in range(x + 1, len(pts)):
            ay, wy = pts[y]
            if ax != ay:
                c = abs(ax - ay) / (wx + wy)
                if c > r:
                    r = c
    return r


def _make_solution(
    peaks: list[Fraction],
    assignment: list[Optional[int]],
    lo: Fraction,
    hi: Fraction,
    r_star: Fraction,
) -> Optional[Solution]:
    """Build a :class:`Solution` for a complete feasible ``assignment``.

    Shrinks every strip to the optimal radius ``r_star``; the strips then
    meet in one point, fixing the positive factor.  Returns ``None`` when
    no positive factor exists.
    """

    lo_star, hi_star = lo, hi
    for i, rank in enumerate(assignment):
        if rank is None:
            continue
        nv = VALUES[rank]
        lo_star = max(lo_star, (peaks[i] - r_star) / nv)
        hi_star = min(hi_star, (peaks[i] + r_star) / nv)
    if lo_star + hi_star <= 0:
        return None
    lam = (lo_star + hi_star) / 2

    assigned: list[AssignedPeak] = []
    rsum = Fraction(0)
    first_rank = -1
    last_rank = -1
    indexed = 0
    for i, rank in enumerate(assignment):
        if rank is None:
            continue
        if first_rank < 0:
            first_rank = rank
        last_rank = rank
        indexed += 1
        nval = VALUES[rank]
        residual = abs(peaks[i] - lam * nval)
        rsum += residual
        assigned.append(
            AssignedPeak(peak_index=i, n=nval, hkl=WITNESSES[nval], residual=residual)
        )
    if indexed < 2:
        return None
    omitted = last_rank - first_rank + 1 - indexed
    return Solution(
        scale_factor=lam,
        assignments=tuple(assigned),
        impurity_indices=tuple(i for i, r in enumerate(assignment) if r is None),
        omitted_representables=omitted,
        max_abs_residual=r_star,
        sum_abs_residual=rsum,
    )


def _seed_solution(
    peaks: list[Fraction],
    tol: Fraction,
    quota: int,
) -> Optional[Solution]:
    """Bounded feasibility DFS producing a pruning incumbent.

    Minimises (impurity count, omitted-value count) over the tolerance
    strips under a hard node cap.  It never decides the result —
    :func:`solve` always performs the complete exhaustive search — so it
    cannot leak a greedy answer; it only supplies an upper bound that
    tightens pruning.
    """

    n = len(peaks)
    strip_lo = [[(p - tol) / v for v in VALUES] for p in peaks]
    strip_hi = [[(p + tol) / v for v in VALUES] for p in peaks]
    assignment: list[Optional[int]] = [None] * n
    best_sol: Optional[Solution] = None
    nodes = 0

    def better(cand: Solution) -> bool:
        if best_sol is None:
            return True
        return (
            len(cand.impurity_indices),
            cand.omitted_representables,
            cand.max_abs_residual,
            cand.sum_abs_residual,
        ) < (
            len(best_sol.impurity_indices),
            best_sol.omitted_representables,
            best_sol.max_abs_residual,
            best_sol.sum_abs_residual,
        )

    def dfs(
        i: int,
        prev_rank: int,
        impurities: int,
        lo: Optional[Fraction],
        hi: Optional[Fraction],
        missing: int,
        indexed: int,
    ) -> bool:
        nonlocal nodes, best_sol
        nodes += 1
        if nodes > SEED_NODE_CAP:
            return False
        remaining = n - i
        must_index = remaining - (quota - impurities)
        if must_index > 0 and len(VALUES) - 1 - prev_rank < must_index:
            return True
        if best_sol is not None and impurities > len(best_sol.impurity_indices):
            return True
        if (
            best_sol is not None
            and indexed
            and impurities == len(best_sol.impurity_indices)
            and missing >= best_sol.omitted_representables
        ):
            return True

        if i == n:
            if indexed < 2 or lo is None or hi is None:
                return True
            sol = _make_solution(
                peaks, assignment, lo, hi, _exact_radius(peaks, assignment)
            )
            if sol is not None and better(sol):
                best_sol = sol
            return True

        p = peaks[i]
        low_value, high_value = _window_bounds(p, tol, lo, hi)
        must_after_here = (n - i - 1) - (quota - impurities)
        rank_ceiling = (
            len(VALUES) - must_after_here if must_after_here > 0 else len(VALUES)
        )
        r_start = max(prev_rank + 1, bisect_left(VALUES, low_value))
        r_end = min(rank_ceiling, bisect_right(VALUES, high_value))
        slo = strip_lo[i]
        shi = strip_hi[i]
        last_shi = strip_hi[n - 1]

        for rank in range(r_start, r_end):
            new_lo = slo[rank] if lo is None else max(lo, slo[rank])
            new_hi = shi[rank] if hi is None else min(hi, shi[rank])
            if new_lo > new_hi:
                continue
            new_missing = missing if indexed == 0 else missing + (rank - prev_rank - 1)
            if (
                best_sol is not None
                and impurities == len(best_sol.impurity_indices)
                and new_missing >= best_sol.omitted_representables
            ):
                break
            if must_after_here > 0 and new_lo > 0:
                if new_lo > last_shi[rank + must_after_here]:
                    continue
            assignment[i] = rank
            cont = dfs(
                i + 1, rank, impurities, new_lo, new_hi, new_missing, indexed + 1
            )
            assignment[i] = None
            if not cont:
                return False

        if impurities < quota:
            return dfs(
                i + 1, prev_rank, impurities + 1, lo, hi, missing, indexed
            )
        return True

    dfs(0, -1, 0, None, None, 0, 0)
    return best_sol


def solve(
    peaks: list[Fraction],
    tolerance: Fraction,
    impurity_quota: int,
    return_stats: bool = False,
):
    """Return the optimally tied solutions, or ``None`` when infeasible.

    A one-element list is a unique solution; two elements mark an exact
    four-way tie (ambiguity) and are the canonical witnesses.  With
    ``return_stats`` the result is ``(solutions, {"nodes": int})``.
    """

    n = len(peaks)
    t = tolerance
    best = _Best(score=(10**9, 10**9, Fraction(10**30), Fraction(10**30)))
    nodes = 0
    R = len(VALUES)

    # Incumbent used purely for pruning; exhaustive search is authority.
    seed = _seed_solution(peaks, t, impurity_quota)
    if seed is not None:
        best.consider(seed)
    gate_fr: Optional[Fraction] = (
        seed.max_abs_residual if seed is not None else None
    )

    # State-independent strip endpoints as reduced integer pairs.  The
    # hot loop only cross-multiplies these (no Fraction dispatch).
    p_minus = [p - t for p in peaks]
    p_plus = [p + t for p in peaks]
    strip_lo = [[_pair((p - t) / v) for v in VALUES] for p in peaks]
    strip_hi = [[_pair((p + t) / v) for v in VALUES] for p in peaks]
    if gate_fr is not None:
        gate_lo = [[_pair((p - gate_fr) / v) for v in VALUES] for p in peaks]
        gate_hi = [[_pair((p + gate_fr) / v) for v in VALUES] for p in peaks]
    else:
        gate_lo = gate_hi = []

    assignment: list[Optional[int]] = [None] * n
    best_imp = best.score[0] if best.solutions else 10**9
    best_miss = best.score[1] if best.solutions else 10**9

    def terminal(lo: Pair, hi: Pair) -> None:
        nonlocal best_imp, best_miss
        r_star = _exact_radius(peaks, assignment)
        sol = _make_solution(
            peaks, assignment, Fraction(lo[0], lo[1]), Fraction(hi[0], hi[1]), r_star
        )
        if sol is not None:
            best.consider(sol)
            best_imp, best_miss = best.score[0], best.score[1]

    def dfs(
        i: int,
        prev_rank: int,
        impurities: int,
        lo: Optional[Pair],
        hi: Optional[Pair],
        missing: int,
        first_rank: int,
        indexed: int,
        gl: Optional[Pair],
        gh: Optional[Pair],
    ) -> None:
        nonlocal nodes, best_imp, best_miss
        nodes += 1
        if nodes > NODE_BUDGET:
            raise SolverBudgetExceeded("search node budget exhausted")

        remaining_peaks = n - i
        must_index = remaining_peaks - (impurity_quota - impurities)
        if must_index > 0 and R - 1 - prev_rank < must_index:
            return

        # Dominance on the first two (monotone) settled criteria.
        if best.solutions and impurities >= best_imp:
            if impurities > best_imp or missing > best_miss:
                return

        if i == n:
            if indexed < 2 or lo is None or hi is None:
                return
            terminal(lo, hi)
            return

        # Integer (ceil/floor) bounds are bounds on the VALUE N; map
        # them back to ranks with bisect because representable values
        # have gaps (e.g. 7 is not h^2+k^2+l^2).
        r_start = prev_rank + 1
        if hi is not None:
            pm = p_minus[i]
            if pm > 0:  # ceil(pm / hi)
                num = pm.numerator * hi[1]
                den = pm.denominator * hi[0]
                ceil_val = -(-num // den)
                r_start = max(r_start, bisect_left(VALUES, ceil_val))
        r_end = R
        if lo is not None and lo[0] > 0:
            pp = p_plus[i]  # floor(pp / lo)
            floor_val = (pp.numerator * lo[1]) // (pp.denominator * lo[0])
            r_end = bisect_right(VALUES, floor_val)

        must_after = (n - i - 1) - (impurity_quota - impurities)
        if must_after > 0:
            ceiling = R - must_after
            if ceiling < r_end:
                r_end = ceiling

        gate_on = gate_fr is not None and impurities == best_imp
        slo = strip_lo[i]
        shi = strip_hi[i]
        glo = gate_lo[i] if gate_on else None
        ghi = gate_hi[i] if gate_on else None
        last_shi = strip_hi[n - 1]

        for rank in range(r_start, r_end):
            cand_lo = slo[rank]
            cand_hi = shi[rank]
            new_lo = cand_lo if lo is None or _gt(cand_lo, lo) else lo
            new_hi = cand_hi if hi is None or _gt(hi, cand_hi) else hi
            if _gt(new_lo, new_hi):
                continue
            new_missing = (
                missing if indexed == 0 else missing + (rank - prev_rank - 1)
            )
            if best.solutions and impurities == best_imp and new_missing > best_miss:
                continue
            if must_after > 0 and new_lo[0] > 0:
                if _gt(new_lo, last_shi[rank + must_after]):
                    continue

            new_gl, new_gh = gl, gh
            if gate_on:
                b_lo, b_hi = glo[rank], ghi[rank]
                new_gl = b_lo if gl is None or _gt(b_lo, gl) else gl
                new_gh = b_hi if gh is None or _gt(gh, b_hi) else gh
                if new_missing == best_miss and _gt(new_gl, new_gh):
                    continue

            assignment[i] = rank
            dfs(
                i + 1,
                rank,
                impurities,
                new_lo,
                new_hi,
                new_missing,
                rank if indexed == 0 else first_rank,
                indexed + 1,
                new_gl,
                new_gh,
            )
            assignment[i] = None

        if impurities < impurity_quota:
            dfs(
                i + 1,
                prev_rank,
                impurities + 1,
                lo,
                hi,
                missing,
                first_rank,
                indexed,
                gl,
                gh,
            )

    dfs(0, -1, 0, None, None, 0, -1, 0, None, None)
    if return_stats:
        return best.solutions or None, {"nodes": nodes}
    return best.solutions or None
