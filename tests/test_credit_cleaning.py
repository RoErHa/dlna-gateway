#!/usr/bin/env python3
"""
test_credit_cleaning.py — junk filtering for songwriting credits.

Scene releases inject their advertising into the tag fields. Measured on
the live library: 691 of 8,570 credited tracks (8%) carry
`www.t.me/pmedia_music` or `www.thenzbplace.com` in composer/lyricist,
so the now-playing panel would read "Words www.t.me/pmedia_music".

The filter is DISPLAY-ONLY and deliberately narrow. The rule that was
tried and REJECTED is the reason this file exists — see
`TestNonLatinScriptsAreNeverJunk`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_credits import clean_credit                          # noqa: E402


class TestRealNamesSurvive(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(clean_credit("Freddie Mercury"), "Freddie Mercury")

    def test_initials_keep_their_dots(self):
        """'J.S. Bach' has dots but no REPEATED punctuation run."""
        self.assertEqual(clean_credit("J.S. Bach"), "J.S. Bach")
        self.assertEqual(clean_credit("Peter F. Hamilton"),
                         "Peter F. Hamilton")

    def test_slash_joined_multi_writer_credit(self):
        """The commonest real shape in this library."""
        v = "Candy Derouge/Gunther Mende/Jennifer Rush/Mary Susan Applegate"
        self.assertEqual(clean_credit(v), v)

    def test_hyphenated_name(self):
        self.assertEqual(clean_credit("Jean-Marc Aubert"), "Jean-Marc Aubert")

    def test_whitespace_is_stripped(self):
        self.assertEqual(clean_credit("  Elton John \n"), "Elton John")

    def test_empty_stays_empty(self):
        self.assertEqual(clean_credit(""), "")
        self.assertEqual(clean_credit("   "), "")
        self.assertEqual(clean_credit(None), "")


class TestNonLatinScriptsAreNeverJunk(unittest.TestCase):
    """⚠ THE REJECTED RULE.

    "A credit with no A-Za-z letters is junk" looked reasonable and
    matched 55 rows on the live library. Every one of them was a REAL
    composer:

        Игорь Фёдорович Стравинский      (Stravinsky)
        Сергей Сергеевич Прокофьев       (Prokofiev)
        Пётр Ильич Чайковский            (Tchaikovsky)
        Сергей Васильевич Рахманинов     (Rachmaninov)
        Ջիվան Գասպարյան                  (Djivan Gasparyan)

    Hiding the Russian composers from a classical library is the exact
    opposite of the feature. Same lesson as `is_a_performer_name`'s
    `allow_numeric` (112, 911, 98° are real bands): a shape test that
    encodes "looks English" erases real data. Do not re-introduce it."""

    NAMES = ["Игорь Фёдорович Стравинский", "Сергей Сергеевич Прокофьев",
             "Пётр Ильич Чайковский", "Сергей Васильевич Рахманинов",
             "Ջիվան Գասպարյան", "坂本龍一", "Björk Guðmundsdóttir"]

    def test_every_non_latin_name_survives(self):
        for n in self.NAMES:
            with self.subTest(name=n):
                self.assertEqual(clean_credit(n), n)


class TestJunkIsRejected(unittest.TestCase):
    """Both rules were measured against all 2,447 distinct credit values
    in the library: each matches exactly 2, with no false positives."""

    def test_telegram_scene_advert(self):
        self.assertEqual(clean_credit("www.t.me/pmedia_music"), "")

    def test_bare_domain(self):
        self.assertEqual(clean_credit("www.thenzbplace.com"), "")

    def test_http_url(self):
        self.assertEqual(clean_credit("https://example.com/releases"), "")

    def test_repeated_punctuation_run_is_machine_garbage(self):
        """No human name contains '....' or '¤¤¤¤¤¤'."""
        self.assertEqual(
            clean_credit("Varioussze....@....2012¤¤¤¤¤¤¤¤¤¤TFM"), "")
        self.assertEqual(clean_credit("Varioussze...@...TFM....2012.."), "")

    def test_an_at_sign_alone_is_not_junk(self):
        """Only a repeated-punctuation RUN condemns a value — a lone
        '@' or dot must not, or initials and odd-but-real credits go."""
        self.assertEqual(clean_credit("DJ @ndy"), "DJ @ndy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
