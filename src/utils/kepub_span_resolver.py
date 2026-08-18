"""
Resolves a global character offset (from the KOReader/xpath precision engine
in ebook_utils.py) to a native Kobo Nickel span ContentID.

Nickel keeps its own local CurrentBookmark.Location and reopens a book there
regardless of what progress percent the server reports, so a synced position
only "sticks" on-device if it points at a span ID that actually exists in the
KePub file Nickel has. Those span IDs (``kobo.<N>.<M>``) are assigned by
kepubify at conversion time and aren't derivable from the source EPUB the way
an XPath or CFI is, so this fetches the same converted KePub CWA serves to a
real device and reads the span IDs directly rather than trying to reproduce
kepubify's paragraph/sentence-splitting algorithm.
"""

import io
import logging
import re
import zipfile
from typing import Optional, Tuple

from bs4 import BeautifulSoup
from lxml import html

from src.utils.ebook_utils import LRUCache

logger = logging.getLogger(__name__)

_SPAN_ID_RE = re.compile(r'^kobo\.\d+\.\d+$')

# A resolved span's total spanned text length is compared against the source
# EPUB chapter's text length; outside this ratio the KePub is treated as not
# matching the source we resolved against (stale cache, mismatched
# conversion, etc.) rather than trusted.
_LENGTH_RATIO_TOLERANCE = (0.5, 2.0)


class KepubSpanResolver:
    """Maps (book, global char offset) -> real Kobo span ContentID."""

    def __init__(self, ebook_parser, cwa_sync_api, cache_size: int = 20):
        self.ebook_parser = ebook_parser
        self.cwa_sync_api = cwa_sync_api
        self._span_cache = LRUCache(capacity=cache_size)

    def invalidate(self, book_id: str) -> None:
        self._span_cache.delete(str(book_id))

    def resolve(self, book_id: str, epub_filename: str, global_offset: int) -> Optional[Tuple[str, str]]:
        """Returns (href, span_id) for the span containing global_offset, or None."""
        if not book_id or not epub_filename or global_offset is None:
            return None

        _, spine_map = self.ebook_parser.extract_text_and_map(epub_filename)
        item = next((i for i in spine_map if i["start"] <= global_offset < i["end"]), None)
        if item is None:
            return None
        local_offset = global_offset - item["start"]
        href = item.get("href")
        source_char_len = item.get("char_len") or 0
        if not href:
            return None

        result = self._resolve_against_cache(book_id, href, local_offset, source_char_len)
        if result is not None:
            return result

        # Cache stale or empty for this book (e.g. the source was
        # re-converted since we last cached it) — evict and retry once with
        # a fresh conversion before giving up.
        self.invalidate(book_id)
        return self._resolve_against_cache(book_id, href, local_offset, source_char_len)

    def _resolve_against_cache(self, book_id, href, local_offset, source_char_len) -> Optional[Tuple[str, str]]:
        span_map = self._get_span_map(book_id)
        if not span_map:
            return None
        spans = span_map.get(href)
        if not spans:
            return None

        kepub_char_len = spans[-1][2]
        if source_char_len <= 0 or kepub_char_len <= 0:
            # An unreachable-length chapter can't be sanity-checked, so don't
            # trust it by default — fail closed rather than silently skipping
            # verification. (source_char_len is 0 only for an empty chapter,
            # which `resolve()`'s item selection already excludes upstream;
            # this guard exists so that invariant isn't required to hold.)
            logger.debug(
                "KepubSpanResolver: cannot verify chapter length for book %s href=%s "
                "(source=%s kepub=%s) — not trusting this span map",
                book_id, href, source_char_len, kepub_char_len,
            )
            return None

        ratio = kepub_char_len / source_char_len
        if not (_LENGTH_RATIO_TOLERANCE[0] <= ratio <= _LENGTH_RATIO_TOLERANCE[1]):
            logger.debug(
                "KepubSpanResolver: chapter length mismatch for book %s href=%s "
                "(source=%s kepub=%s) — not trusting this span map",
                book_id, href, source_char_len, kepub_char_len,
            )
            return None

        for span_id, start, end in spans:
            if start <= local_offset < end:
                return href, span_id

        # Offset falls outside every span's range (e.g. leading/trailing
        # unspanned content) — clamp to the nearest span rather than fail.
        first, last = spans[0], spans[-1]
        if local_offset < first[1]:
            return href, first[0]
        if local_offset >= last[2]:
            return href, last[0]
        return None

    def _get_span_map(self, book_id: str) -> Optional[dict]:
        cache_key = str(book_id)
        cached = self._span_cache.get(cache_key)
        if cached is not None:
            return cached
        span_map = self._build_span_map(book_id)
        if span_map:
            self._span_cache.put(cache_key, span_map)
        return span_map

    def _build_span_map(self, book_id: str) -> Optional[dict]:
        data = self.cwa_sync_api.download_kepub(book_id)
        if not data:
            return None
        try:
            span_map = {}
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith((".xhtml", ".html", ".htm")):
                        continue
                    try:
                        content = zf.read(name)
                    except Exception:
                        continue
                    spans = self._extract_spans(content)
                    if spans:
                        span_map[name] = spans
            return span_map or None
        except Exception as e:
            logger.debug(f"KepubSpanResolver: failed to parse KePub for book {book_id}: {e}")
            return None

    @staticmethod
    def _extract_spans(xhtml_bytes: bytes) -> list:
        """Walk kobo.N.M spans in document order, building (span_id, start, end)
        offsets in the same normalized-text coordinate space as
        EbookParser.extract_text_and_map (BeautifulSoup get_text(separator=' ',
        strip=True)), so they're directly comparable to a global char offset
        resolved against the source EPUB.
        """
        try:
            tree = html.fromstring(xhtml_bytes)
        except Exception:
            return []

        spans = []
        offset = 0
        for el in tree.iter():
            if el.tag != "span":
                continue
            span_id = el.get("id")
            if not span_id or not _SPAN_ID_RE.match(span_id):
                continue
            text = BeautifulSoup(html.tostring(el), "html.parser").get_text(separator=' ', strip=True)
            length = len(text)
            if length == 0:
                continue
            spans.append((span_id, offset, offset + length))
            offset += length + 1  # +1 mirrors the " ".join separator between segments

        return spans
