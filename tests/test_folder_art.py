#!/usr/bin/env python3
"""
test_folder_art.py — picking the cover image that sits BESIDE the music.

The indexer only ever read EMBEDDED pictures, so an album whose cover is
a separate file in its folder showed no art at all. Measured on the live
library: of 506 album-folders with no cover, **99 have an image file
right there** that was never looked at.

Choosing WHICH image is the whole problem, because the candidates differ
by two orders of magnitude and some are not the front cover at all.
Measured, in the same folders:

    front.jpg          2927x2911   2032 KB
    folder.jpg         1123x1111    425 KB
    cover.jpg           300x300      36 KB
    albumartsmall.jpg    75x75        3 KB   <- Windows Media Player
    back.jpg           2900x2908   3015 KB   <- the BACK cover
    label.jpg          2840x2856   1801 KB   <- the disc label

So "first image wins" picks a 75px thumbnail or the back of the sleeve.
Size alone is no better: `back.jpg` is the BIGGEST file in that list.
Only the NAME says which one is the cover.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_providers.localfs_art import (_extract_art_bytes,      # noqa: E402
                                        _folder_art_path)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _album(self, *files, sub="Album", sizes=None):
        d = self.root / sub
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            # big enough to clear the junk-thumbnail floor unless told
            n = (sizes or {}).get(f, 40_000)
            (d / f).write_bytes(b"\xff\xd8\xff" + b"x" * n)
        (d / "01 Track.flac").write_bytes(b"fLaC")
        return d / "01 Track.flac"

    def pick(self, track):
        got = _folder_art_path(track)
        return got.name if got else None


class TestPreferenceOrder(_Base):
    def test_a_lone_cover_is_chosen(self):
        self.assertEqual(self.pick(self._album("cover.jpg")), "cover.jpg")

    def test_conventional_names_beat_everything_else(self):
        for name in ("cover.jpg", "folder.jpg", "front.jpg"):
            with self.subTest(name=name):
                t = self._album(name, "scan001.jpg", sub=f"A-{name}")
                self.assertEqual(self.pick(t), name)

    def test_cover_beats_a_windows_media_player_thumbnail(self):
        """AlbumArtSmall.jpg is 75x75 — it was the COMMONEST image in
        the art-less folders, so losing to it would waste the feature."""
        t = self._album("albumartsmall.jpg", "cover.jpg")
        self.assertEqual(self.pick(t), "cover.jpg")

    def test_a_named_front_is_found_inside_a_longer_filename(self):
        """Real: 'blof - het eind van het begin - front.jpg'."""
        t = self._album("blof - het eind van het begin - front.jpg")
        self.assertIn("front", self.pick(t))


class TestNeverTheWrongPicture(_Base):
    """Each of these is a real file from the measured folders, and each
    would be WRONG on an album cover."""

    def test_the_back_cover_is_never_chosen(self):
        self.assertIsNone(self.pick(self._album("back.jpg")))

    def test_the_disc_label_is_never_chosen(self):
        self.assertIsNone(self.pick(self._album("label.jpg")))

    def test_disc_art_is_never_chosen(self):
        t = self._album("blof-ooginoog-liveinahoy-cd1.jpg")
        self.assertIsNone(self.pick(t))

    def test_a_wmp_LARGE_cover_is_used(self):
        """Windows Media Player writes both variants. Measured on this
        library the `_large` ones are 6-14 KB (about 200px) — modest,
        but a real cover and better than none, and they sat just under
        a byte floor that was never the right instrument for them. The
        NAME already says which variant it is."""
        t = self._album(
            "AlbumArt_{382E130C-3A74-4F43-A99E-8D4532CC4578}_Large.jpg",
            sizes={"AlbumArt_{382E130C-3A74-4F43-A99E-8D4532CC4578}_Large.jpg": 9000})
        self.assertIsNotNone(self.pick(t))

    def test_the_small_variant_still_loses_to_the_large_one(self):
        big = "AlbumArt_{A}_Large.jpg"
        t = self._album(big, "AlbumArt_{A}_Small.jpg",
                        sizes={big: 9000, "AlbumArt_{A}_Small.jpg": 3000})
        self.assertEqual(self.pick(t), big)

    def test_a_wmp_small_thumbnail_alone_is_not_used(self):
        t = self._album("albumart_{72b51731-a5ae-4240-935e-7d2bf4a8789d}_small.jpg")
        self.assertIsNone(self.pick(t))

    def test_back_never_wins_even_though_it_is_the_biggest_file(self):
        """Size is the obvious tiebreak and the wrong one."""
        t = self._album("cover.jpg", "back.jpg",
                        sizes={"cover.jpg": 20_000, "back.jpg": 3_000_000})
        self.assertEqual(self.pick(t), "cover.jpg")

    def test_a_band_whose_name_contains_cd_is_not_read_as_disc_art(self):
        """⚠ The substring trap. Rejecting anything containing 'cd'
        would throw away 'ACDC - cover.jpg'. Same lesson as
        `_is_junk_name`: match whole words, never substrings."""
        t = self._album("ACDC - cover.jpg")
        self.assertEqual(self.pick(t), "ACDC - cover.jpg")


class TestMultiDisc(_Base):
    def test_a_cover_in_the_PARENT_folder_is_found(self):
        """album_key folds CD1/CD2 into the parent, so the album's one
        cover usually sits beside the disc folders, not inside them."""
        album = self.root / "Big Box"
        (album / "CD1").mkdir(parents=True)
        (album / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 40_000)
        track = album / "CD1" / "01 Track.flac"
        track.write_bytes(b"fLaC")
        self.assertEqual(self.pick(track), "cover.jpg")

    def test_an_image_in_the_disc_folder_still_wins_over_the_parent(self):
        album = self.root / "Box"
        (album / "CD2").mkdir(parents=True)
        (album / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 40_000)
        (album / "CD2" / "folder.jpg").write_bytes(b"\xff\xd8\xff" + b"y" * 40_000)
        track = album / "CD2" / "01 Track.flac"
        track.write_bytes(b"fLaC")
        self.assertEqual(self.pick(track), "folder.jpg")

    def test_an_ordinary_folder_does_not_climb_to_its_parent(self):
        """Only a DISC subfolder climbs. Otherwise every album in a
        genre folder would inherit a stray image from it."""
        (self.root / "Genre").mkdir()
        (self.root / "Genre" / "cover.jpg").write_bytes(b"\xff\xd8" + b"x" * 40_000)
        d = self.root / "Genre" / "Some Album"
        d.mkdir()
        t = d / "01 Track.flac"
        t.write_bytes(b"fLaC")
        self.assertIsNone(self.pick(t))


class TestSafety(_Base):
    def test_no_images_at_all(self):
        self.assertIsNone(self.pick(self._album()))

    def test_a_missing_directory_is_survivable(self):
        self.assertIsNone(_folder_art_path(Path("/nope/nothing/x.flac")))

    def test_non_images_are_ignored(self):
        t = self._album("readme.txt", "cover.log", "notes.pdf")
        self.assertIsNone(self.pick(t))

    def test_case_is_irrelevant(self):
        self.assertEqual(self.pick(self._album("COVER.JPG")), "COVER.JPG")


class TestItActuallyReachesTheServer(_Base):
    """`_extract_art_bytes` is the ONE function both the scanner and
    `/localfs/art/<id>` call, so putting the fallback there is what
    makes the cover both indexed and served — no new route, no schema
    change, no URL change."""

    def _flac_with_art(self, d: Path) -> Path:
        """A FLAC the tag reader will refuse — so any bytes that come
        back can only have come from the folder."""
        t = d / "01 Track.flac"
        t.write_bytes(b"fLaC" + b"\x00" * 64)
        return t

    def test_a_folder_cover_is_returned_when_nothing_is_embedded(self):
        d = self.root / "Album"; d.mkdir()
        (d / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 40_000)
        got = _extract_art_bytes(self._flac_with_art(d))
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "image/jpeg")
        self.assertGreater(len(got[0]), 1000)

    def test_a_rejected_image_is_not_served_either(self):
        """back.jpg must not reach the panel through the back door."""
        d = self.root / "Album2"; d.mkdir()
        (d / "back.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 40_000)
        self.assertIsNone(_extract_art_bytes(self._flac_with_art(d)))

    def test_an_absurdly_large_image_is_refused(self):
        """The byte routes cap embedded art at 12 MB; a folder file has
        no such natural limit, and reading one into memory per request
        is how a slideshow of scans becomes an outage."""
        d = self.root / "Album3"; d.mkdir()
        (d / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * (13 * 1024 * 1024))
        self.assertIsNone(_extract_art_bytes(self._flac_with_art(d)))

    def test_the_mime_is_sniffed_not_taken_from_the_extension(self):
        """Same rule as embedded art: a .jpg holding PNG bytes is a
        PNG, and something claiming to be an image that isn't gets the
        raster fallback rather than being trusted."""
        d = self.root / "Album4"; d.mkdir()
        (d / "cover.jpg").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40_000)
        got = _extract_art_bytes(self._flac_with_art(d))
        self.assertEqual(got[1], "image/png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
