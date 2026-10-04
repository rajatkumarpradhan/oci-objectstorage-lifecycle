import copy
import json
import os
import tempfile
import unittest

from os_lifecycle import analysis, model
from os_lifecycle.cli import main

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples", "synthetic.json")
with open(EX, encoding="utf-8") as f:
    BASE = json.load(f)
G = 1024 ** 3


def inv(mut=None):
    d = copy.deepcopy(BASE)
    if mut:
        mut(d)
    return model.load(d)


def bucket(d, name):
    return next(b for b in d["buckets"] if b["name"] == name)


def found(i):
    fs, t = analysis.analyze(i)
    return {(f.rule, f.bucket) for f in fs}, fs, t


class ModelTests(unittest.TestCase):
    def test_loads(self):
        self.assertEqual(len(inv().buckets), 3)

    def test_bad_tier(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: bucket(d, "app-logs")["objects"][0].update(tier="glacier"))

    def test_future_object(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: bucket(d, "app-logs")["objects"][0].update(last_modified="2027-01-01"))

    def test_bad_action(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: bucket(d, "backups")["lifecycle_rules"][0].update(action="explode"))

    def test_bad_days(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: bucket(d, "backups")["lifecycle_rules"][0].update(after_days=0))

    def test_abort_must_target_multipart(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: bucket(d, "backups")["lifecycle_rules"][2].update(target="objects"))

    def test_duplicate_bucket(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: d["buckets"].append(copy.deepcopy(d["buckets"][0])))

    def test_negative_size_and_bad_date(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: bucket(d, "app-logs")["objects"][0].update(size_bytes=-1))
        with self.assertRaises(model.ModelError):
            inv(lambda d: d.update(as_of="not-a-date"))

    def test_missing_prices(self):
        with self.assertRaises(model.ModelError):
            inv(lambda d: d["prices"].pop("archive"))


class FindingTests(unittest.TestCase):
    def test_expected_rules(self):
        r, _, _ = found(inv())
        for k in [("OS001", "app-logs"), ("OS002", "app-logs"), ("OS003", "app-logs"), ("OS004", "user-uploads"),
                  ("OS005", "user-uploads"), ("OS006", "backups"), ("OS008", "backups")]:
            self.assertIn(k, r)

    def test_saving_arithmetic(self):
        _, fs, t = found(inv())
        # 120 GB (90-365d) at (0.025-0.012) + 200 GB (365d+) at (0.025-0.003)
        self.assertAlmostEqual(t["estimated_monthly_saving"], 120 * 0.013 + 200 * 0.022, places=2)

    def test_tiering_rule_removes_os002(self):
        def mut(d):
            bucket(d, "app-logs")["lifecycle_rules"] = [dict(name="t", action="move_archive", after_days=30)]
        r, _, _ = found(inv(mut))
        self.assertNotIn(("OS002", "app-logs"), r)
        self.assertNotIn(("OS001", "app-logs"), r)

    def test_prefix_scoping(self):
        def mut(d):
            bucket(d, "app-logs")["lifecycle_rules"] = [dict(name="t", action="move_archive", after_days=30, prefix="2023/")]
        _, fs, _ = found(inv(mut))
        f = next(f for f in fs if f.rule == "OS002")
        self.assertEqual(f.evidence["candidate_archive_gb"], 0.0)
        self.assertEqual(f.evidence["candidate_ia_gb"], 120.0)

    def test_disabled_rule_ignored(self):
        def mut(d):
            bucket(d, "app-logs")["lifecycle_rules"] = [dict(name="t", action="move_archive", after_days=30, enabled=False)]
        r, _, _ = found(inv(mut))
        self.assertIn(("OS001", "app-logs"), r)

    def test_abort_rule_removes_os003(self):
        def mut(d):
            bucket(d, "app-logs")["lifecycle_rules"] = [dict(name="a", action="abort_multipart", after_days=7)]
        r, _, _ = found(inv(mut))
        self.assertNotIn(("OS003", "app-logs"), r)

    def test_recent_upload_not_flagged(self):
        def mut(d):
            bucket(d, "app-logs")["multipart_uploads"] = [dict(key="n", initiated="2026-09-30", size_bytes=G)]
        r, _, _ = found(inv(mut))
        self.assertNotIn(("OS003", "app-logs"), r)

    def test_version_delete_rule_removes_os004(self):
        def mut(d):
            bucket(d, "user-uploads")["lifecycle_rules"].append(
                dict(name="v", action="delete", after_days=30, target="previous_versions"))
        r, _, _ = found(inv(mut))
        self.assertNotIn(("OS004", "user-uploads"), r)

    def test_early_deletion_boundary(self):
        def ok(d):
            rules = bucket(d, "user-uploads")["lifecycle_rules"]
            rules[1]["after_days"] = 120  # 90 days after the archive move
        r, _, _ = found(inv(ok))
        self.assertNotIn(("OS005", "user-uploads"), r)

        def bad(d):
            bucket(d, "user-uploads")["lifecycle_rules"][1]["after_days"] = 119
        r, _, _ = found(inv(bad))
        self.assertIn(("OS005", "user-uploads"), r)

    def test_infrequent_access_minimum(self):
        def mut(d):
            bucket(d, "user-uploads")["lifecycle_rules"] = [
                dict(name="ia", action="move_infrequent_access", after_days=30),
                dict(name="del", action="delete", after_days=50)]
        r, _, _ = found(inv(mut))
        self.assertIn(("OS005", "user-uploads"), r)

    def test_versions_without_versioning_flag(self):
        def mut(d):
            bucket(d, "user-uploads")["versioning"] = False
        r, _, _ = found(inv(mut))
        self.assertIn(("OS007", "user-uploads"), r)
        self.assertNotIn(("OS004", "user-uploads"), r)

    def test_clean_bucket(self):
        def mut(d):
            d["buckets"] = [dict(name="ok", versioning=False, objects=[
                dict(key="a", size_bytes=G, tier="standard", last_modified="2026-09-30")],
                lifecycle_rules=[dict(name="t", action="move_archive", after_days=400)])]
        r, fs, t = found(inv(mut))
        self.assertEqual(fs, [])
        self.assertEqual(t["estimated_monthly_saving"], 0)

    def test_negative_saving_never_reported(self):
        def mut(d):
            d["prices"] = dict(standard=0.01, infrequent_access=0.02, archive=0.03)
        _, _, t = found(inv(mut))
        self.assertEqual(t["estimated_monthly_saving"], 0)

    def test_sorted(self):
        _, fs, _ = found(inv())
        sev = [analysis.SEV[f.severity] for f in fs]
        self.assertEqual(sev, sorted(sev))


class CliTests(unittest.TestCase):
    def test_report(self):
        with tempfile.TemporaryDirectory() as t:
            out = os.path.join(t, "r.json")
            self.assertEqual(main([EX, "--output", out]), 0)
            with open(out, encoding="utf-8") as f:
                rep = json.load(f)
        self.assertEqual(rep["as_of"], "2026-10-01")
        self.assertIn("ignore retrieval", rep["disclaimer"])

    def test_fail_on_high(self):
        self.assertEqual(main([EX, "--fail-on-high"]), 2)

    def test_bad_input(self):
        self.assertEqual(main(["/nope.json"]), 1)


if __name__ == "__main__":
    unittest.main()
