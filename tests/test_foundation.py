"""Phase 1 foundation tests.

Validates repository structure, configuration hygiene, and secret-safety of
.env.example. Uses the Python standard library only (no dependencies in
Phase 1). Run: python -m unittest discover -s tests -v
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DIRECTORIES = [
    "discovery",
    "backend",
    "crawler",
    "enrichment",
    "outreach",
    "database",
    "n8n",
    "prompts",
    "docs",
    "tests",
    "frontend",
]

REQUIRED_FILES = [
    "README.md",
    ".gitignore",
    ".env.example",
    ".editorconfig",
    "docs/architecture.md",
    "docs/data-model.md",
    "docs/ai-architecture.md",
    "docs/roadmap.md",
    "docs/radio-discovery.md",
]

REQUIRED_MODULE_READMES = [
    f"{d}/README.md" for d in REQUIRED_DIRECTORIES if d not in ("tests",)
] + ["tests/README.md"]

# Patterns that indicate a real credential accidentally committed.
SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9]{20,}",          # OpenAI-style key
    r"ghp_[A-Za-z0-9]{20,}",          # GitHub PAT
    r"AKIA[0-9A-Z]{16}",              # AWS access key id
    r"xox[baprs]-[A-Za-z0-9-]{10,}",  # Slack token
    r"eyJ[A-Za-z0-9_-]{30,}\.eyJ",   # JWT-looking string
]


class TestProjectStructure(unittest.TestCase):
    def test_required_directories_exist(self):
        for d in REQUIRED_DIRECTORIES:
            with self.subTest(directory=d):
                self.assertTrue((ROOT / d).is_dir(), f"missing directory: {d}")

    def test_required_files_exist(self):
        for f in REQUIRED_FILES + REQUIRED_MODULE_READMES:
            with self.subTest(file=f):
                self.assertTrue((ROOT / f).is_file(), f"missing file: {f}")


class TestConfigurationHygiene(unittest.TestCase):
    def test_gitignore_covers_env(self):
        content = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", content)
        self.assertIn("node_modules", content)
        self.assertIn("__pycache__", content)

    def test_gitignore_never_ignores_env_example(self):
        content = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("!.env.example", content)

    def test_readme_documents_current_phase(self):
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("PHASE 1", content)


class TestSecretSafety(unittest.TestCase):
    def test_env_example_contains_no_real_credentials(self):
        content = (ROOT / ".env.example").read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(
                    re.search(pattern, content),
                    f".env.example matches credential pattern {pattern}",
                )

    def test_env_example_defines_core_variables(self):
        content = (ROOT / ".env.example").read_text(encoding="utf-8")
        for var in ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "DATABASE_URL"):
            with self.subTest(variable=var):
                self.assertIn(var, content)


if __name__ == "__main__":
    unittest.main()
