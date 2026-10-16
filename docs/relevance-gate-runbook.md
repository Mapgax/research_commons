# Relevance gate — improvement runbook

How to keep the relevance gate (`docs/spec-jev-relevance-gate.md`) honest after
go-live: watch it weekly, grow the labelled set monthly, and act on what either
one shows. Two scripts and one local folder:

| Tool | Touches | What it is for |
|---|---|---|
| `scripts/relevance_gate.py drift` | reads news_db | weekly judge health |
| `scripts/relevance_eval.py sample --top-up N` | reads news_db, writes `eval/relevance/labels.csv` | monthly labelling batch |
| `scripts/relevance_eval.py run` | sends labelled links to the judge, writes `eval/relevance/` | quality on the labelled set, appended to `history.csv` |
| `scripts/relevance_gate.py rejudge` | plan: reads; `--apply`: judge calls + writes news_db | re-judge after a judge version bump |

Run all of them from the repo root, with the env loaded:
`set -a && source .env && set +a`.

> **Privacy.** `eval/` is git-ignored and must stay on this Mac. The labels are
> R&Co coverage decisions, so never commit them, paste them into a ticket or chat, or
> send them to the second brain or any other cloud service. The only data
> that leaves the machine is what the judge calls already send: title, a
> 1,000-character excerpt, ticker and company name. That goes to Anthropic, and to Gemini
> on fallback. **Never set `RELEVANCE_JUDGE=jev` or run the `jev` eval arm.**
> Both need the spec's Phase 0 privacy sign-off.

## The loop

| When | Step | Time |
|---|---|---|
| Weekly (Monday) | [Drift check](#1-weekly-drift-check) | 5 min |
| Monthly (first week) | [Top up and re-run the eval](#2-monthly-top-up-and-eval) | ~30 min of labelling |
| When the history says so | [Move the threshold](#3-changing-the-threshold) | 10 min |
| When the errors say so | [Change the judge, bump the version, re-judge](#4-changing-the-judge) | an afternoon, plus a few days of re-judging |

Record every threshold or version change in the [change log](#change-log) at
the bottom. Record only the setting and the evidence. Labels and metrics stay out of git.

## 1. Weekly drift check

```bash
python3 scripts/relevance_gate.py drift              # last 8 full weeks + this one
python3 scripts/relevance_gate.py drift --weeks 12 --threshold 0.6   # what-if
```

The check is read-only. Weeks are bucketed by `articles.published_at`. The current week is
marked `(partial)`. Links are limited to tickers in `data/ticker_universe.csv`; pass
`--all-tickers` to drop that limit. Columns:

- **judged**: share of links with a judgment. A shortfall means the daily cap
  ran out (`RELEVANCE_DAILY_CAP`) or the judge was down.
- **fail**: links whose latest attempt failed and that are still unjudged,
  as a share of the links attempted. Failed links are retried after a day.
- **p10…p90, ~0.5**: the spread of `relevance_judged`, and the share of
  judgments between 0.4 and 0.6. A judge that drifts towards 0.5 is getting
  less sure. Check this after a prompt or model change.
- **judged≥t**: share of judged links at or above the threshold.
- **articles≥t**: share of articles where at least one link's *effective*
  relevance clears the threshold. This is what the gate lets through.
- **judges**: the version strings that wrote that week's judgments. If
  `haiku-v1/gemini-…` appears, the Gemini fallback did some of the work.

The flags under "Look at:" mean "look at this week", not "act now":

| Flag | Usual cause | Action |
|---|---|---|
| `failures N% > 10%` | Anthropic outage or quota, or a malformed-response bug | Read the collect logs (`relevance judge haiku-v1: …`). If it persists, fix it. Failed links are retried by themselves. |
| `judged N% < 90%` (complete weeks after go-live) | daily cap hit, or the judge was off | Check `RELEVANCE_JUDGE` on both services. Raise `RELEVANCE_DAILY_CAP` if volume grew. |
| `pass share X vs Y` | the news mix changed (earnings season, a new source), or the judge changed | If there is no judge change that week, label more from that period in the next top-up. |
| `median p X vs Y` | the judge changed behaviour | If you didn't change it, a model update upstream is the suspect. Run the eval. |
| `new judge …` | a version bump or a model fallback | Expected after step 4. Otherwise find out why. |

Weeks before the judge's first attempt are never flagged for coverage. The
collect jobs only judge the last two days (`JUDGE_LOOKBACK` in Companies_News).

## 2. Monthly top-up and eval

```bash
python3 scripts/relevance_eval.py sample --top-up 50   # +50 links from the last 35 days
# label the new rows in eval/relevance/labels.csv (their sampled_on is today)
python3 scripts/relevance_eval.py run                  # arms prior,haiku by default
```

- `--top-up` appends to `labels.csv` and never overwrites it. It splits the new
  rows evenly across the three prior strata (0.3 / 0.6 / 1.0). It spreads them over
  sources and tickers, and skips every link already in the file, labelled or
  not. The first, full sample is still `sample` without `--top-up`.
- Label against the judge's definition (`RELEVANCE_QUESTION` in
  `research_commons/db_news/relevance.py`). A link is relevant if the article is
  primarily about the company or reports something specific to it. Lists,
  passing mentions and name collisions are `not_relevant`. Use `unsure`
  sparingly, because those rows are excluded from the metrics. Put the reason
  for a hard call in `notes`.
- Correct a label only if it was a mistake against that definition. Never
  change one because the judge disagrees.
- `run` sends every labelled link to Haiku again with the *current local
  code*. It writes `report_YYYYMMDD.md` and appends one row per arm to
  `history.csv`. The report's "Over time" table shows the last 12 runs. Use
  `--no-history` for a what-if run you don't want in the record, such as
  testing a prompt edit.

Read the history for three things:

1. **Haiku AUC against the prior AUC.** The gate has to keep beating the
   string-match prior by about 0.05 (the spec's decision rule). A narrowing gap
   means the judge is no longer worth its cost.
2. **`R@prod`, recall at the production threshold.** Keep it ≥ 0.90.
   Missing relevant news is worse than letting noise through. If it falls
   below 0.90, go to step 3.
3. **`thr r90` against `thr prod`.** `thr r90` is the highest threshold that
   still keeps recall ≥ 0.90. If it settles well above the production threshold
   over two runs, you can raise the threshold and gain precision.

Re-label, not just top up, when:

- **The definition changes.** If `RELEVANCE_QUESTION` changes in meaning, not
  just wording, old labels answer a different question. Start a new
  `labels.csv` and keep the old file next to it.
- **The coverage universe shifts.** A new sector or a large focus update
  brings new kinds of name collisions. Draw a larger top-up, for example
  `--top-up 150`, and use `--hard` for known collisions.
- **One stratum is thin.** Below about 30 used rows in a stratum, its numbers
  in the per-stratum table are noise. Top up more.
- **Drift flags a pass-share shift** that the eval doesn't reproduce. The labelled
  set no longer looks like current news.

Precision on the labelled set is not production precision. The strata are
balanced, but production has more 0.3-prior links (about 34%). Compare
thresholds and AUC across runs, not the absolute share that passes.

## 3. Changing the threshold

`RELEVANCE_THRESHOLD` (default 0.5, still the spec's guess until the first
eval) is read by the Companies_News collect jobs at runtime. It sets which
articles the gate lets through to classification (only while `RELEVANCE_GATE`
is on), and which links count in the weighted `sentiment_daily` columns.

1. Pick the value from `history.csv`, not from a single run. Choose the latest
   `thr r90`, rounded down to 0.05. Wait until at least two runs with
   `n_used` ≥ ~200 agree within 0.05. Check with
   `relevance_eval.py run --no-history --threshold <new>` that `R@prod` stays
   ≥ 0.90.
2. Check the volume effect with
   `relevance_gate.py drift --threshold <new>`. The `articles≥t` column is the
   share of articles the gate would pass.
3. Set `RELEVANCE_THRESHOLD=<new>` in Railway → Companies_News on **both**
   services, `morning-collect` and `evening-collect`, to the same value.
   Deploy the staged change. Then set it in this repo's `.env` as well, so
   `drift` and `run` report at the production value.
4. After the next run, the collect log line `relevance gate …` shows the new
   counts. Links below the threshold stay stored and unclassified. Nothing is
   deleted, so lowering the threshold later brings them back.
5. Weighted `sentiment_daily` rows are recomputed only for the days the
   collect job refreshes. For a consistent series, refresh the window
   yourself:
   `python3 -c "from datetime import date; from research_commons.db_news.writes import refresh_sentiment_daily; print(refresh_sentiment_daily(since=date(2026, 10, 1)))"`
6. Add a line to the change log.

## 4. Changing the judge

Change the judge when the eval's disagreement list shows a pattern the prompt
could fix, for example list articles judged relevant, or one name collision
again and again. Also change it when you want a different model.

Keep `HaikuRelevanceJudge` at one link per call (`batch_size=1`, the
default since haiku-v2). In batches of 10, about 7% of verdicts depended on
the neighbouring items, in both directions. Single calls are deterministic at
temperature 0. If you raise the batch size to save cost (about $0.28 instead
of $0.55 per 1k links), first re-run the batch-versus-single comparison
(`eval/relevance/event_study/batch_fix_eval.py`) and show the flips are gone.

1. Edit `_HAIKU_SYSTEM` or `RELEVANCE_QUESTION` in
   `research_commons/db_news/relevance.py`. Then bump
   **`HAIKU_JUDGE_VERSION`** (`haiku-v1` → `haiku-v2`). Bump it for a model-only
   change too, such as `LLM_PRIMARY_MODEL`. The stale check compares only the
   part of `relevance_judge` before the `/`, so without a bump nothing is
   re-judged. If the edit changes what "relevant" *means*, see "Re-label" above.
2. Evaluate locally before shipping:
   `relevance_eval.py run --no-history`. Compare with the last history row of
   the old version. Ship only if AUC is not worse and `R@prod` ≥ 0.90. Then
   run it once more *without* `--no-history`, so the new version's baseline
   is recorded.
3. Run the tests (`python3 -m pytest tests/ -q`). Commit, then push to
   `main`. **The push releases to every consumer.** Redeploy Companies_News
   `morning-collect` and `evening-collect` **without build cache**. Otherwise
   they keep the old library (the `@main` cache trap in `CLAUDE.md`). From then
   on, new links get `haiku-v2`, and the next drift shows `new judge haiku-v2/…`.
4. Re-judge the existing links:

   ```bash
   python3 scripts/relevance_gate.py rejudge --since 2026-10-01          # plan: counts per old version
   python3 scripts/relevance_gate.py rejudge --since 2026-10-01 --apply  # re-judge up to the cap
   ```

   - `--since` is required. Go back as far as you will refresh
     `sentiment_daily` (step 3.5). The gate itself only looks at the last two
     days.
   - Re-judging shares `RELEVANCE_DAILY_CAP` (5,000/day) with the collect jobs,
     which use about 2,600/day. Run it after the evening collect, or with
     `--limit`, so the collect jobs are not starved. Re-run it daily until the
     plan shows 0 stale.
   - A failed re-judge keeps the old judgment and is retried after a day.
     Links attempted within the last day are skipped.
   - Library form: `rejudge_stale_links(judge, company_names=…, since=…)`.
     The daily `judge_pending_links` is unchanged and still only picks
     unjudged links.
5. Refresh the weighted `sentiment_daily` window (step 3.5) and add a line to
   the change log.

**Rollback.** Revert the commit and push. Redeploy without cache, then re-judge
the same way, because the old version is now "other" again. To switch the gate
off entirely, unset `RELEVANCE_GATE` (the gate goes back to shadow mode) or set
`RELEVANCE_JUDGE=none`. Judgments that were already written stay, and the prior
covers everything else.

## Change log

| Date | Change | Evidence |
|---|---|---|
| 2026-10-15 | Gate live in shadow mode: `RELEVANCE_JUDGE=haiku`, `haiku-v1`, threshold 0.5 (unvalidated) | none yet; the eval has not run |
| 2026-10-16 | `haiku-v2`: one link per call (was batches of 10); text parsed by the judge, tolerating fences and prose; echoed ticker checked; output budget 512 + 48/item | Event study, 400 severity ≥ 4 links: batch-10 and single verdicts differed on 7.2% (v1) and 7.0% (v2). Single calls repeat exactly. v1 single calls failed about 50% (trailing explanation, then Gemini's empty response); v2 fails 0%. AUC vs price reaction 0.610 → 0.619. Cost about $0.55 per 1k links. |
