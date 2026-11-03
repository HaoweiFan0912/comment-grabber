"""Search YouTube for a topic and save the comments from the matching videos.

Comments are written to a CSV file as they arrive, and an Excel copy is made at the end.
Both the web UI (app.py) and the command line (cli.py) use CommentFetcher.
"""

from __future__ import annotations

import csv
import html
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests
from dotenv import dotenv_values

from keywords import Keywords

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"

API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_COST = 100  # quota units per search.list call
LIST_COST = 1  # quota units per commentThreads.list / comments.list call
DAILY_QUOTA = 10_000
MAX_RETRIES = 3

MAX_VIDEOS_LIMIT = 500  # YouTube search only pages through about 500 results
MAX_COMMENTS_LIMIT = 10_000

ORDER_CHOICES = {"relevance": "Relevance", "viewCount": "View count", "date": "Upload date"}

CSV_COLUMNS = [
    "Type", "Video Title", "Channel", "Author", "Comment", "Likes", "Replies",
    "Published", "Video URL", "Video ID", "Comment ID", "Parent Comment ID",
]
NUMERIC_COLUMNS = {"Likes", "Replies"}
EXCEL_WIDTHS = [9, 36, 18, 18, 70, 8, 8, 19, 44, 14, 28, 28]

_PLACEHOLDER_KEYS = {"", "your_api_key_here", "在这里填入你的API_KEY"}

Log = Callable[[str, str], None]  # (level: info / warn / error / success, message)


def _env(name: str) -> str:
    """Read from .env first, then the environment. Re-read every time, so editing .env needs no restart."""
    value = dotenv_values(PROJECT_DIR / ".env").get(name) or os.environ.get(name) or ""
    return value.strip()


def load_api_key() -> Optional[str]:
    key = _env("YOUTUBE_API_KEY")
    return None if key in _PLACEHOLDER_KEYS else key


def _count(n: int, singular: str, plural: Optional[str] = None) -> str:
    """_count(1, "video") -> "1 video"; _count(2, "reply", "replies") -> "2 replies"."""
    return f"{n:,} {singular if n == 1 else plural or singular + 's'}"


@dataclass
class FetchOptions:
    topic: str
    output_dir: str
    max_videos: int = 50
    max_comments: int = 500  # top-level comments per video, not counting replies; checked, not saved, when filtering
    order: str = "relevance"
    language: str = ""  # preferred language, e.g. en; empty for any
    region: str = ""  # region code, e.g. US; empty for any
    published_after: str = ""  # e.g. 2026-01-01; empty for no limit
    published_before: str = ""
    include_replies: bool = False
    title_must_include: str = ""  # keywords, see keywords.py
    comment_must_include: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> FetchOptions:
        try:
            return cls(
                topic=str(data.get("topic", "")).strip(),
                output_dir=str(data.get("output_dir", "")).strip(),
                max_videos=int(data.get("max_videos", 50)),
                max_comments=int(data.get("max_comments", 500)),
                order=str(data.get("order", "relevance")),
                language=str(data.get("language", "")).strip(),
                region=str(data.get("region", "")).strip().upper(),
                published_after=str(data.get("published_after", "")).strip(),
                published_before=str(data.get("published_before", "")).strip(),
                include_replies=bool(data.get("include_replies", False)),
                title_must_include=str(data.get("title_must_include", "")).strip(),
                comment_must_include=str(data.get("comment_must_include", "")).strip(),
            )
        except (TypeError, ValueError):
            raise ValueError("Max videos and comments per video must be whole numbers") from None

    def validate(self) -> None:
        if not self.topic:
            raise ValueError("Please enter a topic")
        if not self.output_dir:
            raise ValueError("Please choose where to save the files")
        if not 1 <= self.max_videos <= MAX_VIDEOS_LIMIT:
            raise ValueError(f"Max videos must be between 1 and {MAX_VIDEOS_LIMIT}")
        if not 1 <= self.max_comments <= MAX_COMMENTS_LIMIT:
            raise ValueError(f"Comments per video must be between 1 and {MAX_COMMENTS_LIMIT:,}")
        if self.order not in ORDER_CHOICES:
            raise ValueError("Invalid sort order")
        after = _parse_date(self.published_after, "Start date")
        before = _parse_date(self.published_before, "End date")
        if after and before and after > before:
            raise ValueError("Start date can't be after end date")


@dataclass
class Progress:
    videos_total: int = 0
    videos_done: int = 0  # processed videos, including skipped ones
    videos_skipped: int = 0
    videos_title_rejected: int = 0  # search results dropped because the title didn't match
    comments_scanned: int = 0  # top-level comments looked at
    comments: int = 0  # top-level comments saved
    replies: int = 0  # replies saved
    quota_used: int = 0
    current_video: str = ""


@dataclass
class FetchResult:
    status: str = "running"  # done / stopped (by the user) / failed / empty (no videos found)
    message: str = ""
    output_path: Optional[str] = None  # CSV
    excel_path: Optional[str] = None


class FatalError(Exception):
    """An error that ends the whole job (quota used up, bad API key, network down...). What was saved is kept."""


class ApiError(Exception):
    """A single request failed; usually only affects the current video."""

    _FRIENDLY = {
        "commentsDisabled": "comments are turned off",
        "videoNotFound": "video not found or removed",
        "forbidden": "no permission to read the comments",
    }

    def __init__(self, status: int, reasons: set, message: str):
        super().__init__(message)
        self.status = status
        self.reasons = reasons
        self.message = message

    @classmethod
    def from_response(cls, resp: requests.Response) -> ApiError:
        try:
            error = resp.json().get("error", {})
        except ValueError:
            error = {}
        reasons = {e.get("reason", "") for e in error.get("errors", [])}
        reasons |= {d.get("reason", "") for d in error.get("details", []) if isinstance(d, dict)}
        reasons.discard("")
        message = re.sub(r"<[^>]+>", "", error.get("message") or "") or f"HTTP {resp.status_code}"
        return cls(resp.status_code, reasons, message)

    @property
    def retryable(self) -> bool:
        return self.status >= 500 or bool(
            self.reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
        )

    def fatal_message(self) -> Optional[str]:
        if self.reasons & {"quotaExceeded", "dailyLimitExceeded"}:
            return "Today's API quota is used up (it resets at midnight Pacific Time)"
        if self.reasons & {"keyInvalid", "API_KEY_INVALID"} or "API key not valid" in self.message:
            return "The API key is invalid — check YOUTUBE_API_KEY in .env"
        if self.reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            return "YouTube Data API v3 isn't enabled in the Google Cloud project this API key belongs to"
        if self.reasons & {"keyExpired", "ipRefererBlocked"} or any(r.startswith("API_KEY_") for r in self.reasons):
            return "This API key has expired or is restricted — check it in the Google Cloud console"
        return None

    def __str__(self) -> str:
        for reason, text in self._FRIENDLY.items():
            if reason in self.reasons:
                return text
        return self.message


class _Stopped(Exception):
    """The user pressed Stop."""


class CommentFetcher:
    def __init__(self, api_key: str, options: FetchOptions, log: Optional[Log] = None):
        self.api_key = api_key
        self.options = options
        self.progress = Progress()
        self.result = FetchResult()
        self._log = log or (lambda level, message: None)
        self._title_kw = Keywords(options.title_must_include)
        self._comment_kw = Keywords(options.comment_must_include)
        self._stop = threading.Event()
        self._session = requests.Session()
        proxy = _env("PROXY")
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
        self._out = None
        self._writer = None

    def stop(self) -> None:
        """Ask the job to stop; it stops once the request in progress finishes."""
        self._stop.set()

    def run(self) -> FetchResult:
        try:
            self._run()
        except (_Stopped, KeyboardInterrupt):
            status, message = "stopped", "Stopped"
        except FatalError as e:
            status, message = "failed", str(e)
        else:
            status, message = "done", "Done"

        if self.result.output_path:
            self._export_excel()
            message += ". " + self._summary()
        elif status == "done":
            status, message = "empty", self._empty_message()
        return self._finish(status, message)

    def _run(self) -> None:
        opts = self.options
        folder = _prepare_folder(opts.output_dir)
        self._log("info", f'Searching YouTube for "{opts.topic}"…')
        if self._title_kw:
            self._log("info", f"Titles must include: {self._title_kw}")
        if self._comment_kw:
            self._log("info", f"Comments must include: {self._comment_kw}")
        videos = self._search_videos()
        if not videos:
            return

        p = self.progress
        p.videos_total = len(videos)
        notes = []
        if len(videos) < opts.max_videos:
            notes.append("that's all the search returned")
        if p.videos_title_rejected:
            notes.append(f"{p.videos_title_rejected:,} skipped because the title didn't match")
        note = f" ({'; '.join(notes)})" if notes else ""
        self._log("info", f"Found {_count(len(videos), 'video')}{note}. Fetching comments…")

        path = _new_file_path(folder, opts.topic)
        self.result.output_path = str(path)
        with open(path, "w", newline="", encoding="utf-8-sig") as self._out:
            self._writer = csv.DictWriter(self._out, fieldnames=CSV_COLUMNS)
            self._writer.writeheader()
            for index, video in enumerate(videos, 1):
                self._check_stop()
                self._process_video(index, video)

    def _search_videos(self) -> list:
        opts = self.options
        query = opts.topic
        if self._title_kw:
            # Searching for the title keywords too makes matching titles more likely, so fewer pages are needed
            query += " " + " ".join(f'"{t}"' if " " in t else t for t in self._title_kw.terms)
        params = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "order": opts.order,
            "maxResults": 50,
            "relevanceLanguage": opts.language,
            "regionCode": opts.region,
            "publishedAfter": f"{opts.published_after}T00:00:00Z" if opts.published_after else "",
            "publishedBefore": f"{opts.published_before}T23:59:59Z" if opts.published_before else "",
        }
        videos = {}
        seen = set()
        page_token = ""
        while len(videos) < opts.max_videos:
            self._check_stop()
            try:
                data = self._get("search", {**params, "pageToken": page_token}, cost=SEARCH_COST)
            except ApiError as e:
                raise FatalError(f"Search failed: {e}") from None
            items = data.get("items", [])
            for item in items:
                video_id = item.get("id", {}).get("videoId")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                snippet = item.get("snippet", {})
                # Search results come with HTML entities in titles, e.g. &#39;
                title = html.unescape(snippet.get("title", ""))
                if not self._title_kw.match(title):
                    self.progress.videos_title_rejected += 1
                    continue
                videos[video_id] = {
                    "id": video_id,
                    "title": title,
                    "channel": html.unescape(snippet.get("channelTitle", "")),
                }
            if self._title_kw:
                self._log("info", f"Checked {_count(len(seen), 'search result')}, {len(videos):,} with matching titles")
            page_token = data.get("nextPageToken", "")
            if not items or not page_token:
                break
        return list(videos.values())[: opts.max_videos]

    def _process_video(self, index: int, video: dict) -> None:
        p = self.progress
        p.current_video = video["title"]
        label = f'[{index}/{p.videos_total}] "{video["title"]}"'
        scanned_before, comments_before, replies_before = p.comments_scanned, p.comments, p.replies

        def stats() -> str:
            return self._video_stats(
                p.comments_scanned - scanned_before, p.comments - comments_before, p.replies - replies_before
            )

        try:
            self._fetch_comments(video)
        except ApiError as e:
            if p.comments_scanned == scanned_before:
                p.videos_skipped += 1
                self._log("warn", f"{label} skipped: {e}")
            else:
                self._log("warn", f"{label} interrupted ({e}) after {stats()}")
        except (_Stopped, FatalError, KeyboardInterrupt):
            if p.comments_scanned > scanned_before:
                p.videos_done += 1
                self._log("info", f"{label} stopped partway: {stats()}")
            raise
        else:
            self._log("info", f"{label} {stats()}")
        p.videos_done += 1

    def _video_stats(self, scanned: int, comments: int, replies: int) -> str:
        if self._comment_kw:
            text = f"checked {_count(scanned, 'comment')}, {comments:,} matched"
            if self.options.include_replies:
                text += f", {_count(replies, 'matching reply', 'matching replies')}"
        else:
            text = _count(comments, "comment")
            if self.options.include_replies:
                text += f", {_count(replies, 'reply', 'replies')}"
        return text

    def _fetch_comments(self, video: dict) -> None:
        opts = self.options
        params = {
            "part": "snippet,replies" if opts.include_replies else "snippet",
            "videoId": video["id"],
            "maxResults": 100,
            "order": "relevance",
            "textFormat": "plainText",
        }
        scanned = 0
        page_token = ""
        while scanned < opts.max_comments:
            self._check_stop()
            data = self._get("commentThreads", {**params, "pageToken": page_token})
            for thread in data.get("items", []):
                if scanned >= opts.max_comments:
                    break
                scanned += 1
                self.progress.comments_scanned += 1
                snippet = thread["snippet"]
                top = snippet["topLevelComment"]
                rows = []
                if self._comment_kw.match(top.get("snippet", {}).get("textDisplay", "")):
                    rows.append(self._row(video, top, "comment", reply_count=snippet.get("totalReplyCount", 0)))
                # Replies are checked on their own, whether or not the comment above them matched
                rows += self._replies(video, thread)
                self._write(rows)
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break

    def _replies(self, video: dict, thread: dict) -> list:
        total = thread["snippet"].get("totalReplyCount", 0)
        if not self.options.include_replies or not total:
            return []
        parent_id = thread["id"]
        inline = thread.get("replies", {}).get("comments", [])
        # A comment thread includes at most 5 replies; fetch the rest page by page
        replies = inline if len(inline) >= total else self._fetch_all_replies(parent_id) or inline
        return [
            self._row(video, reply, "reply", parent_id=parent_id)
            for reply in replies
            if self._comment_kw.match(reply.get("snippet", {}).get("textDisplay", ""))
        ]

    def _fetch_all_replies(self, parent_id: str) -> list:
        replies = []
        page_token = ""
        try:
            while True:
                self._check_stop()
                data = self._get("comments", {
                    "part": "snippet",
                    "parentId": parent_id,
                    "maxResults": 100,
                    "textFormat": "plainText",
                    "pageToken": page_token,
                })
                replies += data.get("items", [])
                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break
        except ApiError as e:
            self._log("warn", f"Couldn't get all replies to one comment: {e}")
        return replies

    def _row(self, video: dict, comment: dict, kind: str, reply_count="", parent_id: str = "") -> dict:
        s = comment.get("snippet", {})
        return {
            "Type": kind,
            "Video Title": video["title"],
            "Channel": video["channel"],
            "Author": s.get("authorDisplayName", ""),
            "Comment": s.get("textDisplay", ""),
            "Likes": s.get("likeCount", 0),
            "Replies": reply_count,
            "Published": _local_time(s.get("publishedAt", "")),
            "Video URL": f"https://www.youtube.com/watch?v={video['id']}",
            "Video ID": video["id"],
            "Comment ID": comment.get("id", ""),
            "Parent Comment ID": parent_id,
        }

    def _write(self, rows: Iterable[dict]) -> None:
        for row in rows:
            self._writer.writerow(row)
            if row["Type"] == "comment":
                self.progress.comments += 1
            else:
                self.progress.replies += 1
        self._out.flush()  # write as we go, so nothing fetched is lost if the job fails midway

    def _get(self, endpoint: str, params: dict, cost: int = LIST_COST) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["key"] = self.api_key
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
            except requests.RequestException as e:
                # Never log the exception text: it contains the URL, which contains the API key
                if attempt == MAX_RETRIES:
                    raise FatalError("Can't reach YouTube — check your network, or set PROXY in .env if you need a proxy") from None
                problem = f"Network error ({type(e).__name__})"
            else:
                self.progress.quota_used += cost
                if resp.ok:
                    try:
                        return resp.json()
                    except ValueError:
                        raise ApiError(resp.status_code, set(), "YouTube sent a response that couldn't be read") from None
                error = ApiError.from_response(resp)
                fatal = error.fatal_message()
                if fatal:
                    raise FatalError(fatal)
                if not error.retryable or attempt == MAX_RETRIES:
                    raise error
                problem = f"YouTube had a temporary error ({resp.status_code})"
            wait = 2 ** (attempt + 1)
            self._log("warn", f"{problem}, retrying in {wait}s…")
            if self._stop.wait(wait):
                raise _Stopped
        raise AssertionError("unreachable")

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise _Stopped

    def _export_excel(self) -> None:
        self._log("info", "Creating the Excel file…")
        try:
            self.result.excel_path = str(csv_to_excel(Path(self.result.output_path)))
        except Exception as e:  # Excel is just for convenience; the CSV is complete either way
            self._log("warn", f"Couldn't create the Excel file ({e}); the CSV file is fine")

    def _summary(self) -> str:
        p = self.progress
        parts = [f"Processed {_count(p.videos_done, 'video')}"]
        if self._comment_kw:
            parts.append(f"checked {_count(p.comments_scanned, 'comment')}")
            saved = _count(p.comments, "matching comment")
            if self.options.include_replies:
                saved += f" and {_count(p.replies, 'matching reply', 'matching replies')}"
        else:
            saved = _count(p.comments, "comment")
            if self.options.include_replies:
                saved += f" and {_count(p.replies, 'reply', 'replies')}"
        parts.append(f"saved {saved}")
        parts.append(f"used about {p.quota_used:,} quota units")
        return ", ".join(parts)

    def _empty_message(self) -> str:
        rejected = self.progress.videos_title_rejected
        if self._title_kw and rejected:
            return (f"None of the {_count(rejected, 'search result')} had a title containing all of: "
                    f"{self._title_kw}. Try fewer or different title keywords")
        return "No videos found. Try a different topic or loosen the filters"

    def _finish(self, status: str, message: str) -> FetchResult:
        self.result.status = status
        self.result.message = message
        self.progress.current_video = ""
        level = {"done": "success", "stopped": "warn", "empty": "warn"}.get(status, "error")
        self._log(level, message)
        return self.result


def csv_to_excel(csv_path: Path) -> Path:
    """Convert the CSV to .xlsx.

    When Excel opens a CSV it treats text starting with @, -, + or = as a formula (an author named
    @someone shows up as #NAME?). In .xlsx every text cell is stored as text, so that can't happen.
    """
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Comments")
    ws.freeze_panes = "A2"
    for index, width in enumerate(EXCEL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(index)].width = width

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        header_cells = []
        for name in header:
            cell = WriteOnlyCell(ws, value=name)
            cell.font = Font(bold=True)
            header_cells.append(cell)
        ws.append(header_cells)
        numeric = [name in NUMERIC_COLUMNS for name in header]
        for row in reader:
            cells = []
            for value, is_number in zip(row, numeric):
                if is_number and value.isdigit():
                    cells.append(int(value))
                    continue
                cell = WriteOnlyCell(ws, value=ILLEGAL_CHARACTERS_RE.sub("", value))
                cell.data_type = "s"  # store comments starting with = as text, not formulas
                cells.append(cell)
            ws.append(cells)

    excel_path = csv_path.with_suffix(".xlsx")
    wb.save(excel_path)
    return excel_path


def _prepare_folder(output_dir: str) -> Path:
    folder = Path(output_dir).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FatalError(f"Can't create the folder {folder} ({e.strerror or e})") from None
    if not os.access(folder, os.W_OK):
        raise FatalError(f"No permission to write to {folder}")
    return folder


def _new_file_path(folder: Path, topic: str) -> Path:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", topic).strip("._")[:40] or "youtube"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = folder / f"{name}_{stamp}.csv"
    n = 2
    while path.exists() or path.with_suffix(".xlsx").exists():
        path = folder / f"{name}_{stamp}_{n}.csv"
        n += 1
    return path


def _local_time(value: str) -> str:
    """Turn the API's UTC timestamp into local time, e.g. 2026-11-02 21:40:05."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _parse_date(value: str, label: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must look like 2026-01-01") from None
