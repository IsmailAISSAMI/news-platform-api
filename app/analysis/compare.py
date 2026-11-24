"""Compare lab runs with the same code checks: the repeatable scorecard for prompt/model changes.

Each run directory is re-scored with the CURRENT checks, so runs made before a check
existed are judged by the same rules as new ones.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.analysis.evidence import EvidenceStatus, semantic_issues, verify
from app.analysis.schema import ArticleAnalysis

METRICS = (
    "valid",
    "invalid",
    "evidence",
    "verified",
    "misplaced",
    "fabricated",
    "issues",
    "who",
    "where",
    "claims",
    "timeline",
    "dated_events",
)


@dataclass
class RunScore:
    name: str
    counts: Counter[str] = field(default_factory=Counter)
    seconds: float = 0.0

    @property
    def verified_rate(self) -> float:
        return self.counts["verified"] / self.counts["evidence"] if self.counts["evidence"] else 0.0

    @property
    def avg_seconds(self) -> float:
        return self.seconds / self.counts["valid"] if self.counts["valid"] else 0.0


def score_run(run_dir: Path, paragraphs_by_id: dict[int, list[str]]) -> RunScore:
    score = RunScore(run_dir.name)
    for path in sorted(run_dir.glob("article-*.json")):
        record = json.loads(path.read_text())
        paragraphs = paragraphs_by_id.get(record["article_id"])
        try:
            if "analysis" not in record or paragraphs is None:
                raise ValueError(record.get("error", "no analysis"))
            analysis = ArticleAnalysis.model_validate(_upgrade(record["analysis"]))
        except (ValueError, ValidationError):
            # Invalid now, or produced under an older, incompatible schema version.
            score.counts["invalid"] += 1
            continue
        report = verify(analysis, paragraphs)
        score.counts["valid"] += 1
        score.counts["evidence"] += report.total
        score.counts["verified"] += report.count(EvidenceStatus.VERIFIED)
        score.counts["misplaced"] += report.count(EvidenceStatus.MISPLACED)
        score.counts["fabricated"] += report.count(EvidenceStatus.FABRICATED)
        score.counts["issues"] += len(semantic_issues(analysis, paragraphs))
        score.counts["who"] += len(analysis.who)
        score.counts["where"] += len(analysis.where)
        score.counts["claims"] += len(analysis.claims)
        score.counts["timeline"] += len(analysis.when.timeline)
        score.counts["dated_events"] += analysis.when.event_date.value is not None
        score.seconds += float(record.get("duration_s", 0))
    return score


def render(scores: list[RunScore]) -> str:
    width = max(14, *(len(s.name) for s in scores))
    header = "metric".ljust(16) + "".join(s.name[-width:].rjust(width + 2) for s in scores)
    lines = [header]
    for metric in METRICS:
        lines.append(
            metric.ljust(16) + "".join(str(s.counts[metric]).rjust(width + 2) for s in scores)
        )
    lines.append(
        "verified %".ljust(16) + "".join(f"{s.verified_rate:.0%}".rjust(width + 2) for s in scores)
    )
    lines.append(
        "avg s/article".ljust(16) + "".join(f"{s.avg_seconds:.0f}".rjust(width + 2) for s in scores)
    )
    return "\n".join(lines) + "\n"


# Place roles removed in schema v5 (rarely right, not needed for the map).
_RETIRED_PLACE_ROLES = {"origin": "related_event", "destination": "related_event"}


def _upgrade(analysis: dict[str, object]) -> dict[str, object]:
    """Bring a record from an older schema version to the current one, where lossless.

    - v3 -> v4: `precision` is now derived from the date value, so it is dropped.
    - v4 -> v5: place roles origin/destination map to related_event.
    - v5 -> v6: `next` is no longer asked of the model (derived from predictions), so it
      is dropped.
    - v6 -> v7: the free-text summary was removed (it caused the worst, uncheckable
      errors), so it is dropped.
    Anything else incompatible still fails validation and counts as invalid.
    """
    analysis.pop("next", None)
    overview = analysis.get("overview")
    if isinstance(overview, dict):
        overview.pop("summary", None)
    when = analysis.get("when")
    if isinstance(when, dict):
        event_date = when.get("event_date")
        if isinstance(event_date, dict):
            event_date.pop("precision", None)
        for entry in when.get("timeline") or []:
            if isinstance(entry, dict):
                entry.pop("precision", None)
    for place in analysis.get("where") or []:  # type: ignore[attr-defined]  # JSON list
        if isinstance(place, dict) and place.get("role") in _RETIRED_PLACE_ROLES:
            place["role"] = _RETIRED_PLACE_ROLES[place["role"]]
    return analysis
