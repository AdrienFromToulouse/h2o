"""Quantities, and the dimensions that must never be bridged."""

from decimal import Decimal

import pytest
from h2o_core.units import parse_number, parse_quantity, parse_unit


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        # The installation manual writes this. Read as a decimal comma it would
        # be 4.5 litres instead of 4,500 -- a factor of a thousand in a stored
        # fact, silently.
        ("4,500", Decimal(4500)),
        # Dutch sources and the spec sheet write this. Read as thousands it
        # would be 24.
        ("2,4", Decimal("2.4")),
        ("1,249.00", Decimal("1249.00")),
        ("1.234,56", Decimal("1234.56")),
        ("6", Decimal(6)),
        ("0.8", Decimal("0.8")),
    ],
)
def test_the_decimal_comma_trap(written: str, expected: Decimal) -> None:
    assert parse_number(written) == expected


def test_calendar_time_is_not_exact_time() -> None:
    """h2o's micrograms-versus-IU.

    A month is not a fixed number of hours, so these are different dimensions
    and there is no conversion path between them. Making them one dimension
    would let the pipeline declare a contradiction between two documents that
    do not disagree.
    """
    months = parse_quantity("6 months")
    hours = parse_quantity("4000 hours")

    assert months.dimension == "duration_calendar"
    assert hours.dimension == "duration_exact"
    assert not months.comparable_with(hours)


def test_a_year_is_exactly_twelve_months() -> None:
    """Convertible *within* calendar time, because that one is true by
    definition rather than by approximation."""
    assert parse_quantity("1 year").canonical == parse_quantity("12 months").canonical


def test_celsius_and_fahrenheit_are_separate_dimensions() -> None:
    """An affine conversion is right for an absolute reading and wrong for a
    difference, and nothing in a document says which it is."""
    assert not parse_quantity("5 °C").comparable_with(parse_quantity("41 °F"))


def test_flow_converts_exactly() -> None:
    """The spec sheet gives chilled output in L/h and dispense rate in L/min."""
    assert parse_quantity("2.4 L/min").canonical == parse_quantity("144 L/h").canonical


def test_ucum_spellings_land_on_one_dimension() -> None:
    """The OTEL mapper compares an instrument's declared unit against a claim's,
    and the fixture writes energy the UCUM way."""
    assert parse_unit("kW.h") == parse_unit("kWh")
    assert parse_quantity("3 kW.h").comparable_with(parse_quantity("3000 Wh"))
    assert parse_unit("{bottle}")[0] == "count"
    assert parse_unit("%")[0] == "ratio"


@pytest.mark.parametrize("absent", ["not specified", "n/a", "-", "varies", ""])
def test_an_absence_is_not_a_measurement(absent: str) -> None:
    """Two documents saying "not specified" do not disagree."""
    assert parse_quantity(absent) is None


@pytest.mark.parametrize(
    "hedged",
    ["about 6 months", "approx. 2.4 L/min", "roughly 1.2 L/min", "~18 L/h", "around 5 bar"],
)
def test_hedged_values_are_marked_approximate(hedged: str) -> None:
    """A threshold or an estimate must not create a contradiction with a
    specification, so the hedge travels with the value."""
    quantity = parse_quantity(hedged)
    assert quantity is not None
    assert quantity.approximate


def test_a_bare_number_keeps_its_value_and_claims_no_dimension() -> None:
    """Not everything extracted has a unit, and inventing one would be worse
    than having none."""
    quantity = parse_quantity("12")

    assert quantity.value == Decimal(12)
    assert quantity.dimension is None
    assert quantity.comparable_with(parse_quantity("14"))


def test_money_parses_from_the_symbol() -> None:
    """The spec sheet's price, which check_corpus already asserts is quoted
    readably rather than as an entity."""
    price = parse_quantity("£1,249.00")

    assert price.value == Decimal("1249.00")
    assert price.dimension == "money_gbp"
    assert not price.comparable_with(parse_quantity("1249 EUR"))
