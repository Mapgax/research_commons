# Ticker universe — data dictionary

What every field in the two canonical universe files means, who writes it, and
what it actually causes to happen downstream.

Both files live in `data/` in this repo, are git-versioned, and are `;`-delimited.
Last reviewed: 2026-08-23.

---

## The two axes

The single most important thing to understand: **coverage and delivery are
separate, independent axes.** They live in different files on purpose.

| | file | question it answers | who consumes it |
|---|---|---|---|
| **Coverage** | `data/ticker_universe.csv` → `scope_status` | *what data do we collect and compute for this company?* | MSARN pipeline, Companies_News collection, risk_monitor |
| **Delivery** | `data/email_focus_tickers.csv` → `email_scope` + PM/advisory flags | *who receives this company in the briefing email?* | Companies_News email only |

A ticker can be fully covered and never emailed, or emailed regardless of how
little we collect for it. Neither file derives from the other. The audience
flags used to be columns on the universe file; they were retired from there on
2026-07-29 and reinstated as their own file the same day, precisely because
collapsing the two axes into one dial lost information the email pipeline
needed.

> **Historical note.** On 2026-08-23 the audience flags were used *once*, by
> hand, as the input signal for a coverage re-tiering (see "Amendment log"
> below). That was a one-off manual decision, not a standing rule, and it did
> not create a link between the files. Nothing reads one to compute the other.

---

## `data/ticker_universe.csv` — the canonical universe

496 rows as of 2026-08-23. Mirrored into `market.ticker_metadata` by
`python3 -m research_commons.universe sync-db`, and distributed as a trimmed
`tickers.csv` into each consumer repo by `... distribute --apply`.

| column | meaning | notes |
|---|---|---|
| `company_name` | Display name | Free text. Not a key. |
| `ISIN` | **The merge key.** | Every ingest matches on this, never on the ticker. Two placeholder rows (`TSLA`, `XOM`) have an empty ISIN — they are seed rows kept so FK'd price history survives, and they cannot participate in any ISIN join. |
| `bloomberg_ticker` | e.g. `NOVN SW Equity` | Source for the derived Yahoo symbol and currency when those columns are blank. |
| `ticker` | Logical internal symbol | The DB primary key across six FK'd tables. **Never hand-edit** — use `universe rename-ticker`, which relies on `ON UPDATE CASCADE`. Hand-editing orphans the history. |
| `sector_name` | GICS-style sector | Used for grouping in dashboards and features. |
| `currency` | ISO 4217 **trading** currency | Never auto-refreshed from uploads. Upload files carry `DenCurrency`, a *denomination* currency that is frequently wrong about where the line actually trades (EUR for LSE names, USD for Hong Kong lines). Mismatches are reported, not applied. |
| `yahoo_symbol` | Authoritative Yahoo Finance symbol | Overrides the Bloomberg-derived guess. Getting this wrong is the single most common cause of a ticker silently collecting nothing. |
| `scope_status` | **The coverage dial.** `active` / `inactive` / `watchlist` | See the next section — this is the field with real consequences. |
| `added_date` | When the row entered the universe | |
| `superseded_isins` | Prior ISINs after a corporate action | Keeps history attached across an ISIN change. |
| `notes` | Free text | Where the "why" of an unusual row lives. Read it before acting on a surprising row. |

### `scope_status` — what each tier actually costs and gets

The tier → stage mapping is defined in exactly one place:
[`research_commons/scope_policy.py`](../research_commons/scope_policy.py).
Import `scopes_for(stage)` / `includes(stage, scope_status)`; never restate the
sets locally.

| stage | `active` | `inactive` | `watchlist` |
|---|:--:|:--:|:--:|
| `prices` | ✅ | ✅ | ✅ |
| `fundamentals` | ✅ | ✅ | ✅ |
| `features` | ✅ | ✅ | — |
| `altdata` (Google Trends, EDGAR) | ✅ | ✅ | — |
| `options` | ✅ | ✅ | — |
| `news` (+ LLM classification) | ✅ | ✅ | — |
| `scoring` (model inference, daily overview merge) | ✅ | — | — |

Read in plain language:

- **`active`** — everything, plus model scoring. `scoring` is the *only* stage
  that separates active from inactive; on every other axis the two are
  identical. Currently 73 names.
- **`inactive`** — collected and feature-engineered on exactly the same cadence
  as active, but never scored. This is the bulk of the universe (368) and is
  not a dormant tier despite the name.
- **`watchlist`** — prices and fundamentals only. Quant-analysable for equity
  research, cheap to hold, instant to promote. No features, no alt-data, no
  options, no news, no LLM spend. Currently 55 names.

Counts as of 2026-08-23: **73 active / 368 inactive / 55 watchlist**.

### Promotion and demotion

Changing a tier is a **data edit to this file**, followed by `sync-db` and
`distribute --apply`. It is not a code change.

- Promoting `watchlist` → `inactive` or `active` gives a name features, news
  and enrichment starting on the next nightly run. If its price history is
  stale, `step_prices_with_backfill`'s recency arm in MSARN closes the gap
  automatically (added 2026-08-23).
- Demoting to `watchlist` stops features, alt-data, options and news for that
  name. **Existing rows are not deleted** — they simply stop being refreshed
  and go stale in place.
- `ingest --new-status` only applies to brand-new ISINs. It silently no-ops on
  names already in the file; retag those by editing this file and running
  `sync-db`.

---

## `data/email_focus_tickers.csv` — the delivery audience

441 rows as of 2026-08-23. Merged from PM/advisory uploads by
`python3 -m research_commons.universe update-email-focus [file] --apply`, and
distributed to targets flagged `email_focus=true` in `universe_targets.toml`.

| column | meaning |
|---|---|
| `ISIN` | Merge key against the universe file. |
| `company_name` | Display name (informational). |
| `scope_pm_ch` | On the Swiss PM audience list. |
| `scope_pm_de` | On the German PM audience list. |
| `scope_advisory` | On the advisory audience list. |
| `email_scope` | **The delivery dial.** Companies_News reads *this*, not `scope_status`, to decide who is in the briefing email. |

Rules that surprise people:

- An ISIN **absent** from this file counts as `0` for all four flags — i.e.
  excluded from the email by default. Absence is meaningful, not neutral.
- `email_scope` is a real, independently-editable column. It is populated equal
  to `scope_pm_ch` by convention today, but the file is the source of truth and
  the two are allowed to diverge.
- On removal from an upload, the command **asks** whether to keep or zero the
  current values — except `scope_advisory`, which always resets to `0` with no
  prompt. A non-interactive stdin (cron) defaults to "keep".
- Companies_News reads `email_scope` via `load_email_focus_tickers()` /
  `email_focus_delivery_tickers()`. It does **not** consult `scope_status` for
  delivery.

---

## Amendment log

**2026-07-29** — `scope_pm_ch` / `scope_pm_de` / `scope_advisory` retired as
columns on `ticker_universe.csv` (values archived in
`data/archive/retired_scope_flags_20260729.csv`) and reinstated the same day as
the standalone `email_focus_tickers.csv`, after the retirement turned out to
remove audience targeting the email pipeline still needed.

**2026-08-22** — 300 names moved `watchlist` → `inactive` (commit `6327514`),
tripling the covered universe from 188 to 488. 194 of them arrived carrying
price history that had been bootstrapped once and then frozen (140 at exactly
2026-04-17), and 101 had no price rows at all. This exposed two gaps in MSARN,
both fixed on 2026-08-23: watchlist names were never refreshed after their
one-time bootstrap, and the nightly catch-up window was one global date, so a
name joining with older history kept its gap permanently.

**2026-08-23** — `scope_policy.py` introduced as the single definition of the
tier → stage mapping, replacing six hand-synced copies across three repos.
Watchlist gained recurring prices + fundamentals.

**2026-08-23** — One-time re-tier: 47 rows moved `inactive` → `watchlist`,
namely those carrying no PM and no advisory flag in `email_focus_tickers.csv`
(all 47 were absent from that file entirely). Full list:
`data/archive/retier_inactive_to_watchlist_20260823.csv`. Deliberately limited
to demotions — no promotions to `active`, and no change to any row already
`active` or `watchlist`. The two ISIN-less placeholder rows (`TSLA`, `XOM`)
were excluded, since they cannot be evaluated against an ISIN-keyed file and
demoting them would have been a join artifact rather than a decision.
