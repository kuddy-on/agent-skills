"""Release Please configuration regression tests."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ReleasePleaseConfigurationTests(unittest.TestCase):
    def test_manifest_matches_version_file(self) -> None:
        manifest = json.loads(
            (ROOT / ".release-please-manifest.json").read_text(encoding="utf-8")
        )
        version = (ROOT / "version.txt").read_text(encoding="utf-8").strip()

        self.assertEqual(manifest, {".": version})
        self.assertRegex(version, SEMVER)

    def test_root_package_uses_simple_release_type(self) -> None:
        config = json.loads(
            (ROOT / "release-please-config.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            config["packages"]["."],
            {
                "include-component-in-tag": False,
                "initial-version": "0.1.0",
                "package-name": "agent-skills",
                "release-type": "simple",
            },
        )

    def test_workflow_uses_github_token_and_current_action(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-please.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("googleapis/release-please-action@v5", workflow)
        self.assertIn("token: ${{ github.token }}", workflow)
        self.assertNotIn("RELEASE_PLEASE_TOKEN", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pull-requests: write", workflow)


if __name__ == "__main__":
    unittest.main()
