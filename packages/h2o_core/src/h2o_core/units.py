"""Quantities, and which of them may be compared.

ADR-002 step 5 requires unit normalisation to be **dimension-safe by
construction**: comparing across dimensions is an error, not a conversion.

h2o's version of that rule is calendar time. "Every 6 months" and "4,000 hours"
are both durations and are *not* interconvertible, because a month is not a
fixed number of hours. Treating them as one dimension would let the pipeline
declare a contradiction between two documents that do not disagree, or hide one
that do. This is the same trap as micrograms versus IU in a nutrition corpus:
the units look like the same kind of thing and are not.

Temperature is the other one. C and F are separate dimensions here because an
affine conversion is right for an absolute reading and wrong for a difference,
and nothing in a document says which it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

__all__ = ["Quantity", "parse_number", "parse_quantity", "parse_unit"]

#: unit token -> (dimension, factor to the dimension's canonical unit)
_UNITS: dict[str, tuple[str, Decimal]] = {
    # Exact durations. Convertible among themselves, never to calendar time.
    "s": ("duration_exact", Decimal(1)),
    "sec": ("duration_exact", Decimal(1)),
    "second": ("duration_exact", Decimal(1)),
    "seconds": ("duration_exact", Decimal(1)),
    "min": ("duration_exact", Decimal(60)),
    "minute": ("duration_exact", Decimal(60)),
    "minutes": ("duration_exact", Decimal(60)),
    "h": ("duration_exact", Decimal(3600)),
    "hr": ("duration_exact", Decimal(3600)),
    "hour": ("duration_exact", Decimal(3600)),
    "hours": ("duration_exact", Decimal(3600)),
    "d": ("duration_exact", Decimal(86400)),
    "day": ("duration_exact", Decimal(86400)),
    "days": ("duration_exact", Decimal(86400)),
    "wk": ("duration_exact", Decimal(604800)),
    "week": ("duration_exact", Decimal(604800)),
    "weeks": ("duration_exact", Decimal(604800)),
    # Calendar durations. A year is exactly twelve months by definition; a month
    # is not any fixed number of hours, which is the whole point.
    "mo": ("duration_calendar", Decimal(1)),
    "month": ("duration_calendar", Decimal(1)),
    "months": ("duration_calendar", Decimal(1)),
    "maand": ("duration_calendar", Decimal(1)),
    "maanden": ("duration_calendar", Decimal(1)),
    "y": ("duration_calendar", Decimal(12)),
    "yr": ("duration_calendar", Decimal(12)),
    "year": ("duration_calendar", Decimal(12)),
    "years": ("duration_calendar", Decimal(12)),
    "jaar": ("duration_calendar", Decimal(12)),
    # Flow, canonical L/min.
    "l/min": ("flow", Decimal(1)),
    "lpm": ("flow", Decimal(1)),
    "l/h": ("flow", Decimal(1) / Decimal(60)),
    "l/hr": ("flow", Decimal(1) / Decimal(60)),
    # Pressure, canonical bar.
    "bar": ("pressure", Decimal(1)),
    "mbar": ("pressure", Decimal("0.001")),
    "kpa": ("pressure", Decimal("0.01")),
    "mpa": ("pressure", Decimal(10)),
    "psi": ("pressure", Decimal("0.0689476")),
    # Volume, canonical L.
    "l": ("volume", Decimal(1)),
    "litre": ("volume", Decimal(1)),
    "litres": ("volume", Decimal(1)),
    "liter": ("volume", Decimal(1)),
    "liters": ("volume", Decimal(1)),
    "ml": ("volume", Decimal("0.001")),
    "m3": ("volume", Decimal(1000)),
    # Energy, canonical kWh. UCUM writes this "kW.h", and the OTEL mapper
    # compares an instrument's declared unit against a claim's, so both spellings
    # have to land on one dimension.
    "kwh": ("energy", Decimal(1)),
    "kw.h": ("energy", Decimal(1)),
    "wh": ("energy", Decimal("0.001")),
    # Ratio and counts.
    "%": ("ratio", Decimal(1)),
    "percent": ("ratio", Decimal(1)),
    "{bottle}": ("count", Decimal(1)),
    "bottle": ("count", Decimal(1)),
    "bottles": ("count", Decimal(1)),
    "cycle": ("count", Decimal(1)),
    "cycles": ("count", Decimal(1)),
    # Separate dimensions on purpose. See the module docstring.
    "c": ("temperature_c", Decimal(1)),
    "°c": ("temperature_c", Decimal(1)),
    "f": ("temperature_f", Decimal(1)),
    "°f": ("temperature_f", Decimal(1)),
    # Money, canonical major units. The spec sheet carries a price and
    # check_corpus already asserts a citation quotes it readably.
    "£": ("money_gbp", Decimal(1)),
    "gbp": ("money_gbp", Decimal(1)),
    "€": ("money_eur", Decimal(1)),
    "eur": ("money_eur", Decimal(1)),
}

_NUMBER = re.compile(r"[-+]?\d[\d.,]*")
_QUANTITY = re.compile(
    r"(?P<sym>[£€])?\s*(?P<num>[-+]?\d[\d.,]*)\s*(?P<unit>[^\s\d]*(?:/[a-z]+)?)", re.I
)

#: Values that state an absence rather than a measurement. Two documents saying
#: "not specified" do not disagree.
_ABSENCE = {"", "-", "n/a", "na", "none", "not specified", "unspecified", "varies", "tbd"}


@dataclass(frozen=True)
class Quantity:
    """A number, its dimension, and its value in that dimension's canonical unit."""

    value: Decimal
    unit: str | None
    dimension: str | None
    canonical: Decimal | None
    approximate: bool = False

    def comparable_with(self, other: Quantity) -> bool:
        """Whether these two may be compared at all.

        Two values with no dimension are comparable as bare numbers. Otherwise
        the dimensions must match exactly -- there is no conversion path between
        dimensions, by construction, because that is what makes a false
        contradiction impossible rather than unlikely.
        """
        if self.dimension is None and other.dimension is None:
            return True
        return self.dimension is not None and self.dimension == other.dimension


def parse_number(text: str) -> Decimal | None:
    """Parse a number written the English or the continental way.

    "4,500 litres" is four and a half thousand; "2,4 L/min" is two point four.
    The installation manual writes the first and Dutch sources write the second,
    so guessing wrong is a factor-of-a-thousand error in a stored fact.

    The rule: a comma followed by exactly three digits, with no other separator
    in play, is a thousands separator. Anything else is a decimal comma.
    """
    cleaned = text.strip().replace(" ", "")
    if not cleaned:
        return None

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        # Whichever comes last is the decimal separator.
        cleaned = (
            cleaned.replace(",", "")
            if cleaned.rfind(".") > cleaned.rfind(",")
            else cleaned.replace(".", "").replace(",", ".")
        )
    elif has_comma:
        parts = cleaned.split(",")
        thousands = len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and len(parts[0]) <= 3
        cleaned = cleaned.replace(",", "") if thousands else cleaned.replace(",", ".")

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_unit(token: str) -> tuple[str, Decimal] | None:
    """Map a unit as written to a dimension and a conversion factor."""
    key = token.strip().casefold().rstrip(".")
    if key in _UNITS:
        return _UNITS[key]
    key = key.replace("per ", "/").replace(" ", "")
    return _UNITS.get(key)


def parse_quantity(text: str, unit_hint: str | None = None) -> Quantity | None:
    """Parse a value as written in a document.

    Returns None for an absence marker, so "not specified" never enters a
    comparison as if it were a measurement.
    """
    raw = (text or "").strip()
    if raw.casefold() in _ABSENCE:
        return None

    approximate = bool(
        re.search(r"\b(about|approx\.?|approximately|roughly|around|~)\b|~", raw, re.I)
    )

    match = _QUANTITY.search(raw)
    if not match:
        return None

    number = parse_number(match.group("num"))
    if number is None:
        return None

    token = (match.group("unit") or "").strip() or match.group("sym") or unit_hint or ""
    resolved = parse_unit(token) if token else None

    if resolved is None:
        return Quantity(
            value=number,
            unit=token or None,
            dimension=None,
            canonical=None,
            approximate=approximate,
        )

    dimension, factor = resolved
    return Quantity(
        value=number,
        unit=token,
        dimension=dimension,
        canonical=number * factor,
        approximate=approximate,
    )
