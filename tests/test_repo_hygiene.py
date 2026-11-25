"""Unit tests for scripts/check_repo_hygiene.py.

Detection is tested on synthetic strings. Secret-looking literals are assembled from
pieces so that this file does not itself match the patterns it tests.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_repo_hygiene", REPO_ROOT / "scripts" / "check_repo_hygiene.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks the module up while it executes
    spec.loader.exec_module(module)
    return module


hygiene = _load_script()

GOOGLE_KEY = "AIza" + "S" * 35
OPENAI_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
DB_REAL = "postgresql+psycopg://news:" + "hunter2x" + "@db.example.invalid:5432/news"
PEM = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
QUOTE = "[P" + "1" + "] " + '"' + "A synthetic paragraph." + '"'


@pytest.mark.parametrize(
    "text",
    [
        f"GEMINI_API_KEY={GOOGLE_KEY}",
        f"token: {OPENAI_KEY}",
        f"DATABASE_URL={DB_REAL}",
        PEM,
    ],
)
def test_secret_patterns_are_detected(text: str) -> None:
    findings = hygiene.find_secrets("notes.txt", text)

    assert findings
    assert all(f.check == "secret" for f in findings)


@pytest.mark.parametrize(
    "text",
    [
        "postgresql+psycopg://news:change-me@db:5432/news",
        "postgresql+psycopg://news:ci-password@localhost:5432/news",
        "postgresql+psycopg://news:$POSTGRES_PASSWORD@db:5432/news",
        "AIza-too-short",
        "sk-short",
        "plain prose with no secrets at all",
    ],
)
def test_placeholders_and_plain_text_are_not_secrets(text: str) -> None:
    assert hygiene.find_secrets("README.md", text) == []


def test_secret_finding_reports_line_but_never_the_value() -> None:
    text = "first line\nsecond line\n" + GOOGLE_KEY

    (finding,) = hygiene.find_secrets("app/x.py", text)

    assert "line 3" in finding.detail
    assert GOOGLE_KEY not in str(finding)


@pytest.mark.parametrize(
    "path",
    [
        "backups/nightly.dump",
        "data/news.sql.gz",
        "exports/articles.jsonl",
        ".env",
        "config/.env",
        "reports/news-eval-2026.md",
        "docs/review-notes.md",
    ],
)
def test_forbidden_files_are_detected(path: str) -> None:
    assert hygiene.find_forbidden_name(path)


@pytest.mark.parametrize(
    "path",
    [".env.example", "scripts/db_roles.sql", "docs/architecture.md", "app/cli.py"],
)
def test_allowed_files_are_not_forbidden(path: str) -> None:
    assert hygiene.find_forbidden_name(path) == []


def test_article_quote_marker_is_detected_in_text_formats() -> None:
    for path in ("docs/sample.md", "fixtures/out.json", "notes.txt"):
        findings = hygiene.find_article_quotes(path, f"intro\n{QUOTE}\n")

        assert len(findings) == 1
        assert findings[0].check == "article quote"
        assert "line 2" in findings[0].detail


def test_article_quote_marker_is_ignored_in_other_file_types() -> None:
    assert hygiene.find_article_quotes("app/prompt.py", QUOTE) == []


def test_article_quote_allowlist_covers_only_the_llm_prompt_test() -> None:
    assert {"tests/analysis/test_llm.py"} == hygiene.ARTICLE_QUOTE_ALLOWLIST
    assert hygiene.find_article_quotes("tests/analysis/test_llm.py", QUOTE) == []
    assert hygiene.find_article_quotes("tests/analysis/other.md", QUOTE)


def test_check_file_combines_all_checks() -> None:
    findings = hygiene.check_file("docs/leak.md", f"{OPENAI_KEY}\n{QUOTE}")

    assert {f.check for f in findings} == {"secret", "article quote"}


def test_repository_is_clean() -> None:
    """The real tracked files must pass the gate (exit 0)."""
    assert hygiene.main() == 0
