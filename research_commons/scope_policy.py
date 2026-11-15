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

  - ``active``    — everything, and scored on the DAILY cadence.
  - ``inactive``  — everything active gets, but scored only on the weekly
    cadence. Collection, features, alt-data, options and news are identical.
  - ``watchlist`` — every public, unmetered stage (prices, fundamentals,
    features, options, EDGAR insider data) but no Google Trends, no news and
    no scoring. Fully quant-analysable, cheap to hold, instant to promote.
  - ``delisted``  — nothing. The row stays in the canonical universe only so
    its ISIN is still known: a later universe upload that still lists the
    name merges onto it instead of re-adding it as watchlist, and a focus
    upload cannot promote it. Added 2026-11-15 for names whose listing is gone
    (take-privates, mergers) and that Yahoo no longer quotes.

Corrected 2026-08-27: this table used to say ``scoring`` was ``active``-only
and that inactive was "never scored". MSARN actually scores the whole covered
universe every Sunday (``data_export/overview.py`` SCOPE_FULL, launchd
``full-refresh``) and only the daily merge (SCOPE_FOCUS) is active-only. The
active/inactive line is a scoring *cadence*, not scoring at all — which is why
there are two stages below rather than one.

Email delivery is deliberately **not** in this table. It is a separate axis
owned by ``data/email_focus_tickers.csv``, and being ``active`` does not imply
delivery. There is one directed link the other way, enforced by
``universe/email_focus.py`` rather than by this table: an emailed name must sit
in a tier that scores daily. See docs/UNIVERSE_DICTIONARY.md, "The two axes".
"""

from __future__ import annotations

ACTIVE = "active"
INACTIVE = "inactive"
WATCHLIST = "watchlist"
DELISTED = "delisted"

ALL_SCOPES: frozenset[str] = frozenset({ACTIVE, INACTIVE, WATCHLIST, DELISTED})

# stage -> the scope_status values that stage runs for. DELISTED is in none.
STAGE_SCOPES: dict[str, frozenset[str]] = {
    # Public, unmetered sources (Yahoo, SEC EDGAR) and local compute: every
    # tier gets them, watchlist included, so a staged name is fully
    # quant-analysable — prices, fundamentals, the engineered feature set,
    # option metrics and Form 4 insider counts. Widened 2026-11-14 from
    # prices + fundamentals: the original line was drawn by API budgets, and
    # none of these stages has one (the extra ~56 names cost ~13% more nightly
    # runtime and ~0.2 GB of features storage).
    "prices": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    "fundamentals": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    "features": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    "options": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    # SEC EDGAR Form 4 on its own (no key, 10 req/s fair-access limit).
    "insider": frozenset({ACTIVE, INACTIVE, WATCHLIST}),
    # Covered-only: a real budget. "altdata" is the combined Google Trends +
    # EDGAR collection; Google Trends is rate-limited hard enough that only
    # ~120 of ~440 covered names get a value per day already. "news" spends
    # news-API and LLM budget.
    "altdata": frozenset({ACTIVE, INACTIVE}),
    "news": frozenset({ACTIVE, INACTIVE}),
    # Model inference. Every covered name is scored — the weekly SCOPE_FULL
    # overview rebuild covers active + inactive alike. Watchlist has features
    # but is not on the risk board.
    "scoring": frozenset({ACTIVE, INACTIVE}),
    # The one distinction active/inactive actually carries: the fast daily
    # overview merge (SCOPE_FOCUS), which is what keeps a name's risk numbers
    # same-day fresh instead of up-to-a-week old.
    "scoring_daily": frozenset({ACTIVE}),
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
