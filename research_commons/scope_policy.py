"""Which pipeline stages run for which ``scope_status`` tier.

This module is the *single* definition of the coverage policy. Before it, the
rule was restated in six places — MSARN's ``COVERED_SCOPES`` /
``load_covered_registry()`` / ``_load_scope_tickers("active")``, Companies_News'
``allowed_statuses={"active", "inactive"}``, ``data_export/overview.py``'s
``SCOPE_FOCUS``/``SCOPE_FULL``, and the backfill workflow's YAML defaults — so
"what does inactive actually get?" had six answers that had to be kept in sync
by hand. Consumers should import the meaning from here rather than restate it.

Deliberately pure: no DB, no pandas, no I/O. Consumer runtime is
CSV-authoritative (each repo reads its generated ``tickers.csv``), so a policy
lookup must not require a database round-trip the way
``tickers.load_registry()`` does.

Scope tiers, in terms of what they actually gate:

  - ``active``    — everything, plus model scoring (the daily overview merge).
  - ``inactive``  — collected and feature-engineered, but never scored.
  - ``watchlist`` — prices and fundamentals only: quant-analysable, but no
    features, alt-data, options or news. Cheap to hold, instant to promote.

Email delivery is deliberately **not** in this table. It is a genuinely
independent axis owned by ``data/email_focus_tickers.csv`` — a ticker can be
emailed regardless of its coverage tier, and being ``active`` does not imply
delivery.
"""

from __future__ import annotations

ACTIVE = "active"
INACTIVE = "inactive"
WATCHLIST = "watchlist"

ALL_SCOPES: frozenset[str] = frozenset({ACTIVE, INACTIVE, WATCHLIST})

# stage -> the scope_status values that stage runs for.
STAGE_SCOPES: dict[str, frozenset[str]] = {
    # Cheap, free (yfinance), and the basis for any quant analysis — so every
    # tier gets it, including watchlist. Without this a watchlist name is
    # bootstrapped once and then rots, which is what this table was written to fix.
    "prices": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    "fundamentals": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    # Covered-only: real per-ticker cost (API budgets, runtime) and only useful
    # once a name is genuinely researched.
    "features": frozenset({ACTIVE, INACTIVE}),
    "altdata": frozenset({ACTIVE, INACTIVE}),
    "options": frozenset({ACTIVE, INACTIVE}),
    "news": frozenset({ACTIVE, INACTIVE}),
    # The one distinction active/inactive actually carries today: model
    # inference and the fast daily overview merge.
    "scoring": frozenset({ACTIVE}),
}


def normalize_scope(raw: str | None) -> str:
    """Fold a raw ``scope_status`` value to a known tier.

    Unknown or empty values fall back to ``inactive`` rather than ``active``,
    so a malformed row never silently opts a ticker into scoring.
    """
    value = str(raw or "").strip().lower()
    return value if value in ALL_SCOPES else INACTIVE


def scopes_for(stage: str) -> frozenset[str]:
    """Return the scope tiers ``stage`` runs for.

    Raises ``KeyError`` for an unknown stage — a typo'd stage name must fail
    loudly rather than quietly resolve to an empty universe.
    """
    key = str(stage or "").strip().lower()
    try:
        return STAGE_SCOPES[key]
    except KeyError:
        raise KeyError(
            f"Unknown pipeline stage: {stage!r}. Known stages: {sorted(STAGE_SCOPES)}"
        ) from None


def includes(stage: str, scope_status: str | None) -> bool:
    """Return True if ``stage`` should run for a ticker in ``scope_status``."""
    return normalize_scope(scope_status) in scopes_for(stage)


def stages_for(scope_status: str | None) -> frozenset[str]:
    """Return every stage that runs for a ticker in ``scope_status``."""
    scope = normalize_scope(scope_status)
    return frozenset(stage for stage, scopes in STAGE_SCOPES.items() if scope in scopes)
