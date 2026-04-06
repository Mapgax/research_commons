"""URL canonicalization, content hashing, and language detection.

These three primitives make article deduplication work across the three projects.

Learning note — why SHA-256 for dedup instead of a DB UNIQUE on url?
    URLs for the same article vary wildly across news aggregators (tracker params,
    redirects, AMP versions). Content-hashing on (canonical_url + title + body)
    means: if two fetchers retrieve the same story through different URLs we still
    only store it once. The trade-off is that the hash must be stable — i.e. the
    normalisation logic here must NEVER change without also rehashing existing rows.
    That's why the hash definition is documented as "frozen" in writes.py.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urldefrag, urlsplit, urlunsplit


# Query parameters that are pure tracking noise — strip them before hashing.
_STRIP_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name",
    "fbclid", "gclid", "msclkid", "dclid",
    "ref", "referrer", "source",
    "_ga", "_gl",
})


def canonicalize_url(url: str) -> str:
    """Normalize a URL so that different links to the same article map to one string.

    Steps (in order):
    1. Drop the fragment (#...).
    2. Lower-case scheme and host.
    3. Strip tracking query params (utm_*, fbclid, gclid, …).
    4. Sort remaining query params alphabetically.
    5. Remove trailing slash from the path (unless the path is bare "/").
    """
    url = url.strip()
    url_no_frag, _ = urldefrag(url)         # step 1: strip #fragment
    parts = urlsplit(url_no_frag)

    # step 2: lowercase scheme + host
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    # step 3+4: filter and sort query params
    qs_pairs = parse_qsl(parts.query, keep_blank_values=True)
    filtered = sorted(
        (k, v) for k, v in qs_pairs if k.lower() not in _STRIP_PARAMS
    )
    new_query = urlencode(filtered)

    # step 5: strip trailing slash (but keep bare "/")
    path = parts.path.rstrip("/") or "/"

    canonical = urlunsplit((scheme, netloc, path, new_query, ""))
    return canonical


def content_hash(*, canonical_url: str, title: str, body: str | None) -> str:
    """Return the SHA-256 hex digest used as the dedup key in ``articles``.

    Definition (FROZEN — changing this requires rehashing all existing rows):

        sha256( canonical_url + "\\n" + title.strip() + "\\n" + (body or "").strip() )

    The separator ``\\n`` is chosen because it cannot appear inside a URL.
    """
    payload = f"{canonical_url}\n{title.strip()}\n{(body or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_language(text: str) -> str | None:
    """Best-effort ISO 639-1 language detection. Returns None if uncertain.

    Tries the optional ``langdetect`` library; degrades gracefully if absent.
    Texts shorter than 20 characters always return None (too short to be reliable).
    """
    if len(text) < 20:
        return None
    try:
        import langdetect  # optional dependency
        return langdetect.detect(text)
    except Exception:  # library not installed or detection failed
        return None
