import json
from pathlib import Path

from app.analysis.compare import render, score_run

PARAGRAPHS = {1: ["The ministry announced the alert system on Monday in Rabat."]}
QUOTE = {"paragraph": 1, "quote": "announced the alert system on Monday"}


def analysis(quote: dict[str, object]) -> dict[str, object]:
    return {
        "overview": {"headline": "h", "report_type": "new_event",
                     "event_status": "completed"},
        "what": {"main_event": "x"},
        "who": [{"name_as_written": "ministry", "full_name": "ministry", "type": "government_body",
                 "role": "announced", "involvement": "central", "evidence": quote}],
        "when": {"event_date": {"value": "2026-11-23", "evidence": quote}},
        "where": [{"name_as_written": "Rabat", "role": "event_location", "evidence": quote}],
        "claims": [], "how": None, "topics": [],
        "tone": {"label": "neutral", "explanation": "x"},
    }  # fmt: skip


def write(run: Path, name: str, record: dict[str, object]) -> None:
    run.mkdir(exist_ok=True)
    (run / name).write_text(json.dumps(record))


def test_scores_valid_invalid_and_fabricated(tmp_path: Path) -> None:
    run = tmp_path / "run-a"
    write(run, "article-1.json", {"article_id": 1, "duration_s": 10, "analysis": analysis(QUOTE)})
    invented = {"paragraph": 1, "quote": "the ministry announced a curfew overnight"}
    write(run, "article-2.json", {"article_id": 1, "duration_s": 30,
                                  "analysis": analysis(invented)})  # fmt: skip
    write(run, "article-3.json", {"article_id": 1, "error": "invalid output"})
    write(run, "article-4.json", {"article_id": 1, "analysis": {"old": "schema v1"}})

    score = score_run(run, PARAGRAPHS)

    assert score.counts["valid"] == 2
    assert score.counts["invalid"] == 2  # an error record and an incompatible old schema
    assert score.counts["fabricated"] == 3  # who, when and where in the second record
    assert score.counts["dated_events"] == 2
    assert score.avg_seconds == 20
    assert "verified %" in render([score])
