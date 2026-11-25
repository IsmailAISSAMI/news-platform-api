"""Guards on the GitHub workflows (docs/architecture.md 5.6 and 9). No network, no YAML dep."""

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_ci_never_receives_secrets_or_real_data() -> None:
    ci = _text("ci.yml")

    assert "secrets." not in ci
    assert "schedule:" not in ci


def test_ingest_is_scheduled_and_never_runs_on_pull_requests() -> None:
    ingest = _text("ingest.yml")

    assert "schedule:" in ingest
    assert '- cron: "47 22 * * *"' in ingest  # once a day, 23:47 in Morocco
    assert "pull_request" not in ingest
    assert "permissions:\n  contents: read" in ingest
    assert "cancel-in-progress: false" in ingest


def test_ingest_only_ingests_with_the_database_url_secret() -> None:
    ingest = _text("ingest.yml")

    assert "DATABASE_URL: ${{ secrets.DATABASE_URL }}" in ingest
    assert ingest.count("secrets.") == 1  # no Gemini key
    for forbidden in ("news analyze", "news lab", "pytest", "alembic upgrade", "--rewalk"):
        assert forbidden not in ingest
    assert "uv run --no-dev news ingest --since-days 1" in ingest


def _run_block(workflow: str, step_name: str) -> list[str]:
    """The lines of a step's `run: |` block (stdlib text slicing; comments dropped)."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}")
    step_indent = len(lines[start]) - len(lines[start].lstrip())
    block: list[str] = []
    run_indent: int | None = None
    for line in lines[start + 1 :]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= step_indent:
            break  # the next step
        if run_indent is None:
            if line.strip() == "run: |":
                run_indent = indent
            continue
        if line.strip() and indent <= run_indent:
            break  # another key of the same step
        if line.strip() and not line.strip().startswith("#"):
            block.append(line.strip())
    assert block, f"step {step_name!r} has no run block"
    return block


def test_ingest_refuses_the_neon_pooler_and_plain_text_connections() -> None:
    guard = _run_block(_text("ingest.yml"), "Refuse a pooled or unencrypted database URL")

    pooler = next(line for line in guard if line.startswith("*-pooler.*)"))
    assert "exit 1" in pooler
    tls = next(line for line in guard if "sslmode=require" in line)
    assert tls.endswith(";;") and "exit" not in tls  # the only accepted branch
    fallback = next(line for line in guard if line.startswith("*)"))
    assert "exit 1" in fallback
    assert guard.index(pooler) < guard.index(tls) < guard.index(fallback)


def test_the_guard_parser_ignores_comments_and_other_steps() -> None:
    workflow = (
        "    steps:\n"
        "      - name: Guard\n"
        "        run: |\n"
        "          # *-pooler.*) mentioned in a comment\n"
        "          true\n"
        "      - name: Other\n"
        "        run: |\n"
        "          *-pooler.*) exit 1 ;;\n"
    )
    assert _run_block(workflow, "Guard") == ["true"]


def test_every_action_is_pinned_by_sha() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("- uses:") or line.strip().startswith("uses:"):
                ref = line.split("@", 1)[1].split()[0]
                assert len(ref) == 40, f"{path.name}: {line.strip()}"
