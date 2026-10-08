import contextlib
import io
import json
import os
import tempfile
import unittest

from os_lifecycle import analysis, model
from os_lifecycle.importer import ImportErrors, main, parse_prices, rows_to_doc

EX = os.path.join(os.path.dirname(__file__), "..", "examples", "synthetic_listing.csv")
PRICES = {"standard": 0.025, "infrequent_access": 0.012, "archive": 0.003}
PSTR = "standard=0.025,infrequent_access=0.012,archive=0.003"
HDR = "key,size_bytes,tier,last_modified\n"
GOOD = "a.dat,100,standard,2026-01-01\n"


def conv(text, bucket="b", as_of="2026-10-01"):
    return rows_to_doc(text, as_of, PRICES, bucket)


def errs(text, bucket="b"):
    try:
        conv(text, bucket)
    except ImportErrors as e:
        return e.errors
    return []


class ImporterTests(unittest.TestCase):
    def test_example_converts_loads_and_analyzes(self):
        with open(EX, encoding="utf-8") as f:
            doc = rows_to_doc(f.read(), "2026-10-01", PRICES)
        inv = model.load(doc)
        self.assertEqual([b.name for b in inv.buckets], ["app-logs", "media"])
        self.assertEqual(len(inv.buckets[0].objects), 3)
        fs, _ = analysis.analyze(inv)
        self.assertTrue(fs)

    def test_versioning_inferred_from_previous_version_rows(self):
        with open(EX, encoding="utf-8") as f:
            doc = rows_to_doc(f.read(), "2026-10-01", PRICES)
        b = {x["name"]: x for x in doc["buckets"]}
        self.assertFalse(b["app-logs"]["versioning"])
        self.assertTrue(b["media"]["versioning"])

    def test_single_bucket_with_flag_and_no_rules(self):
        doc = conv(HDR + GOOD)
        self.assertEqual(doc["buckets"][0]["name"], "b")
        self.assertNotIn("lifecycle_rules", doc["buckets"][0])

    def test_recent_listing_only_reports_missing_rules(self):
        # a listing carries no lifecycle rules, so OS001 is expected; fresh data must not add staleness findings
        doc = conv(HDR + "new.dat,100,standard,2026-09-30\n")
        fs, _ = analysis.analyze(model.load(doc))
        self.assertEqual([f.rule for f in fs], ["OS001"])

    def test_tier_case_and_header_case_tolerated(self):
        self.assertEqual(errs(" Key , SIZE_BYTES ,Tier,last_modified\na,1,STANDARD,2026-01-01\n"), [])

    def test_timestamp_last_modified_accepted(self):
        self.assertEqual(errs(HDR + "a,1,standard,2026-01-01T10:00:00Z\n"), [])

    def test_missing_column_and_missing_bucket(self):
        self.assertIn("last_modified", errs("key,size_bytes,tier\na,1,standard\n")[0])
        self.assertIn("bucket", errs(HDR + GOOD, bucket=None)[0])

    def test_bad_values_report_row_numbers(self):
        e = errs(HDR + GOOD + "b.dat,abc,standard,2026-01-01\n" + "c.dat,1,glacier,2026-01-01\n" + "d.dat,1,standard,13/45/2026\n")
        self.assertEqual([x[:6] for x in e], ["row 3:", "row 4:", "row 5:"])

    def test_negative_size_and_future_date_rejected(self):
        self.assertEqual(len(errs(HDR + "a,-5,standard,2026-01-01\n")), 1)
        self.assertIn("after as_of", errs(HDR + "a,1,standard,2027-01-01\n")[0])

    def test_empty_key_and_bad_previous_version(self):
        self.assertIn("key is empty", errs(HDR + ",1,standard,2026-01-01\n")[0])
        self.assertIn("previous_version", errs(HDR.strip() + ",previous_version\na,1,standard,2026-01-01,maybe\n")[0])

    def test_duplicate_object_but_version_pair_is_fine(self):
        self.assertIn("duplicate", errs(HDR + GOOD + GOOD)[0])
        self.assertEqual(errs(HDR.strip() + ",previous_version\na,1,standard,2026-01-01,\na,1,standard,2025-01-01,true\n"), [])

    def test_extra_fields_blank_lines_and_empty_file(self):
        self.assertIn("more fields", errs(HDR + GOOD.strip() + ",extra\n")[0])
        self.assertEqual(errs(HDR + "\n" + GOOD + "\n"), [])
        self.assertEqual(errs(HDR), ["no data rows"])

    def test_prices_parsing(self):
        self.assertEqual(parse_prices(PSTR), PRICES)
        for bad in ("standard=0.025", "standard=x,infrequent_access=1,archive=1", "gold=1,standard=1,infrequent_access=1,archive=1"):
            with self.assertRaises(ImportErrors):
                parse_prices(bad)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_round_trip_into_analyzer(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "inv.json")
            self.assertEqual(self.run_cli([EX, "--as-of", "2026-10-01", "--prices", PSTR, "--output", out])[0], 0)
            self.assertEqual(len(model.load_file(out).buckets), 2)

    def test_cli_stdout_json_and_errors(self):
        code, out, _ = self.run_cli([EX, "--as-of", "2026-10-01", "--prices", PSTR])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["as_of"], "2026-10-01")
        self.assertEqual(self.run_cli(["/nonexistent.csv", "--as-of", "2026-10-01", "--prices", PSTR])[0], 1)
        self.assertEqual(self.run_cli([EX, "--as-of", "yesterday", "--prices", PSTR])[0], 1)
        self.assertEqual(self.run_cli([EX, "--as-of", "2026-10-01", "--prices", "bad"])[0], 1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.csv")
            with open(p, "w") as f:
                f.write(HDR + "a,x,standard,2026-01-01\n")
            code, _, err = self.run_cli([p, "--as-of", "2026-10-01", "--prices", PSTR, "--bucket", "b"])
            self.assertEqual(code, 1)
            self.assertIn("row 2", err)


if __name__ == "__main__":
    unittest.main()
