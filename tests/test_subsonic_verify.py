"""tests/subsonic_verify.py's classification — what counts as a SEARCH MISS."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from subsonic_verify import has_searchable_name  # noqa: E402


class TestHasSearchableName(unittest.TestCase):
    def test_all_tags_blank_is_unsearchable(self):
        """Displayed as 'Unknown Album' or the folder name; FTS has neither."""
        self.assertFalse(has_searchable_name(
            [{"album": ""}, {"album": None}, {"album": "  "}, {}]))

    def test_one_tagged_track_is_enough(self):
        self.assertTrue(has_searchable_name([{"album": ""}, {"album": "Alien 4"}]))

    def test_no_tracks(self):
        self.assertFalse(has_searchable_name([]))


if __name__ == "__main__":
    unittest.main()
