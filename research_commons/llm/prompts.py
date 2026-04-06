"""Named prompt templates shared across the three projects.

Adding a prompt here (rather than inlining one inside a project) is the
contract for "I expect another project to use this exact wording".

Each template is a plain string with named ``{}`` placeholders so callers can
``template.format(**kwargs)``. Keep them short and version them in the docstring
when you change wording.
"""

from __future__ import annotations

# v1 — 2026-04: classify a single news article into event_type / severity / sentiment.
ARTICLE_CLASSIFY_V1 = """\
You are a financial news analyst. Read the article below and return a JSON object
with the following keys:

  - event_type:    one of [earnings, guidance, mna, regulatory, macro, product, lawsuit, other]
  - severity:      integer 1..5 (1 = minor, 5 = market-moving)
  - sentiment:     float in [-1.0, 1.0]
  - summary:       at most 2 sentences in English
  - tickers:       array of stock ticker symbols mentioned (use the official symbol)
  - themes:        array of high-level themes (e.g. "AI", "supply chain")

Return JSON only, no markdown fences.

Title: {title}
Source: {source}
Published: {published_at}

Body:
{body}
"""


# v1 — 2026-04: classify an investment-idea document for the Idee_Scraping pipeline.
IDEA_CLASSIFY_V1 = """\
You are a thematic equity analyst. Given the document text below, return a JSON
object with:

  - thesis:        one sentence summarising the investment thesis
  - asset_class:   one of [equity, bond, commodity, fx, crypto, other]
  - region:        one of [US, EU, UK, CH, JP, CN, EM, GLOBAL, other]
  - tickers:       array of ticker symbols explicitly mentioned
  - themes:        array of themes
  - confidence:    float 0..1 (your confidence the thesis is well-supported)

Return JSON only.

Document:
{document_text}
"""


# v1 — 2026-04: produce a daily briefing in HTML for the Companies_News pipeline.
DAILY_BRIEFING_V1 = """\
You are a sell-side research analyst writing a concise daily briefing for a
portfolio manager. Use the structured ticker data below to write at most 200
words of commentary in clear, professional English. Highlight surprises and
flag any items with severity >= 4.

Date: {as_of}
Tickers covered: {tickers}

Per-ticker JSON payloads:
{payload_json}
"""
