#!/usr/bin/env python3
"""
tests/test_art_safety.py — a media file must never make anything phone home.

From the 2026-08-20 audit. A media file is untrusted input, and its embedded
cover is the one part of it the gateway hands to a browser. Two constructs
make that dangerous, and the protection against BOTH is currently a side
effect of sniffing magic bytes instead of trusting the container's declared
MIME — which is exactly the kind of accidental safety that gets refactored
away by someone "fixing" the MIME handling.

These tests pin the behaviour so that cannot happen silently:

  * ID3 `APIC` with MIME `-->` means the payload is a URL rather than image
    bytes (a documented phone-home form). It must be treated as opaque data
    and never dereferenced.
  * An SVG cover is script-capable markup. It must never be labelled
    image/svg+xml, or a cover becomes an XSS / tracking beacon.

Also asserts the tag reader stays on its scalar allowlist, so no ID3 URL
frame (WOAR/WXXX/WCOM) can ever become a URL the gateway fetches.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dlna_providers.localfs_tags import _sniff_image_mime  # noqa: E402

_RASTER = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class TestNoPhoneHomeFromEmbeddedArt(unittest.TestCase):

    def test_apic_url_payload_is_never_a_url_type(self):
        """ID3 APIC mime='-->' carries a URL. It must stay opaque bytes."""
        for payload in (b"http://attacker.example/beacon.png",
                        b"https://attacker.example/track?id=1",
                        b"//attacker.example/x.png"):
            mime = _sniff_image_mime(payload)
            self.assertIn(mime, _RASTER, payload)
            # Nothing here may hint the payload should be dereferenced.
            self.assertNotIn("uri", mime.lower())
            self.assertNotIn("html", mime.lower())

    def test_svg_cover_is_never_typed_as_svg(self):
        """An SVG cover must not be parseable as SVG by the browser."""
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
               b'<script>fetch("http://evil/")</script></svg>')
        self.assertNotEqual(_sniff_image_mime(svg), "image/svg+xml")
        self.assertIn(_sniff_image_mime(svg), _RASTER)

    def test_html_cover_is_never_typed_as_html(self):
        html = b'<html><img src="http://evil/beacon"></html>'
        self.assertNotEqual(_sniff_image_mime(html), "text/html")
        self.assertIn(_sniff_image_mime(html), _RASTER)

    def test_sniffer_only_ever_returns_raster_types(self):
        """The allowlist is raster-only ON PURPOSE — see the docstring on
        _sniff_image_mime. A new branch returning svg/html/anything active
        would reopen both vectors above."""
        for data in (b"", b"\x00" * 32, b"MZ\x90\x00", b"%PDF-1.4",
                     b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF89a",
                     b"RIFF\x00\x00\x00\x00WEBP"):
            self.assertIn(_sniff_image_mime(data), _RASTER, data[:8])

    def test_real_formats_still_detected(self):
        """The safety must not come at the cost of correct detection."""
        self.assertEqual(_sniff_image_mime(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(_sniff_image_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(_sniff_image_mime(b"GIF89a-rest"), "image/gif")
        self.assertEqual(
            _sniff_image_mime(b"RIFF\x10\x00\x00\x00WEBPVP8 "), "image/webp")


class TestTagReaderReadsNoUrls(unittest.TestCase):
    """The other half of "no phone home from a media file": the indexer must
    not read any URL-bearing tag frame in the first place."""

    def test_read_tags_uses_a_scalar_allowlist(self):
        import inspect

        from dlna_providers import localfs_tags
        src = inspect.getsource(localfs_tags._read_tags)
        # ID3 URL frames + the common free-text URL keys. If a future edit
        # starts reading any of these, it must come with a decision about
        # whether the value is ever dereferenced.
        for frame in ("WOAR", "WXXX", "WCOM", "WOAF", "WORS", "WPAY",
                      "website", "contact", "url"):
            self.assertNotIn(f'"{frame}"', src,
                             f"_read_tags now reads {frame!r} — if that value "
                             f"can ever be fetched, a crafted file phones home")

    def test_read_tags_opens_in_easy_mode(self):
        """`easy=True` is what limits mutagen to mapped scalar keys rather
        than exposing every raw frame (APIC/W*** included)."""
        import inspect

        from dlna_providers import localfs_tags
        src = inspect.getsource(localfs_tags._read_tags)
        self.assertIn("easy=True", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
