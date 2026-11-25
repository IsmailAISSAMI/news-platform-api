"""The lab's flow without a database: it calls the shared analysis and writes files only.

Synthetic answers; the simulated Gemini API is never reached (analysis is replaced).
"""

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import lab, pipeline
from app.analysis.budget import RequestBudget
from app.analysis.llm import GeminiClient
from app.analysis.pipeline import AnalysisOutcome
from tests.analysis.fake_ledger import FakeLedger


class NoDatabase:
    """Stands in for the session: any use of it fails the test."""

    def __getattr__(self, name: str) -> Any:
        msg = f"the lab touched the database ({name})"
        raise AssertionError(msg)


def client(tmp_path: Path) -> GeminiClient:
    async def no_sleep(_: float) -> None:
        return None

    def unused(_: httpx.Request) -> httpx.Response:
        raise AssertionError("the lab test must not reach the network")

    return GeminiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(unused)),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemma-4-31b-it",
        api_key=SecretStr("test-key"),
        budget=RequestBudget(
            per_run=3,
            per_day=1000,
            per_minute=10,
            input_tokens_per_minute=8000,
        ),
        ledger=FakeLedger(),
        sleep=no_sleep,
    )


def outcome(article_id: int, status: str, error: str | None = None) -> AnalysisOutcome:
    return AnalysisOutcome(
        status=cast(Any, status),  # test double: only the fields the lab reads
        outcome=f"#{article_id} {status}",
        title=f"Synthetic {article_id}",
        url=f"https://example.invalid/{article_id}",
        error=error,
        raw_output="{" if status == "failed" else None,
    )


async def test_lab_uses_the_shared_analysis_for_every_article(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "LAB_ROOT", tmp_path / "lab")
    seen: list[int] = []

    async def fake(session: Any, llm: Any, article_id: int, version: str, **_: Any) -> Any:
        seen.append(article_id)
        assert version.startswith("gemma-4-31b-it|")
        return outcome(article_id, "failed", error="invalid output")

    monkeypatch.setattr(pipeline, "analyse_article", fake)

    out, results = await lab.run_lab(cast(AsyncSession, NoDatabase()), client(tmp_path), [11, 12])

    assert seen == [11, 12]
    assert [r.ok for r in results] == [False, False]
    assert sorted(p.name for p in out.iterdir()) == [
        "article-11.json", "article-12.json", "summary.md",
    ]  # fmt: skip


async def test_lab_files_are_private_and_stop_after_a_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "LAB_ROOT", tmp_path / "lab")
    seen: list[int] = []

    async def fake(session: Any, llm: Any, article_id: int, version: str, **_: Any) -> Any:
        seen.append(article_id)
        return outcome(article_id, "stopped", error="quota reached (HTTP 429)")

    monkeypatch.setattr(pipeline, "analyse_article", fake)

    out, results = await lab.run_lab(
        cast(AsyncSession, NoDatabase()), client(tmp_path), [21, 22, 23]
    )

    assert seen == [21]  # nothing more is sent after the stop
    assert "not sent" in (results[1].error or "")
    record = json.loads((out / "article-21.json").read_text())
    assert record["status"] == "stopped"
    assert (out / "article-21.json").stat().st_mode & 0o777 == 0o600
    assert (out / "summary.md").exists()


async def test_the_lab_refuses_a_budget_above_three_answered_requests(tmp_path: Path) -> None:
    llm = client(tmp_path)
    llm.budget.per_run = 4  # what a raised setting would give
    with pytest.raises(ValueError, match=r"lab per-run limit must be 1\.\.3"):
        await lab.run_lab(cast(AsyncSession, NoDatabase()), llm, [1])
