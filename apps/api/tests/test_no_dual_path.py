"""h2o has one code path, and one dispatcher that decides who calls it.

This is mechanised rather than reviewed because it is the kind of rule that
erodes one convenient branch at a time. The first `if IS_LAMBDA` outside the
dispatcher is always reasonable in isolation; it is the tenth that produces a
system tested in one configuration and shipped in another.
"""

from __future__ import annotations

from pathlib import Path

API = Path(__file__).resolve().parents[1] / "h2o_api"
CORE = Path(__file__).resolve().parents[3] / "packages" / "h2o_core" / "src" / "h2o_core"

#: The dispatcher itself, and the module that defines the flag.
ALLOWED = {"dispatch.py", "config.py"}


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_is_lambda_is_read_only_in_the_dispatcher() -> None:
    offenders = [
        path.relative_to(API)
        for path in _sources(API)
        if path.name not in ALLOWED and "IS_LAMBDA" in path.read_text()
    ]

    assert not offenders, (
        f"IS_LAMBDA reached {offenders}. Deciding *who calls* the work belongs in "
        "dispatch.py; deciding *what the work does* belongs nowhere."
    )


def test_the_core_library_cannot_tell_where_it_is_running() -> None:
    """h2o_core is handed clients and told nothing about its deployment.

    This is what makes the dispatcher possible: if a fan-out step could ask
    whether it was inside Step Functions, the two arms would stop being the
    same call.
    """
    offenders = [
        path.name
        for path in _sources(CORE)
        if "AWS_LAMBDA_FUNCTION_NAME" in (text := path.read_text()) or "IS_LAMBDA" in text
    ]

    assert not offenders, f"h2o_core learned where it runs, in {offenders}"
