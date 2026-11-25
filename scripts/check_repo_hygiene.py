"""Fail if tracked files could leak secrets, real data or article text.

Standard library only, no network. It lists tracked files with `git ls-files` and checks:

(a) secret patterns (API keys, a real password in a database URL, private key blocks);
(b) forbidden file names (`.env`, dumps, JSONL exports, news-eval or review outputs);
(c) article-quote markers (`[P12] "...`) in Markdown, JSON and text files.

Run with `uv run python scripts/check_repo_hygiene.py`. Exit 0 when clean, 1 with one line
per finding otherwise.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "OpenAI-style secret key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # A password in a URL. Documented placeholders are excluded: change-me (local template),
    # ci-password (CI-only throwaway database), test-only-password (test fixtures), $VAR.
    "database URL with a real password": re.compile(
        r"postgresql\+psycopg://[^:\s]+:(?!change-me|ci-password|test-only-password|\$)[^@\s]+@"
    ),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Tracked file names that must never be committed (matched against the file name).
FORBIDDEN_NAME_GLOBS: tuple[str, ...] = ("*.dump", "*.sql.gz", "*.jsonl")
# Tracked path fragments that must never be committed (matched against the full path).
FORBIDDEN_PATH_FRAGMENTS: tuple[str, ...] = ("news-eval", "review")
# The only tracked file allowed to be named like `.env`.
ENV_TEMPLATE = ".env.example"

ARTICLE_QUOTE_PATTERN = re.compile(r'\[P\d+\]\s*"')
ARTICLE_QUOTE_SUFFIXES: tuple[str, ...] = (".md", ".json", ".txt")
# Files where a synthetic article-quote rendering is tested on purpose. Keep this list tiny.
ARTICLE_QUOTE_ALLOWLIST: frozenset[str] = frozenset({"tests/analysis/test_llm.py"})


@dataclass(frozen=True)
class Finding:
    path: str
    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.check}: {self.detail}"


def find_secrets(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for label, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            # Never echo the matched value: only where it was found.
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path, "secret", f"{label} at line {line}"))
    return findings


def find_forbidden_name(path: str) -> list[Finding]:
    name = Path(path).name
    findings: list[Finding] = []
    if name == ".env" and path != ENV_TEMPLATE:
        findings.append(Finding(path, "forbidden file", "a .env file must never be committed"))
    for glob in FORBIDDEN_NAME_GLOBS:
        if fnmatch.fnmatch(name, glob):
            findings.append(Finding(path, "forbidden file", f"matches '{glob}' (data or export)"))
    for fragment in FORBIDDEN_PATH_FRAGMENTS:
        if fragment in path:
            findings.append(
                Finding(path, "forbidden file", f"path contains '{fragment}' (evaluation output)")
            )
    return findings


def find_article_quotes(path: str, text: str) -> list[Finding]:
    if not path.endswith(ARTICLE_QUOTE_SUFFIXES) or path in ARTICLE_QUOTE_ALLOWLIST:
        return []
    findings: list[Finding] = []
    for match in ARTICLE_QUOTE_PATTERN.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        findings.append(Finding(path, "article quote", f"paragraph quote marker at line {line}"))
    return findings


def check_file(path: str, text: str) -> list[Finding]:
    """All checks for one tracked file. `text` is the decoded content (may be empty)."""
    return [
        *find_secrets(path, text),
        *find_forbidden_name(path),
        *find_article_quotes(path, text),
    ]


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607  # developer's git on PATH, fixed arguments
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [p for p in result.stdout.decode().split("\0") if p]


def scan(root: Path, paths: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in paths:
        full = root / rel
        if not full.is_file():
            continue  # deleted but still in the index
        text = full.read_bytes().decode("utf-8", errors="ignore")
        findings.extend(check_file(rel, text))
    return findings


def main() -> int:
    findings = scan(REPO_ROOT, tracked_files(REPO_ROOT))
    if not findings:
        sys.stdout.write("Repository hygiene: OK\n")
        return 0
    sys.stderr.write(f"Repository hygiene: {len(findings)} finding(s)\n")
    for finding in findings:
        sys.stderr.write(f"  {finding}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
