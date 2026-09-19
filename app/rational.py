"""Exact rational parsing.

Peak positions and tolerances are *rational* numbers.  The service never
performs float comparison: JSON floating point literals are rejected and
callers must provide integers, decimal/``p/q`` strings, or explicit
``{"num": p, "den": q}`` objects.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any


class RationalError(ValueError):
    """Raised when a value cannot be interpreted as an exact rational."""


def parse_rational(value: Any, *, name: str = "value") -> Fraction:
    """Parse ``value`` into an exact :class:`Fraction`.

    Accepted forms::

        42                 # JSON integer
        "1.5"              # decimal string
        "3/7"              # fraction string
        {"num": 3, "den": 7}

    JSON non-integer numbers (floats) are rejected so that no precision is
    ever lost before the arithmetic layer.
    """
    if isinstance(value, bool):
        raise RationalError(f"{name} must be a rational number, not boolean")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        raise RationalError(
            f"{name} must be supplied as an integer, string, or "
            "{num,den} object; floating point literals are forbidden"
        )
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise RationalError(f"{name} must be a non-empty rational string")
        try:
            return Fraction(text)
        except (ZeroDivisionError, ValueError) as exc:
            raise RationalError(f"{name} is not a valid rational: {value!r}") from exc
    if isinstance(value, dict):
        num = value.get("num")
        den = value.get("den", 1)
        if not isinstance(num, int) or isinstance(num, bool):
            raise RationalError(f"{name}.num must be an integer")
        if not isinstance(den, int) or isinstance(den, bool):
            raise RationalError(f"{name}.den must be an integer")
        if den == 0:
            raise RationalError(f"{name}.den must be non-zero")
        return Fraction(num, den)
    raise RationalError(
        f"{name} must be an integer, string, or {{num,den}} object, "
        f"got {type(value).__name__}"
    )


def rational_to_json(value: Fraction) -> str:
    """Serialize a Fraction as a canonical exact string."""
    return str(value)
