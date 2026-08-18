"""Unit tests for KepubSpanResolver.

Fixtures are a minimal real EPUB (parsed with EbookParser, same as the live
codepath) paired with a synthetic KePub — a hand-built zip with kobo.N.M
span-wrapped XHTML, standing in for what kepubify would actually produce.
"""

import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from src.utils.ebook_utils import EbookParser
from src.utils.kepub_span_resolver import KepubSpanResolver

SPAN1_TEXT = "First segment text here."
SPAN2_TEXT = "Second segment follows now."
# The source chapter runs a bit past what the synthetic KePub spans below
# cover, so an offset can land past the last span while still being inside
# the source spine item's own range.
CHAPTER1_TEXT = f"{SPAN1_TEXT} {SPAN2_TEXT} Extra tail words for the clamp test."


def _write_epub(path: Path, chapter_body: str, second_chapter_body: str = None) -> None:
    """Write a minimal but real EPUB, mirroring test_ebook_parse_cache_invalidation.py."""
    manifest_items = ['<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>']
    spine_items = ['<itemref idref="c1"/>']
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        if second_chapter_body is not None:
            manifest_items.append('<item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="c2"/>')
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">test-book</dc:identifier>'
            "<dc:title>Test</dc:title></metadata>"
            f'<manifest>{"".join(manifest_items)}</manifest>'
            f'<spine>{"".join(spine_items)}</spine></package>',
        )
        zf.writestr(
            "c1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<body><p>{chapter_body}</p></body></html>",
        )
        if second_chapter_body is not None:
            zf.writestr(
                "c2.xhtml",
                '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<body><p>{second_chapter_body}</p></body></html>",
            )


def _write_kepub_bytes(span1_text: str, span2_text: str) -> bytes:
    """Build a synthetic KePub: same c1.xhtml href, spans wrapping the text."""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "c1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
            f'<span id="kobo.1.1">{span1_text}</span>'
            f'<span id="kobo.1.2">{span2_text}</span>'
            "</p></body></html>",
        )
    return buf.getvalue()


class TestKepubSpanResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.books_dir = Path(self.tmp.name)
        self.parser = EbookParser(self.books_dir, epub_cache_dir=self.books_dir / "cache")
        self.epub_path = self.books_dir / "book.epub"
        _write_epub(self.epub_path, CHAPTER1_TEXT)

        self.mock_sync_api = Mock()
        self.resolver = KepubSpanResolver(self.parser, self.mock_sync_api)

        # c1 is the first (only) spine item, so its 'start' is 0 and global
        # offsets equal local offsets directly.
        _, self.spine_map = self.parser.extract_text_and_map(self.epub_path)
        self.assertEqual(self.spine_map[0]["start"], 0)

    def test_resolves_offset_in_first_span(self):
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)

        result = self.resolver.resolve("42", "book.epub", 5)

        self.assertEqual(result, ("c1.xhtml", "kobo.1.1"))
        self.mock_sync_api.download_kepub.assert_called_once_with("42")

    def test_resolves_offset_in_second_span(self):
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)

        offset = len(SPAN1_TEXT) + 5  # +1 join gap lands us inside span 2
        result = self.resolver.resolve("42", "book.epub", offset)

        self.assertEqual(result, ("c1.xhtml", "kobo.1.2"))

    def test_offset_past_last_span_clamps_to_last(self):
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)

        # Inside the source chapter's range, but past the last span the
        # synthetic KePub covers.
        offset = len(SPAN1_TEXT) + 1 + len(SPAN2_TEXT) + 3
        result = self.resolver.resolve("42", "book.epub", offset)

        self.assertEqual(result, ("c1.xhtml", "kobo.1.2"))

    def test_reuses_cached_span_map_on_second_call(self):
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)

        self.resolver.resolve("42", "book.epub", 5)
        self.resolver.resolve("42", "book.epub", 5)

        self.mock_sync_api.download_kepub.assert_called_once()

    def test_download_failure_falls_back_to_none(self):
        self.mock_sync_api.download_kepub.return_value = None

        result = self.resolver.resolve("42", "book.epub", 5)

        self.assertIsNone(result)

    def test_href_not_present_in_kepub_falls_back_to_none(self):
        # Simulate a KePub whose internal filename doesn't match the source
        # EPUB's spine href at all.
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr(
                "other.xhtml",
                '<html><body><span id="kobo.1.1">Unrelated text</span></body></html>',
            )
        self.mock_sync_api.download_kepub.return_value = buf.getvalue()

        result = self.resolver.resolve("42", "book.epub", 5)

        self.assertIsNone(result)

    def test_gross_length_mismatch_is_not_trusted_and_falls_back_to_none(self):
        # A KePub chapter whose spanned text is wildly shorter than the
        # source chapter suggests a stale cache / mismatched conversion --
        # better to fall back than send a misleading span.
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes("x", "y")

        result = self.resolver.resolve("42", "book.epub", 5)

        self.assertIsNone(result)

    def test_zero_source_char_len_fails_closed_rather_than_skipping_check(self):
        # source_char_len == 0 can't happen via resolve() itself (an
        # empty-length chapter can never be selected as `item`, since its
        # [start, end) range is empty), but _resolve_against_cache must not
        # rely on that as its only protection -- call it directly to prove
        # a zero length is treated as "untrusted", not "unverified, allow".
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)

        result = self.resolver._resolve_against_cache("42", "c1.xhtml", 5, source_char_len=0)

        self.assertIsNone(result)

    def test_stale_cache_is_evicted_and_retried_once(self):
        bad_kepub = _write_kepub_bytes("x", "y")  # fails the length sanity check
        good_kepub = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)
        self.mock_sync_api.download_kepub.side_effect = [bad_kepub, good_kepub]

        result = self.resolver.resolve("42", "book.epub", 5)

        self.assertEqual(result, ("c1.xhtml", "kobo.1.1"))
        self.assertEqual(self.mock_sync_api.download_kepub.call_count, 2)

    def test_invalidate_forces_redownload(self):
        self.mock_sync_api.download_kepub.return_value = _write_kepub_bytes(SPAN1_TEXT, SPAN2_TEXT)

        self.resolver.resolve("42", "book.epub", 5)
        self.resolver.invalidate("42")
        self.resolver.resolve("42", "book.epub", 5)

        self.assertEqual(self.mock_sync_api.download_kepub.call_count, 2)


if __name__ == "__main__":
    unittest.main()
