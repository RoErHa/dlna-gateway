#!/usr/bin/env python3
"""
tests/test_schema_sync.py — fail the suite if the committed `schema.sql`
has drifted from the actual DB schema.

`schema.sql` is a generated artifact that does NOT auto-update on
migrations; it drifted unnoticed across A1/A2 (album_key) and the
loudness removal. This guard compares it to a fresh, fully-migrated DB's
`.schema` dump so any future schema change that forgets to regenerate is
caught immediately. Fix when it fails: `python3 tools/regen_schema.py`.

Run standalone:
    python3 -m unittest tests.test_schema_sync -v
"""
import os
import shutil
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import tools.regen_schema as regen


class TestSchemaSync(unittest.TestCase):

    @unittest.skipUnless(shutil.which("sqlite3"),
                         "sqlite3 CLI not available to dump .schema")
    def test_schema_sql_matches_fresh_db(self):
        fresh = regen.generate_schema()
        self.assertTrue(os.path.exists(regen.SCHEMA_PATH),
                        "schema.sql is missing")
        with open(regen.SCHEMA_PATH) as f:
            committed = f.read()
        self.assertEqual(
            committed, fresh,
            "schema.sql is out of date with the migrated DB schema — "
            "regenerate it: python3 tools/regen_schema.py")


if __name__ == "__main__":
    unittest.main()
