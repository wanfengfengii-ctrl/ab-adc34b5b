from fractions import Fraction

from app.solver import VALUES, WITNESSES, solve


def test_representables_are_sums_of_three_squares():
    assert VALUES[0] == 1
    assert VALUES[-1] == 432
    # 7 is the classic non-representable number (Legendre 3-square).
    assert 7 not in VALUES
    assert 15 not in VALUES
    # Every value is h2+k2+l2 with normalized indices.
    for n in VALUES:
        h, k, l = WITNESSES[n]
        assert 0 <= h <= k <= l <= 12
        assert h * h + k * k + l * l == n


def test_clean_unique_solution():
    sols = solve([Fraction(x) for x in ["2", "4", "6", "8"]], Fraction("0.01"), 0)
    assert len(sols) == 1
    s = sols[0]
    assert [a.n for a in s.assignments] == [1, 2, 3, 4]
    assert s.scale_factor == 2
    assert s.max_abs_residual == 0
    assert s.sum_abs_residual == 0
    assert s.impurity_indices == ()
    assert s.omitted_representables == 0


def test_impurity_detection():
    sols = solve(
        [Fraction(x) for x in ["2", "4", "6", "8", "900"]],
        Fraction("0.01"),
        1,
    )
    assert len(sols) == 1
    s = sols[0]
    assert s.impurity_indices == (4,)
    assert [a.n for a in s.assignments] == [1, 2, 3, 4]
    assert s.scale_factor == 2


def test_ambiguity_returns_two_witnesses():
    # Peaks 152,153,154,157 are both lambda*N for N in {152,153,154,157}
    # (lambda=1) and proportional to {304,306,308,314} (lambda=1/2);
    # the two mappings tie on all four criteria.
    sols = solve(
        [Fraction(x) for x in ["152", "153", "154", "157"]],
        Fraction("1e-9"),
        0,
    )
    assert sols is not None and len(sols) == 2
    keys = sorted(s.aligned_key() for s in sols)
    assert keys == [
        (152, 153, 154, 157),
        (304, 306, 308, 314),
    ]
    for s in sols:
        assert s.omitted_representables == 1
        assert s.max_abs_residual == 0


def test_no_solution_when_ratio_too_large():
    sols = solve(
        [Fraction(x) for x in ["1", "1000", "2000", "3000"]],
        Fraction("1e-9"),
        0,
    )
    assert sols is None


def test_criterion_order_prefers_fewer_impurities_then_density():
    # Tight-tolerance pattern 1..32 with quota 2; the optimum should
    # prefer 0 impurities when one exists at wider tolerance.
    wide = solve([Fraction(x) for x in range(1, 33)], Fraction("1000"), 2)
    assert wide[0].impurity_indices == ()


def test_tolerance_is_inclusive_boundary():
    # A residual exactly equal to the tolerance is admissible.
    sols = solve([Fraction(x) for x in ["1", "2", "3", "4"]], Fraction(0), 0)
    assert sols[0].scale_factor == 1
    edge = solve(
        [Fraction(x) for x in ["1.5", "2", "3", "4"]], Fraction("0.5"), 0
    )
    assert edge is not None
