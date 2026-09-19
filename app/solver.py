"""Cubic-cell powder indexing solver (exact arithmetic, exhaustive).

Algorithm
---------
Peaks ``p_i`` (strictly increasing positive rationals) are mapped, in order,
to distinct representable values ``N_j = h^2+k^2+l^2`` with indices
``0 <= h <= k <= l <= 12``.  Up to ``b <= 2`` peaks may be impurities.  An
unknown strictly positive proportionality factor ``c`` must satisfy

    |p_i - c * N_j| <= tolerance

for every retained peak (exact rational comparison, never float).

The search is an exhaustive *interval propagation* DFS, not greedy peak
picking: a partial mapping carries the feasible interval ``[L, U]`` for ``c``
obtained by intersecting every assigned peak's tolerance interval.  A branch
is extended while the interval stays non-empty and while a lex-optimal
completion is still possible; exact relaxations provide lower bounds on the
extra impurity count and skipped-value count, and an exact residual
branch-and-bound prunes classes that cannot improve on the incumbent.  Every
surviving complete mapping is scored on

    1. number of impurity peaks
    2. representable values skipped between the first and last mapping
    3. maximum absolute residual
    4. sum of absolute residuals

For a fixed mapping, criteria 3 and 4 are minimized over ``c in [L, U]`` by
an exact piecewise-linear convex minimization (all breakpoints rational).
Two mappings with identical four criteria but different peak->N assignments
make the result *ambiguous*; two canonical witnesses are returned.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from fractions import Fraction

from .representable import representable_values, witnesses

#: Maximum number of complete mappings evaluated for one request.  It only
#: guards pathological inputs; when hit, callers receive an explicit error
#: rather than a partial answer.
PATTERN_CAP = 2_000_000


class SolverLimit(RuntimeError):
    """Internal combinatorial limit exceeded (never reported as a result)."""


@dataclass(frozen=True)
class PeakMapping:
    peak_index: int
    n: int
    hkl: tuple[int, int, int]
    residual: Fraction


@dataclass(frozen=True)
class Solution:
    scale_factor: Fraction
    impurity_indices: tuple[int, ...]
    mappings: tuple[PeakMapping, ...]
    skipped_representable: int
    max_abs_residual: Fraction
    sum_abs_residual: Fraction

    @property
    def impurity_count(self) -> int:
        return len(self.impurity_indices)

    def assignment_key(self) -> tuple[int, ...]:
        """Orderable peak -> N signature (-1 means impurity).

        Impurities sort before any representable N; the key always spans the
        full peak list so different impurity patterns never compare equal.
        """
        matched = {m.peak_index: m.n for m in self.mappings}
        count = len(matched) + len(self.impurity_indices)
        return tuple(matched.get(i, -1) for i in range(count))


@dataclass
class _Pattern:
    """A complete feasible mapping, before residual optimization."""

    assignment: tuple[int, ...]          # N per matched peak, in peak order
    matched_indices: tuple[int, ...]
    interval: tuple[Fraction, Fraction]
    skipped: int
    residuals: tuple[Fraction, Fraction] | None = None


@dataclass
class SolveResult:
    status: str                           # "unique" | "ambiguous" | "no_solution"
    solutions: list[Solution] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Piecewise-linear residual minimization for one fixed mapping
# ---------------------------------------------------------------------------

def _residual_minimizer(
    peaks: list[Fraction],
    pattern: _Pattern,
) -> tuple[Fraction, Fraction, Fraction]:
    """Return (c, max|res|, sum|res|) for the lex-best c in the interval.

    Each residual ``|p_i - n_i c|`` is the maximum of two affine lines; the
    max envelope is convex piecewise linear, as is the sum.  The minimizer
    of the envelope therefore lies at an interval endpoint, a zero point
    p_i/n_i, or an intersection of two envelope lines.  Among minimizers of
    the maximum, the sum is minimized at such breakpoints too.  Everything
    is computed with Fractions.
    """
    lo, hi = pattern.interval
    ns = list(pattern.assignment)
    ps = [peaks[i] for i in pattern.matched_indices]

    # lines: value(c) = slope * c + intercept
    lines: list[tuple[Fraction, Fraction]] = []
    for p, n in zip(ps, ns):
        nf = Fraction(n)
        lines.append((nf, -p))     # n*c - p   (c >= p/n)
        lines.append((-nf, p))     # p - n*c   (c <= p/n)

    candidates: set[Fraction] = {lo, hi}
    for p, n in zip(ps, ns):
        z = p / n
        if lo <= z <= hi:
            candidates.add(z)
    for a in range(len(lines)):
        s1, v1 = lines[a]
        for b in range(a + 1, len(lines)):
            s2, v2 = lines[b]
            denom = s1 - s2
            if denom == 0:
                continue
            x = (v2 - v1) / denom
            if lo <= x <= hi:
                candidates.add(x)

    best: tuple[Fraction, Fraction, Fraction] | None = None
    for c in sorted(candidates):
        if c <= 0:
            continue
        residuals = [abs(p - n * c) for p, n in zip(ps, ns)]
        rmax = max(residuals)
        rsum = sum(residuals, Fraction(0))
        score = (rmax, rsum, c)
        if best is None or score < best:
            best = score
    assert best is not None
    rmax, rsum, c = best
    return c, rmax, rsum


# ---------------------------------------------------------------------------
# Exhaustive interval-propagation search
# ---------------------------------------------------------------------------

def _edge_interval(
    peak: Fraction, n: int, tol: Fraction
) -> tuple[Fraction, Fraction]:
    """Feasible c interval: |peak - c*n| <= tol  (c > 0)."""
    low = (peak - tol) / n
    high = (peak + tol) / n
    if low < 0:
        low = Fraction(0)
    return low, high


def _window_bounds(
    values: tuple[int, ...],
    peak: Fraction,
    tol: Fraction,
    lo: Fraction,
    hi: Fraction,
    j_min: int,
    j_max: int,
) -> tuple[int, int]:
    """Integer index range of N compatible with the current c-interval.

    ``c in [lo, hi]`` and ``|peak - cN| <= tol`` imply
    ``ceil((peak-tol)/hi) <= N <= floor((peak+tol)/lo)``.  Reduced to exact
    integer bounds and located by integer bisection -- no float comparison.
    """
    ceil_low = math.ceil((peak - tol) / hi)
    a = bisect.bisect_left(values, ceil_low, j_min, j_max)
    if lo > 0:
        floor_high = math.floor((peak + tol) / lo)
        b2 = bisect.bisect_right(values, floor_high, a, j_max)
    else:
        b2 = j_max
    return a, b2


def _greedy_seed(
    edges: list[list[tuple[Fraction, Fraction]]],
    peaks: list[Fraction],
    values: tuple[int, ...],
    m: int,
    b: int,
    tol: Fraction,
) -> _Pattern | None:
    """Return one genuinely feasible mapping via leftmost interval narrowing.

    Peaks are processed in order; each retained peak is assigned the smallest
    representable value whose tolerance interval intersects the running
    c-interval, so a returned pattern is *really* feasible.  Peaks that cannot
    extend the chain are dropped as impurities (at most b).  Used only to
    activate pruning before the exhaustive DFS finds its first leaf.
    """
    r_count = len(values)

    best: _Pattern | None = None

    def run_from(start_j: int, drop_budget: int) -> _Pattern | None:
        """Leftmost-greedy chain from a fixed first value index."""
        lo, hi = edges[0][start_j]
        vidx = [start_j]
        combo = [0]
        for pi in range(1, m):
            wl, wh = _window_bounds(
                values, peaks[pi], tol, lo, hi, vidx[-1] + 1, r_count
            )
            if wl >= wh:
                if drop_budget <= 0:
                    return None
                drop_budget -= 1
                continue
            j2 = wl
            l, h = edges[pi][j2]
            lo = lo if lo >= l else l
            hi = hi if hi <= h else h
            vidx.append(j2)
            combo.append(pi)
        pat = _Pattern(
            assignment=tuple(values[jj] for jj in vidx),
            matched_indices=tuple(combo),
            interval=(lo, hi),
            skipped=vidx[-1] - vidx[0] + 1 - len(vidx),
        )
        _, rmax, rsum = _residual_minimizer(peaks, pat)
        pat.residuals = (rmax, rsum)
        return pat

    def rank(pat: _Pattern):
        return (
            m - len(pat.matched_indices),
            pat.skipped,
            pat.residuals[0],
            pat.residuals[1],
        )

    # One cheap greedy dive per possible first value index; this discovers a
    # feasible scale even when it lies near the high end of the table.
    for start_j in range(r_count):
        for drop_budget in range(b, -1, -1):
            pat = run_from(start_j, drop_budget)
            if pat is not None and (best is None or rank(pat) < rank(best)):
                best = pat
                break
    return best


def _partial_residual_lower_bounds(
    peaks: list[Fraction], pattern: _Pattern
) -> tuple[Fraction, Fraction]:
    """Independent exact minima over the feasible c-interval of a *partial*
    mapping:

      m* = min_c max_i |p_i - n_i c|   (exact breakpoint enumeration)
      s* = min_c sum_i |p_i - n_i c|   (exact n-weighted median)

    Both are unconditional lower bounds on every completion (future matches
    only add non-negative residuals), and they are computed independently so
    the lex prune (rmax, then rsum) never compares a conditional value.
    """
    _, m_star, _ = _residual_minimizer(peaks, pattern)  # rmax component exact

    lo, hi = pattern.interval
    ps = [peaks[i] for i in pattern.matched_indices]
    ns = list(pattern.assignment)
    pairs = sorted((ps[i] / ns[i], ns[i]) for i in range(len(ps)))
    total_w = sum(ns)
    half = Fraction(total_w, 2)
    acc = 0
    med = pairs[-1][0]
    for z, w in pairs:
        acc += w
        if acc >= half:
            med = z
            break
    c_star = med if lo < med < hi else (lo if med <= lo else hi)
    s_star = sum(
        (abs(p - n * c_star) for p, n in zip(ps, ns)), Fraction(0)
    )
    return m_star, s_star


def solve_indexing(
    peaks: list[Fraction],
    tolerance: Fraction,
    max_impurities: int,
    *,
    _values: tuple[int, ...] | None = None,
    _wits: dict[int, tuple[int, int, int]] | None = None,
) -> SolveResult:
    """Index peaks onto representable N values.

    The keyword table parameters exist only for exhaustive cross-checking on
    a restricted value domain; production callers leave them unset and use the
    full ``0 <= h <= k <= l <= 12`` table.
    """
    values = _values if _values is not None else representable_values()
    r_count = len(values)
    m = len(peaks)
    b = max_impurities
    wits = _wits if _wits is not None else witnesses()

    # Pre-compute every edge interval: edges[i][j] = (low, high).
    edges: list[list[tuple[Fraction, Fraction]]] = [
        [_edge_interval(peaks[i], values[j], tolerance) for j in range(r_count)]
        for i in range(m)
    ]

    def value_window(
        peak: Fraction, lo: Fraction, hi: Fraction, j_min: int, j_max: int
    ) -> tuple[int, int]:
        return _window_bounds(values, peak, tolerance, lo, hi, j_min, j_max)

    # Best lex pair (impurities, skipped) found so far drives pruning.
    best_pair: tuple[int, int] | None = None
    patterns_at_best: list[_Pattern] = []
    explored = 0
    nodes = 0

    # Best residual criteria (rmax, rsum) seen for each (impurities, skipped)
    # class, updated as complete feasible mappings are recorded.  Used only to
    # prune branches whose optimistic (impurities, skipped) class already ties
    # an incumbent class but whose partial residuals provably cannot beat it.
    # Leaf enumeration remains exhaustive within the optimal class.
    incumbent: dict[tuple[int, int], tuple[Fraction, Fraction]] = {}

    # Seed with one real feasible mapping so pruning is active immediately.
    seed = _greedy_seed(edges, peaks, values, m, b, tolerance)
    if seed is not None and seed.residuals is not None:
        sp = (m - len(seed.matched_indices), seed.skipped)
        best_pair = sp
        patterns_at_best = [seed]
        incumbent[sp] = seed.residuals

    def record(
        matched_idx: list[int],
        assign: list[int],
        skipped: int,
        lo: Fraction,
        hi: Fraction,
    ) -> None:
        nonlocal best_pair, patterns_at_best, explored
        impurities = m - len(matched_idx)
        if impurities > b:
            return
        pair = (impurities, skipped)
        if best_pair is not None and pair > best_pair:
            return
        explored += 1
        if explored > PATTERN_CAP:
            raise SolverLimit("pattern cap exceeded")
        pattern = _Pattern(
            assignment=tuple(assign),
            matched_indices=tuple(matched_idx),
            interval=(lo, hi),
            skipped=skipped,
        )
        _, rmax, rsum = _residual_minimizer(peaks, pattern)
        pattern.residuals = (rmax, rsum)
        prior = incumbent.get(pair)
        if prior is None or (rmax, rsum) < prior:
            incumbent[pair] = (rmax, rsum)
        if best_pair is None or pair < best_pair:
            best_pair = pair
            patterns_at_best = [pattern]
        else:
            patterns_at_best.append(pattern)

    def forward_look(
        i: int, j: int, lo: Fraction, hi: Fraction, d: int
    ) -> tuple[int, int] | None:
        """Relaxed forward feasibility over the still-unassigned peaks.

        Fixing the parent c-interval (not narrowing it) makes this a true
        relaxation of every completion.  Peaks are matched left-to-right to
        their smallest compatible representable value -- the canonical
        earliest-feasible matching, which maximizes the number of matches for
        ordered monotone windows and minimizes the value span (hence skipped
        values).  Returns a lower bound ``(extra_impurities, extra_skipped)``
        or None when not enough distinct values remain to meet the quota.
        """
        cursor = j + 1
        drops = 0
        gaps = 0
        matched = d
        for k in range(i + 1, m):
            wl, wh = value_window(peaks[k], lo, hi, cursor, r_count)
            if wl >= wh:
                drops += 1
                continue
            gaps += wl - cursor
            cursor = wl + 1
            matched += 1
        if matched < m - b:
            return None
        return drops, gaps

    def dfs(
        i: int,
        j: int,
        lo: Fraction,
        hi: Fraction,
        matched_idx: list[int],
        assign: list[int],
        skipped: int,
    ) -> None:
        nonlocal best_pair, patterns_at_best, explored, nodes
        nodes += 1
        if nodes > 20_000_000:
            raise SolverLimit("search node cap exceeded")

        d = len(matched_idx)

        # Relaxed lower bounds on additional impurities / skipped values.
        look = forward_look(i, j, lo, hi, d)
        if look is None:
            record(matched_idx, assign, skipped, lo, hi)
            return
        extra_drops, extra_gaps = look
        committed_imp = (i + 1) - d
        opt_imp = committed_imp + extra_drops
        opt_skip = skipped + extra_gaps
        if opt_imp > b:
            return
        if best_pair is not None and (opt_imp, opt_skip) > best_pair:
            return

        need = (m - b) - d
        remaining_values = r_count - 1 - j
        extended = False
        for i2 in range(i + 1, m):
            left_after = m - 1 - i2
            # Even if all peaks after i2 match, we cannot reach the quota.
            if d + 1 + left_after < m - b:
                break
            cur_imp = (i2 + 1) - (d + 1)
            if cur_imp > b:
                break
            j_cap = min(r_count, j + 1 + (remaining_values - need) + 1)
            win_lo, win_hi = value_window(peaks[i2], lo, hi, j + 1, j_cap)
            if win_lo >= win_hi:
                continue
            # Coarsest skip count even at the first compatible value.
            if best_pair is not None and (cur_imp, skipped + win_lo - j - 1) > best_pair:
                break  # larger i2/j2 cannot reduce impurities or skips
            for j2 in range(win_lo, win_hi):
                new_skipped = skipped + j2 - j - 1
                opt_class = (cur_imp, new_skipped)
                if best_pair is not None and opt_class > best_pair:
                    break  # larger j2 only grows the skip count
                low2, high2 = edges[i2][j2]
                new_lo = lo if lo >= low2 else low2
                new_hi = hi if hi <= high2 else high2
                if new_lo > new_hi or new_hi <= 0:
                    continue
                n2 = values[j2]
                # Residual branch-and-bound: within the incumbent optimal
                # (impurities, skipped) class, prune when the exact minimal
                # residual of the partial mapping already cannot beat it.
                # Future matches only add residuals, so the partial optimum is
                # an exact lower bound.  The same exact breakpoint solver used
                # at leaves is reused -- no approximation or float.
                if best_pair is not None and opt_class == best_pair:
                    inc = incumbent.get(best_pair)
                    if inc is not None:
                        partial = _Pattern(
                            assignment=tuple(assign + [n2]),
                            matched_indices=tuple(matched_idx + [i2]),
                            interval=(new_lo, new_hi),
                            skipped=new_skipped,
                        )
                        lb_max, lb_sum = _partial_residual_lower_bounds(
                            peaks, partial
                        )
                        if lb_max > inc[0] or (
                            lb_max == inc[0] and lb_sum > inc[1]
                        ):
                            continue
                matched_idx.append(i2)
                assign.append(n2)
                dfs(i2, j2, new_lo, new_hi, matched_idx, assign, new_skipped)
                assign.pop()
                matched_idx.pop()
                extended = True
        if not extended:
            record(matched_idx, assign, skipped, lo, hi)

    # Roots: first matched peak index i0 must be <= b (only b impurities).
    for i0 in range(min(b, m - 1) + 1):
        for j0 in range(r_count):
            lo0, hi0 = edges[i0][j0]
            if hi0 <= 0:
                continue
            if best_pair is not None and (i0, 0) > best_pair:
                break  # larger i0 only increases optimal impurities
            dfs(i0, j0, lo0, hi0, [i0], [values[j0]], 0)

    if best_pair is None or not patterns_at_best:
        return SolveResult(status="no_solution")

    # Residual minimization + global lex selection, dedupe by assignment.
    best_key: tuple[int, int, Fraction, Fraction] | None = None
    chosen: list[Solution] = []
    seen: set[tuple[int, ...]] = set()

    for pattern in patterns_at_best:
        c, rmax, rsum = _residual_minimizer(peaks, pattern)
        key = (m - len(pattern.matched_indices), pattern.skipped, rmax, rsum)
        if best_key is not None and key > best_key:
            continue
        sol = _build_solution(peaks, pattern, c, rmax, rsum, wits)
        akey = sol.assignment_key()
        if best_key is None or key < best_key:
            best_key = key
            chosen = [sol]
            seen = {akey}
        elif akey not in seen:
            seen.add(akey)
            chosen.append(sol)

    if not chosen:
        return SolveResult(status="no_solution")
    status = "ambiguous" if len(chosen) >= 2 else "unique"
    return SolveResult(status=status, solutions=chosen[:2] if status == "ambiguous" else chosen)


def _build_solution(
    peaks: list[Fraction],
    pattern: _Pattern,
    c: Fraction,
    rmax: Fraction,
    rsum: Fraction,
    wits: dict[int, tuple[int, int, int]],
) -> Solution:
    matched_set = set(pattern.matched_indices)
    mappings = tuple(
        PeakMapping(
            peak_index=i,
            n=n,
            hkl=wits[n],
            residual=abs(peaks[i] - c * n),
        )
        for i, n in zip(pattern.matched_indices, pattern.assignment)
    )
    impurities = tuple(i for i in range(len(peaks)) if i not in matched_set)
    return Solution(
        scale_factor=c,
        impurity_indices=impurities,
        mappings=mappings,
        skipped_representable=pattern.skipped,
        max_abs_residual=rmax,
        sum_abs_residual=rsum,
    )
