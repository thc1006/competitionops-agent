import re
from datetime import datetime
from zoneinfo import ZoneInfo

from competitionops.config import Settings
from competitionops.schemas import CompetitionBrief, Deliverable, ScoringRubricItem

_TZ = ZoneInfo("Asia/Taipei")

_DATE_PATTERN = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")

_DELIVERABLE_KEYWORDS: list[tuple[str, str]] = [
    ("pitch deck", "Pitch deck"),
    ("pitch", "Pitch deck"),
    ("deck", "Pitch deck"),
    ("proposal", "Proposal document"),
    ("business plan", "Business plan"),
    ("plan", "Business plan"),
    ("video", "Video submission"),
    ("prototype", "Prototype demo"),
    ("demo", "Prototype demo"),
    ("report", "Report document"),
    ("簡報", "Pitch deck"),
    ("提案", "Proposal document"),
    ("影片", "Video submission"),
    ("報告", "Report document"),
]

_RUBRIC_KEYWORDS = [
    "innovation",
    "business",
    "feasibility",
    "impact",
    "technical",
    "execution",
    "creativity",
    "創新",
    "商業",
    "影響",
    "技術",
]


class BriefExtractor:
    """MVP deterministic extractor.

    Operates on text only. Never reaches out to the network or external APIs.
    Later phases will swap in LLM structured extraction and Docling/Crawl4AI
    ingestion behind the same interface.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract_from_text(
        self, content: str, source_uri: str | None = None
    ) -> CompetitionBrief:
        name = self._guess_name(content)
        organizer = self._guess_organizer(content)
        submission_deadline = self._guess_submission_deadline(content)
        final_event_date = self._guess_final_event_date(content)
        deliverables = self._guess_deliverables(content)
        rubric = self._guess_rubric(content)
        eligibility = self._guess_eligibility(content)
        anonymous_rules = self._guess_anonymous_rules(content)
        language_requirements = self._guess_language_requirements(content)

        risk_flags: list[str] = []
        if submission_deadline is None:
            risk_flags.append("missing_submission_deadline")
        if not deliverables:
            risk_flags.append("missing_deliverables")
        if anonymous_rules:
            risk_flags.append("anonymous_submission")
        if any(d.page_limit is not None for d in deliverables):
            risk_flags.append("page_limit")
        if any(d.duration_limit_seconds is not None for d in deliverables):
            risk_flags.append("video_duration_limit")

        return CompetitionBrief(
            competition_id=self._slugify(name),
            name=name,
            organizer=organizer,
            source_uri=source_uri,
            submission_deadline=submission_deadline,
            final_event_date=final_event_date,
            eligibility=eligibility,
            deliverables=deliverables,
            scoring_rubric=rubric,
            anonymous_rules=anonymous_rules,
            language_requirements=language_requirements,
            risk_flags=risk_flags,
        )

    def _guess_name(self, content: str) -> str:
        for line in content.splitlines():
            cleaned = line.strip()
            if cleaned and len(cleaned) <= 120:
                return cleaned
        return "Untitled Competition"

    def _guess_organizer(self, content: str) -> str | None:
        for pattern in (
            r"^\s*Organizer\s*[:：]\s*(.+)$",
            r"^\s*Host(?:ed by)?\s*[:：]\s*(.+)$",
            r"^\s*主辦(?:單位)?\s*[:：]\s*(.+)$",
        ):
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if match:
                value = match.group(1).strip()
                if value:
                    return value
        return None

    def _parse_date(self, text: str, hour: int = 23, minute: int = 59) -> datetime | None:
        match = _DATE_PATTERN.search(text)
        if not match:
            return None
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, hour, minute, tzinfo=_TZ)
        except ValueError:
            return None

    def _guess_submission_deadline(self, content: str) -> datetime | None:
        for line in content.splitlines():
            lowered = line.lower()
            if any(token in lowered for token in ("deadline", "submit", "截止", "繳交")):
                parsed = self._parse_date(line)
                if parsed is not None:
                    return parsed
        return self._parse_date(content)

    def _guess_final_event_date(self, content: str) -> datetime | None:
        for line in content.splitlines():
            lowered = line.lower()
            if any(
                token in lowered
                for token in ("final event", "final pitch", "demo day", "決賽", "頒獎")
            ):
                parsed = self._parse_date(line, hour=18, minute=0)
                if parsed is not None:
                    return parsed
        return None

    def _guess_deliverables(self, content: str) -> list[Deliverable]:
        lowered = content.lower()
        seen: dict[str, Deliverable] = {}
        for keyword, title in _DELIVERABLE_KEYWORDS:
            if keyword.lower() in lowered and title not in seen:
                seen[title] = Deliverable(
                    title=title,
                    description=f"Detected requirement related to {keyword}.",
                )
        deliverables = list(seen.values())

        page_match = re.search(r"(\d{1,3})\s*-?\s*(?:page|頁)", content, re.IGNORECASE)
        if page_match:
            pages = int(page_match.group(1))
            for deliverable in deliverables:
                if any(
                    token in deliverable.title.lower()
                    for token in ("deck", "proposal", "plan", "report")
                ):
                    deliverable.page_limit = pages
                    break

        duration_match = re.search(
            r"(\d{1,4})\s*-?\s*(?:second|sec|秒)", content, re.IGNORECASE
        )
        if duration_match:
            seconds = int(duration_match.group(1))
            for deliverable in deliverables:
                if "video" in deliverable.title.lower():
                    deliverable.duration_limit_seconds = seconds
                    break

        return deliverables[:8]

    def _guess_rubric(self, content: str) -> list[ScoringRubricItem]:
        lowered = content.lower()
        seen: set[str] = set()
        items: list[ScoringRubricItem] = []
        for keyword in _RUBRIC_KEYWORDS:
            if keyword.lower() in lowered and keyword.lower() not in seen:
                seen.add(keyword.lower())
                items.append(
                    ScoringRubricItem(title=keyword, description="Detected rubric keyword.")
                )
        return items

    def _guess_eligibility(self, content: str) -> list[str]:
        items: list[str] = []
        for line in content.splitlines():
            cleaned = line.strip()
            match = re.match(
                r"^(?:Eligibility|Who can apply|資格(?:要求)?)\s*[:：]\s*(.+)$",
                cleaned,
                re.IGNORECASE,
            )
            if match:
                items.append(match.group(1).strip())
        return items

    def _guess_anonymous_rules(self, content: str) -> list[str]:
        rules: list[str] = []
        for line in content.splitlines():
            lowered = line.lower()
            if "anonymous" in lowered or "匿名" in line:
                cleaned = line.strip()
                if cleaned and cleaned not in rules:
                    rules.append(cleaned)
        return rules

    def _guess_language_requirements(self, content: str) -> list[str]:
        items: list[str] = []
        for line in content.splitlines():
            cleaned = line.strip()
            match = re.match(
                r"^(?:Language|語言(?:要求)?)\s*[:：]\s*(.+)$",
                cleaned,
                re.IGNORECASE,
            )
            if match:
                items.append(match.group(1).strip())
        return items

    def _slugify(self, name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
        return slug or "untitled-competition"
