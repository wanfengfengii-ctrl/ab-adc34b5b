"""Exact rational arithmetic input handling.

No floating point is ever used to parse, compare or score the supplied
peak positions: decimal strings are decoded digit by digit into
``fractions.Fraction``, and JSON numbers are only accepted when they are
exact integers (the documented wire format uses decimal strings).
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

from .errors import ErrorCode

# Safety bounds so that machine generated but pathological literals
# (huge digit runs, giant exponents) cannot exhaust resources.
MAX_LITERAL_CHARS = 64
MAX_EXPONENT_ABS = 40
# Long-division budget when locating a decimal repetend.  Exceeding it
# falls back to exact ``p/q`` notation rather than looping for up to a
# denominator-sized number of steps.
MAX_EXPANSION_DIGITS = 256


class RationalParseError(ValueError):
    """Raised when a value cannot be decoded as an exact rational number."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.INVALID_RATIONAL):
        super().__init__(message)
        self.code = code


def rational_from_decimal(text: str) -> Fraction:
    """Decode a decimal literal (e.g. ``"1.5"``, ``"3/2"``) exactly.

    Supported forms:

    * optional sign followed by digits, optionally containing one
      decimal point (``"12"``, ``"-0.025"``, ``".5"``, ``"1."``);
    * an optional exponent such as ``"1.2e3"`` / ``"1.2E-2"`` — handled
      with pure integer shifts, never with floats;
    * a fraction ``"p/q"`` of two operands with a non-zero denominator
      (useful for machine generated inputs).

    Underscores, whitespace inside the token, ``NaN``/``Infinity`` and
    any other spelling are rejected.
    """

    if not isinstance(text, str):
        raise RationalParseError("rational value must be a JSON string")
    if len(text) > MAX_LITERAL_CHARS:
        raise RationalParseError("numeric literal too long")
    s = text.strip()
    if not s:
        raise RationalParseError("empty rational value")
    if any(ch.isspace() for ch in s):
        raise RationalParseError("embedded whitespace is not allowed")
    if s.count("/") == 1:
        num_s, den_s = s.split("/")
        num = _parse_decimal_operand(num_s)
        den = _parse_decimal_operand(den_s)
        if den == 0:
            raise RationalParseError("zero denominator")
        return Fraction(num, den)
    if "/" in s:
        raise RationalParseError("malformed fraction")
    return _parse_decimal_operand(s)


def _parse_decimal_operand(s: str) -> Fraction:
    if not s:
        raise RationalParseError("empty numeric operand")
    sign = 1
    body = s
    if body[0] in "+-":
        if body[0] == "-":
            sign = -1
        body = body[1:]
    if not body:
        raise RationalParseError("missing digits")

    exponent = 0
    if "e" in body or "E" in body:
        if body.count("e") + body.count("E") != 1:
            raise RationalParseError("malformed exponent")
        mantissa, _, exp_s = body.replace("E", "e").partition("e")
        body = mantissa
        if not exp_s or len(exp_s) == 1 and exp_s[0] in "+-":
            raise RationalParseError("malformed exponent")
        try:
            exponent = int(exp_s, 10)
        except ValueError:
            raise RationalParseError("malformed exponent") from None
        if abs(exponent) > MAX_EXPONENT_ABS:
            raise RationalParseError("exponent out of range")

    if body.count(".") > 1:
        raise RationalParseError("multiple decimal points")
    if "." in body:
        int_s, frac_s = body.split(".")
    else:
        int_s, frac_s = body, ""
    digits = int_s + frac_s
    if not digits or not digits.isdigit():
        raise RationalParseError("not a decimal number")

    value = int(digits, 10) * sign
    # value / 10**scale * 10**exponent, combined exactly.
    power = exponent - len(frac_s)
    if power >= 0:
        return Fraction(value * 10**power, 1)
    return Fraction(value, 10 ** (-power))


def rational_from_json(value: Any, *, field: str) -> Fraction:
    """Decode one JSON token as an exact rational.

    Strings are parsed as decimal literals.  JSON numbers are accepted
    only when integral, because a non-integral JSON ``number`` has no
    uniquely defined exact decimal value on this interface.
    """

    if isinstance(value, str):
        return rational_from_decimal(value)
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise RationalParseError(
            f"{field} must be sent as an exact decimal string"
        )
    if isinstance(value, int):
        return Fraction(value)
    raise RationalParseError(f"{field} has unsupported type")


def canonical_decimal(ratio: Fraction) -> str:
    """Render a :class:`Fraction` as its exact decimal or ``p/q`` form.

    Terminating decimals are rendered plainly (``1/2 -> "0.5"``).
    Repeating decimals use parenthesised repetend notation
    (``1/3 -> "0.(3)"``).  If the expansion would exceed
    :data:`MAX_EXPANSION_DIGITS` digits the exact fraction spelling is
    used instead; integers carry no decimal point.
    """

    if ratio.denominator == 1:
        return str(ratio.numerator)
    negative = ratio.numerator < 0
    n, d = abs(ratio.numerator), ratio.denominator
    prefix = "-" if negative else ""

    whole, rem = divmod(n, d)
    seen: dict[int, int] = {}
    digits: list[str] = []
    while rem != 0 and rem not in seen:
        seen[rem] = len(digits)
        rem *= 10
        digit, rem = divmod(rem, d)
        digits.append(str(digit))
        if len(digits) > MAX_EXPANSION_DIGITS:
            return f"{prefix}{n}/{d}"

    if rem == 0:
        frac = "".join(digits).rstrip("0")
        return f"{prefix}{whole}.{frac}" if frac else f"{prefix}{whole}"

    start = seen[rem]
    non_rep = "".join(digits[:start])
    rep = "".join(digits[start:])
    frac = f"{non_rep}({rep})" if non_rep else f"({rep})"
    return f"{prefix}{whole}.{frac}"


def dumps_canonical(payload: Any) -> bytes:
    """Deterministic JSON encoding used for request/response certificates.

    Keys sort lexicographically, separators carry no insignificant
    whitespace, and non-ASCII is emitted as UTF-8 rather than escaped.
    """

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
