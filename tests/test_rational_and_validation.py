from fractions import Fraction

import pytest

from app.rational import canonical_decimal, rational_from_decimal, rational_from_json
from app.errors import ErrorCode
from app.service import ServiceError, parse_index_payload


def test_decimal_forms_are_exact():
    assert rational_from_decimal("1.5") == Fraction(3, 2)
    assert rational_from_decimal("-0.025") == Fraction(-1, 40)
    assert rational_from_decimal(".5") == Fraction(1, 2)
    assert rational_from_decimal("1.") == Fraction(1)
    assert rational_from_decimal("1.2e3") == Fraction(1200)
    assert rational_from_decimal("1.2E-2") == Fraction(12, 1000)
    assert rational_from_decimal("3/2") == Fraction(3, 2)
    assert rational_from_decimal(" 7 ") == Fraction(7)


def test_decimal_rendering():
    assert canonical_decimal(Fraction(1, 2)) == "0.5"
    assert canonical_decimal(Fraction(1, 3)) == "0.(3)"
    assert canonical_decimal(Fraction(1, 6)) == "0.1(6)"
    assert canonical_decimal(Fraction(4)) == "4"
    assert canonical_decimal(Fraction(-1, 4)) == "-0.25"


def test_rejected_literals():
    for bad in ["", "  ", "nan", "NaN", "Infinity", "1_000", "1.2.3", "1/0", "abc", "1 2"]:
        with pytest.raises(ValueError):
            rational_from_decimal(bad)


def test_float_token_is_refused():
    with pytest.raises(Exception) as exc:
        rational_from_json(1.5, field="x")
    assert exc.value.code == ErrorCode.INVALID_RATIONAL


def test_parse_enforces_count_and_order():
    with pytest.raises(ServiceError) as exc:
        parse_index_payload({"peaks": ["1", "2", "3"], "tolerance": "0.1"})
    assert exc.value.code == ErrorCode.PEAK_COUNT_OUT_OF_RANGE

    with pytest.raises(ServiceError) as exc:
        parse_index_payload({"peaks": ["1", "1", "2", "3"], "tolerance": "0.1"})
    assert exc.value.code == ErrorCode.PEAKS_NOT_STRICTLY_INCREASING

    with pytest.raises(ServiceError) as exc:
        parse_index_payload({"peaks": ["-1", "1", "2", "3"], "tolerance": "0.1"})
    assert exc.value.code == ErrorCode.PEAK_POSITION_NOT_POSITIVE

    with pytest.raises(ServiceError) as exc:
        parse_index_payload({"peaks": ["1", "2", "3", "4"], "tolerance": "0"})
    assert exc.value.code == ErrorCode.TOLERANCE_NOT_POSITIVE

    with pytest.raises(ServiceError) as exc:
        parse_index_payload(
            {"peaks": ["1", "2", "3", "4"], "tolerance": "0.1", "impurity_quota": 3}
        )
    assert exc.value.code == ErrorCode.IMPURITY_QUOTA_OUT_OF_RANGE
