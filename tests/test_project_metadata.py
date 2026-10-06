from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / "pyproject.toml").is_file(), "requires repository metadata")
class ProjectMetadataTests(unittest.TestCase):
    def test_apache_license_matches_package_and_citation(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn('license = "Apache-2.0"', metadata)
        self.assertIn('license-files = ["LICENSE", "NOTICE"]', metadata)
        self.assertIn("license: Apache-2.0", citation)

    def test_readme_opens_with_voluntary_author_citation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        opening = readme.splitlines()[0]
        self.assertIn("使用请引用：Haocheng Wang", opening)
        self.assertIn("[CITATION.cff](CITATION.cff)", opening)
        self.assertIn("不是 Apache-2.0 协议的附加使用条件", opening)
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn("family-names: Wang", citation)
        self.assertIn("given-names: Haocheng", citation)
        self.assertIn('repository-code: "https://github.com/haochengw372-hash/pyXcom"', citation)

    def test_notice_is_preserved_and_shipped(self):
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Haocheng Wang", notice)
        self.assertIn("MIT License", notice)
        self.assertIn("Permission is hereby granted, free of charge", notice)
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"/NOTICE"', metadata)


if __name__ == "__main__":
    unittest.main()
