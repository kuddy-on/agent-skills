from __future__ import annotations

import json
import re
import stat
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
YAML_KEY = re.compile(r"^[a-z][a-z0-9_-]*$")
INTERFACE_ENTRY = re.compile(r"^  ([a-z][a-z0-9_]*):\s+(.+)$")


def scalar(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        decoded = json.loads(value)
        if not isinstance(decoded, str):
            raise ValueError("expected a string scalar")
        return decoded
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def read_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("SKILL.md must start with YAML front matter")
    header, body = text[4:].split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if not separator or not YAML_KEY.fullmatch(key):
            raise ValueError(f"invalid front matter line: {line!r}")
        if key in metadata:
            raise ValueError(f"duplicate front matter key: {key}")
        metadata[key] = scalar(value)
    return metadata, body


def read_interface(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "interface:":
        raise ValueError("agents/openai.yaml must start with interface:")
    interface: dict[str, str] = {}
    for line in lines[1:]:
        match = INTERFACE_ENTRY.fullmatch(line)
        if not match:
            raise ValueError(f"invalid interface line: {line!r}")
        key, value = match.groups()
        if key in interface:
            raise ValueError(f"duplicate interface key: {key}")
        interface[key] = scalar(value)
    return interface


class SkillLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_dirs = sorted(
            path for path in SKILLS_ROOT.iterdir() if path.is_dir()
        )
        self.assertTrue(
            self.skill_dirs, "the repository must contain at least one skill"
        )

    def test_skill_frontmatter_matches_directory(self) -> None:
        for skill_dir in self.skill_dirs:
            with self.subTest(skill=skill_dir.name):
                self.assertRegex(skill_dir.name, SKILL_NAME)
                self.assertLess(len(skill_dir.name), 64)
                metadata, body = read_frontmatter(skill_dir / "SKILL.md")
                self.assertEqual(set(metadata), {"name", "description"})
                self.assertEqual(metadata["name"], skill_dir.name)
                self.assertTrue(metadata["description"].strip())
                self.assertTrue(body.strip())

    def test_agent_interface_has_required_fields(self) -> None:
        required = {"display_name", "short_description", "default_prompt"}
        for skill_dir in self.skill_dirs:
            with self.subTest(skill=skill_dir.name):
                interface = read_interface(skill_dir / "agents" / "openai.yaml")
                self.assertTrue(required.issubset(interface))
                for key in required:
                    self.assertTrue(interface[key].strip(), f"{key} must not be empty")
                self.assertIn(f"${skill_dir.name}", interface["default_prompt"])

    def test_python_scripts_are_executable(self) -> None:
        for skill_dir in self.skill_dirs:
            scripts = sorted((skill_dir / "scripts").glob("*.py"))
            for script in scripts:
                with self.subTest(script=script.relative_to(ROOT)):
                    self.assertEqual(
                        script.read_text(encoding="utf-8").splitlines()[0],
                        "#!/usr/bin/env python3",
                    )
                    self.assertTrue(script.stat().st_mode & stat.S_IXUSR)

    def test_shell_scripts_use_bash_strict_mode(self) -> None:
        for script in sorted(ROOT.glob("skills/*/scripts/*.sh")):
            with self.subTest(script=script.relative_to(ROOT)):
                lines = script.read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines[0], "#!/usr/bin/env bash")
                self.assertIn("set -euo pipefail", lines[:5])
                self.assertTrue(script.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
