import json

import pytest
from pydantic import ValidationError

from competitionops.config import Settings
from competitionops.schemas import CompetitionBrief
from competitionops.services.brief_extractor import BriefExtractor


def _extract(content: str, source_uri: str | None = None) -> CompetitionBrief:
    return BriefExtractor(Settings()).extract_from_text(content, source_uri=source_uri)


def test_extract_brief_detects_deadline_and_deliverable() -> None:
    content = """
    RunSpace Innovation Challenge
    Submission deadline: 2026-06-15
    Required: 10-page pitch deck and 90-second video.
    Rubric: innovation, business feasibility, impact.
    """
    brief = _extract(content, source_uri="test://brief")

    assert brief.name == "RunSpace Innovation Challenge"
    assert brief.submission_deadline is not None
    assert brief.source_uri == "test://brief"
    assert brief.deliverables
    assert not any(flag == "missing_submission_deadline" for flag in brief.risk_flags)


def test_runspace_like_brief_happy_path() -> None:
    content = """RunSpace Innovation Challenge
Organizer: NYCU Startup Hub
Submission deadline: 2026-06-15
Final event: 2026-07-20
Eligibility: Open to all NYCU students and alumni within 5 years.
Language: English only.
Required deliverables: pitch deck, demo video, prototype.
Rubric: innovation, business feasibility, impact, technical excellence.
"""
    brief = _extract(content, source_uri="test://runspace")

    assert brief.competition_id == "runspace-innovation-challenge"
    assert brief.name == "RunSpace Innovation Challenge"
    assert brief.organizer == "NYCU Startup Hub"
    assert brief.source_uri == "test://runspace"
    assert brief.submission_deadline is not None
    assert brief.submission_deadline.tzinfo is not None
    assert brief.final_event_date is not None
    assert brief.final_event_date.tzinfo is not None
    assert brief.submission_deadline < brief.final_event_date
    assert any("nycu" in item.lower() for item in brief.eligibility)
    assert any("english" in item.lower() for item in brief.language_requirements)
    assert len(brief.deliverables) >= 3
    assert len(brief.scoring_rubric) >= 3
    assert "missing_submission_deadline" not in brief.risk_flags
    assert "missing_deliverables" not in brief.risk_flags


def test_brief_without_deadline_flags_risk() -> None:
    content = "Quiet Competition\nNo schedule yet. Please stay tuned.\n"
    brief = _extract(content)

    assert brief.submission_deadline is None
    assert brief.final_event_date is None
    assert "missing_submission_deadline" in brief.risk_flags


def test_brief_extracts_multiple_deliverables() -> None:
    content = """Multi Deliverable Cup
Submission deadline: 2026/05/01
Required: pitch deck, demo video, business plan, prototype demo.
"""
    brief = _extract(content)

    titles = [d.title.lower() for d in brief.deliverables]
    assert len(brief.deliverables) >= 3
    assert any("pitch" in t or "deck" in t for t in titles)
    assert any("video" in t for t in titles)
    assert any("plan" in t or "business" in t for t in titles)
    # de-duplication: keywords for the same artifact should collapse
    assert len(titles) == len(set(titles))


def test_brief_flags_anonymous_rules_and_format_limits() -> None:
    content = """Anon Pitch Open
Submission deadline: 2026-05-01
Anonymous submission required. Do not include team names or logos.
Pitch deck must not exceed 10 pages.
Video must not exceed 90 seconds.
"""
    brief = _extract(content)

    assert brief.anonymous_rules, "anonymous_rules should be captured verbatim"
    assert any("anonymous" in flag for flag in brief.risk_flags)
    assert any(d.page_limit == 10 for d in brief.deliverables)
    assert any(d.duration_limit_seconds == 90 for d in brief.deliverables)
    assert any("page" in flag for flag in brief.risk_flags)
    assert any("video" in flag or "duration" in flag for flag in brief.risk_flags)


def test_brief_with_malformed_date_does_not_crash() -> None:
    content = """Broken Date Cup
Submission deadline: 2026-13-45
Required: pitch deck.
"""
    brief = _extract(content)

    assert brief.submission_deadline is None
    assert "missing_submission_deadline" in brief.risk_flags


def test_brief_serializes_to_json_with_iso_deadline() -> None:
    content = """JSON Cup
Submission deadline: 2026-05-01
Required: pitch deck.
"""
    brief = _extract(content)

    data = brief.model_dump(mode="json")
    serialized = json.dumps(data)
    reloaded = json.loads(serialized)

    assert isinstance(reloaded["submission_deadline"], str)
    assert reloaded["submission_deadline"].startswith("2026-05-01")
    # round-trip should re-validate cleanly
    assert CompetitionBrief.model_validate(reloaded).name == "JSON Cup"


def test_competition_brief_requires_name_and_id() -> None:
    with pytest.raises(ValidationError):
        CompetitionBrief.model_validate({})
    with pytest.raises(ValidationError):
        CompetitionBrief.model_validate({"competition_id": "demo"})
