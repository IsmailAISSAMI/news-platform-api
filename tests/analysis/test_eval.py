"""Gold-set tooling: sampling, checklist items and scoring. Synthetic data only."""

import csv
from pathlib import Path

import pytest

from app import cli
from app.analysis.eval import (
    REPO_ROOT,
    _write_gold,
    gold_items,
    score_gold,
    section_of,
    stratified_sample,
)

Q = {"paragraph": 1, "quote": "x"}
CLEANED: dict[str, object] = {
    "who": [{"name_as_written": "Martin", "full_name": "Martin", "type": "person",
             "role": "visited", "involvement": "central", "evidence": Q}],
    "where": [{"name_as_written": "Agadir", "role": "event_location", "evidence": Q}],
    "when": {"event_date": {"value": "2026-11-23", "evidence": Q},
             "timeline": [{"date": "2026-11-20", "event": "Talks", "evidence": Q}]},
    "claims": [{"text": "Ties grow", "kind": "opinion", "attributed_to": "Martin",
                "evidence": Q}],
    "why": [{"text": "Long cooperation", "kind": "stated", "attributed_to": None,
             "evidence": Q}],
    "impact": [{"area": "economy", "text": "More trade", "certainty": "expected",
                "evidence": Q}],
}  # fmt: skip


def test_gold_items_cover_every_section_in_order() -> None:
    items = gold_items(CLEANED)

    assert [(i.section, i.path) for i in items] == [
        ("who", "$.who[0]"), ("where", "$.where[0]"), ("when", "$.when.event_date"),
        ("when", "$.when.timeline[0]"), ("claims", "$.claims[0]"), ("why", "$.why[0]"),
        ("impact", "$.impact[0]"),
    ]  # fmt: skip
    assert items[4].text == "[opinion] Ties grow (attributed to Martin)"


def test_no_event_date_means_no_item() -> None:
    assert gold_items({"when": {"event_date": {"value": None, "evidence": None}}}) == []


def test_sample_is_stratified_and_deterministic() -> None:
    categories = dict.fromkeys(range(1, 21), "politics") | {21: "sports", 22: "economy"}

    sample = stratified_sample(categories, 5)

    assert sample == stratified_sample(categories, 5)  # same seed, same sample
    assert {21, 22} <= set(sample)  # small categories are represented
    assert len(set(sample)) == 5
    assert stratified_sample(categories, 100) == stratified_sample(categories, 100)
    assert len(stratified_sample(categories, 100)) == 22  # never more than exist


@pytest.mark.parametrize(
    ("path", "section"),
    [("$.who[2]", "who"), ("$.when.timeline[0]", "when"), ("$.when.event_date", "when"),
     ("$.claims[1]", "claims"), ("$.topics[0]", "other")],
)  # fmt: skip
def test_section_of(path: str, section: str) -> None:
    assert section_of(path) == section


def write_labels(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["article_id", "path", "section", "item", "verdict", "note"])
        writer.writerows([("1", p, "", "", v, n) for p, v, n in rows])
    return path


def test_score_gold(tmp_path: Path) -> None:
    labels = write_labels(
        tmp_path / "labels.csv",
        [
            ("$.who[0]", "correct", ""),
            ("$.who[1]", "wrong", "not in the event"),
            ("$.claims[0]", "missing detail", ""),
            ("$.claims[1]", "Correct", ""),
            ("$.why[0]", "", ""),  # not labelled yet
        ],
    )

    report = score_gold(labels)

    assert report.sections["who"].precision == 0.5
    assert report.sections["claims"].precision == 1.0
    assert report.sections["why"].precision is None
    assert report.overall.labelled == 4
    assert report.overall.precision == 0.75
    assert report.unlabelled == 1
    assert report.worst == [("1", "$.who[1]", "not in the event")]
    assert "who" in report.render()


def test_unknown_verdicts_fail_loudly(tmp_path: Path) -> None:
    labels = write_labels(tmp_path / "labels.csv", [("$.who[0]", "maybe", "")])

    with pytest.raises(ValueError, match="unknown verdict"):
        score_gold(labels)


def test_gold_files_never_go_inside_the_repo() -> None:
    with pytest.raises(ValueError, match="outside the repo"):
        _write_gold(REPO_ROOT / "gold", {"labels.csv": ""})


def test_gold_files_are_private(tmp_path: Path) -> None:
    target = _write_gold(tmp_path / "gold", {"labels.csv": "a\n"})

    assert (tmp_path / "gold").stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "labels.csv").stat().st_mode & 0o777 == 0o600


def test_eval_cli_arguments() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["eval", "export"]).n == 30
    assert parser.parse_args(["eval", "export", "--n", "5"]).n == 5
    assert parser.parse_args(["eval", "score", "l.csv"]).labels == Path("l.csv")
    with pytest.raises(SystemExit):
        parser.parse_args(["eval"])


def test_eval_score_needs_an_existing_file(tmp_path: Path) -> None:
    assert cli.main(["eval", "score", str(tmp_path / "missing.csv")]) == cli.EXIT_NOT_READY
