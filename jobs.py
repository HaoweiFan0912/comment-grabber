"""Saved progress for unfinished jobs, and a running count of today's quota use.

Everything lives in the project's state/ folder (not uploaded to git):
- state/jobs/<job id>.json: one file per unfinished job, deleted once the job finishes
- state/quota.json: quota units this app has used today (YouTube's quota day runs on Pacific Time)
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / "state"
JOBS_DIR = STATE_DIR / "jobs"
QUOTA_FILE = STATE_DIR / "quota.json"

try:
    from zoneinfo import ZoneInfo

    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # no time zone database: assume Pacific Standard Time
    PACIFIC = timezone(timedelta(hours=-8))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic, so a crash never leaves a half-written file


@dataclass
class Checkpoint:
    """Everything needed to pick an unfinished job up where it left off."""

    job_id: str
    options: dict
    csv_path: str
    created: str
    updated: str = ""
    search_done: bool = False
    search_page_token: str = ""
    seen_video_ids: list = field(default_factory=list)
    videos: list = field(default_factory=list)  # [{"id", "title", "channel"}]
    video_index: int = 0  # the video being worked on
    video_page_token: str = ""  # next page of that video's comments; "" means its first page
    video_scanned: int = 0  # comments of that video already checked
    progress: dict = field(default_factory=dict)  # fetcher.Progress counters

    @classmethod
    def new(cls, options: dict, csv_path: str) -> Checkpoint:
        created = datetime.now().isoformat(timespec="seconds")
        return cls(job_id=uuid.uuid4().hex[:12], options=options, csv_path=csv_path, created=created)

    @classmethod
    def load(cls, job_id: str) -> Checkpoint:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
            raise ValueError("Unknown job")
        try:
            data = json.loads((JOBS_DIR / f"{job_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("This unfinished job can't be found any more") from None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def path(self) -> Path:
        return JOBS_DIR / f"{self.job_id}.json"

    def save(self) -> None:
        self.updated = datetime.now().isoformat(timespec="seconds")
        _write_json(self.path, asdict(self))

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def summary(self) -> dict:
        p, o = self.progress, self.options
        return {
            "job_id": self.job_id,
            "topic": o.get("topic", ""),
            "title_must_include": o.get("title_must_include", ""),
            "comment_must_include": o.get("comment_must_include", ""),
            "csv_path": self.csv_path,
            "csv_exists": Path(self.csv_path).exists(),
            "updated": self.updated or self.created,
            "search_done": self.search_done,
            "videos_total": len(self.videos),
            "videos_done": p.get("videos_done", 0),
            "comments": p.get("comments", 0),
            "replies": p.get("replies", 0),
        }


def unfinished_jobs() -> list:
    """Unfinished jobs, most recently run first."""
    jobs = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            jobs.append(Checkpoint.load(path.stem))
        except ValueError:
            continue
    return sorted(jobs, key=lambda cp: cp.updated or cp.created, reverse=True)


def _pacific_day() -> str:
    return datetime.now(PACIFIC).date().isoformat()


def quota_used_today() -> int:
    """Quota units this app has used since the last reset. Use of the same key elsewhere isn't counted."""
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(data.get("used", 0)) if data.get("day") == _pacific_day() else 0


def add_quota(units: int) -> None:
    _write_json(QUOTA_FILE, {"day": _pacific_day(), "used": quota_used_today() + units})


def next_quota_reset() -> datetime:
    """The next midnight Pacific Time, when YouTube resets the daily quota."""
    tomorrow = (datetime.now(PACIFIC) + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=PACIFIC)
