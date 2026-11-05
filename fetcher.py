"""Search YouTube for a topic and save the comments from the matching videos.

Comments are written to a CSV file as they arrive, and an Excel copy is made at the end.
Progress is saved after every page, so a job that stops (Stop button, quota used up, network
down...) can be continued later without searching again or saving duplicates.
A single search only reaches about 500 videos, so bigger jobs run in batches: search for a batch,
collect its comments, then search again over stretches of time, skipping videos already seen.
Both the web UI (app.py) and the command line (cli.py) use CommentFetcher.
"""

from __future__ import annotations

import csv
import html
import math
import os
import re
import threading
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests
from dotenv import dotenv_values

from jobs import Checkpoint, add_quota, next_quota_reset, unfinished_jobs
from keywords import Keywords

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"

API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_COST = 100  # quota units per search.list call
LIST_COST = 1  # quota units per commentThreads.list / comments.list call
DAILY_QUOTA = 10_000
MAX_RETRIES = 3

MAX_VIDEOS_LIMIT = 10_000
MAX_COMMENTS_LIMIT = 10_000
BATCH_SIZE = 500  # YouTube search only pages through about 500 results, so bigger jobs run in batches
SATURATED_RESULTS = 400  # a search that returned this many probably has more that YouTube won't show
YOUTUBE_START = "2005-04-23"  # the first video on YouTube
EXCEL_MAX_ROWS = 1_048_575  # Excel's sheet limit, not counting the header row

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
    wait_for_quota: bool = False  # when the daily quota runs out, wait for the reset and keep going

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
                wait_for_quota=bool(data.get("wait_for_quota", False)),
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
    videos_done: int = 0  # finished videos, including skipped ones
    videos_skipped: int = 0
    videos_title_rejected: int = 0  # search results dropped because the title didn't match
    comments_scanned: int = 0  # top-level comments looked at
    comments: int = 0  # top-level comments saved
    replies: int = 0  # replies saved
    quota_used: int = 0  # by this job, over all its runs
    current_video: str = ""
    waiting_until: str = ""  # local time the job resumes at, while waiting for the quota to reset
    batch: int = 0  # current batch number; 0 when the job isn't batched
    searching: bool = False


@dataclass
class FetchResult:
    status: str = "running"  # done / stopped (by the user) / failed / empty (no videos found)
    message: str = ""
    output_path: Optional[str] = None  # CSV
    excel_path: Optional[str] = None
    resumable: bool = False  # saved as an unfinished job that can be continued
    job_id: Optional[str] = None


class FatalError(Exception):
    """An error that ends the run (bad API key, network down...). Saved comments and progress are kept."""


class QuotaExceeded(FatalError):
    """The daily quota is used up."""


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

    def fatal_error(self) -> Optional[FatalError]:
        if self.reasons & {"quotaExceeded", "dailyLimitExceeded"}:
            return QuotaExceeded("Today's API quota is used up (it resets at midnight Pacific Time)")
        if self.reasons & {"keyInvalid", "API_KEY_INVALID"} or "API key not valid" in self.message:
            return FatalError("The API key is invalid — check YOUTUBE_API_KEY in .env")
        if self.reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            return FatalError("YouTube Data API v3 isn't enabled in the Google Cloud project this API key belongs to")
        if self.reasons & {"keyExpired", "ipRefererBlocked"} or any(r.startswith("API_KEY_") for r in self.reasons):
            return FatalError("This API key has expired or is restricted — check it in the Google Cloud console")
        return None

    def __str__(self) -> str:
        for reason, text in self._FRIENDLY.items():
            if reason in self.reasons:
                return text
        return self.message


class _Stopped(Exception):
    """The user pressed Stop."""


class CommentFetcher:
    def __init__(self, api_key: str, options: FetchOptions, log: Optional[Log] = None,
                 checkpoint: Optional[Checkpoint] = None):
        self.api_key = api_key
        self.options = options
        self.cp = checkpoint  # None until a new job has picked its file name
        self.resumed = checkpoint is not None
        self.progress = _restore_progress(checkpoint.progress) if checkpoint else Progress()
        self.result = FetchResult(output_path=checkpoint.csv_path if checkpoint else None)
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
        self._entry_index = 0  # first video handled since the last (re)start, which may be half done
        self._saved_ids: set = set()  # comment IDs already in the CSV for the current video

    @classmethod
    def resume(cls, api_key: str, job_id: str, log: Optional[Log] = None,
               wait_for_quota: Optional[bool] = None) -> CommentFetcher:
        checkpoint = Checkpoint.load(job_id)
        options = FetchOptions.from_dict(checkpoint.options)
        if wait_for_quota is not None:
            options.wait_for_quota = wait_for_quota
        return cls(api_key, options, log=log, checkpoint=checkpoint)

    @property
    def job_id(self) -> Optional[str]:
        return self.cp.job_id if self.cp else None

    def stop(self) -> None:
        """Ask the job to stop; it stops once the request in progress finishes."""
        self._stop.set()

    def run(self) -> FetchResult:
        if self.resumed:
            self._log("info", self._resume_message())
        while True:
            try:
                self._run()
            except QuotaExceeded as e:
                if self.options.wait_for_quota:
                    try:
                        self._wait_for_quota_reset()
                        continue
                    except (_Stopped, KeyboardInterrupt):
                        status, message = "stopped", "Stopped"
                else:
                    status, message = "failed", str(e)
            except (_Stopped, KeyboardInterrupt):
                status, message = "stopped", "Stopped"
            except FatalError as e:
                status, message = "failed", str(e)
            else:
                status, message = "done", "Done"
            break
        return self._end(status, message)

    def _run(self) -> None:
        opts, p = self.options, self.progress
        folder = _prepare_folder(opts.output_dir)
        if self.cp is None:
            taken = {job.csv_path for job in unfinished_jobs()}
            self.cp = Checkpoint.new(asdict(opts), str(_new_file_path(folder, opts.topic, taken)))
            self.result.output_path = self.cp.csv_path
            if opts.max_videos > BATCH_SIZE:
                self.cp.batched = True
                self.cp.search_range = [opts.published_after or YOUTUBE_START, opts.published_before or date.today().isoformat()]
            self._log("info", f'Searching YouTube for "{opts.topic}"…')
            if self.cp.batched:
                self._log("info", f"{opts.max_videos:,} videos is more than one search can reach, "
                                  f"so they'll be collected in batches of about {BATCH_SIZE}")
            if self._title_kw:
                self._log("info", f"Titles must include: {self._title_kw}")
            if self._comment_kw:
                self._log("info", f"Comments must include: {self._comment_kw}")
        cp = self.cp
        self._entry_index = cp.video_index
        p.videos_total = self._videos_total()

        while True:
            if not cp.search_done and cp.video_index >= cp.batch_end:
                self._search_batch()
            if cp.search_done:
                cp.batch_end = len(cp.videos)
            p.videos_total = self._videos_total()
            self._collect_batch()
            if cp.search_done:
                return

    def _collect_batch(self) -> None:
        cp = self.cp
        if cp.video_index >= cp.batch_end:
            return
        with open(cp.csv_path, "a", newline="", encoding="utf-8-sig") as self._out:
            self._writer = csv.DictWriter(self._out, fieldnames=CSV_COLUMNS)
            if self._out.tell() == 0:
                self._writer.writeheader()
            while cp.video_index < cp.batch_end:
                self._check_stop()
                self._process_video(cp.video_index)
                cp.video_index += 1
                cp.video_page_token, cp.video_scanned = "", 0
                self._save_checkpoint()

    def _videos_total(self) -> int:
        cp = self.cp
        return self.options.max_videos if cp.batched and not cp.search_done else len(cp.videos)

    def _search_batch(self) -> None:
        """Search until the next batch of videos is ready, or there's nothing left to find."""
        cp, opts, p = self.cp, self.options, self.progress
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
        }
        p.searching = True
        if cp.batched:
            p.batch = cp.batches_done + 1
        target = min(cp.batch_end + BATCH_SIZE, opts.max_videos)
        seen = set(cp.seen_video_ids)
        restarted = False
        while len(cp.videos) < target:
            self._check_stop()
            start, end = cp.search_window or (opts.published_after, opts.published_before)
            params["publishedAfter"] = f"{start}T00:00:00Z" if start else ""
            params["publishedBefore"] = f"{end}T23:59:59Z" if end else ""
            try:
                data = self._get("search", {**params, "pageToken": cp.search_page_token}, cost=SEARCH_COST)
            except ApiError as e:
                if cp.search_page_token and e.status == 400 and not restarted:
                    # Saved positions in search results can expire; videos already found are kept
                    self._log("warn", "The saved position in the search results has expired; searching again from the start")
                    cp.search_page_token, restarted = "", True
                    continue
                raise FatalError(f"Search failed: {e}") from None
            items = data.get("items", [])
            cp.window_results += len(items)
            for item in items:
                video_id = item.get("id", {}).get("videoId")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                cp.seen_video_ids.append(video_id)
                snippet = item.get("snippet", {})
                # Search results come with HTML entities in titles, e.g. &#39;
                title = html.unescape(snippet.get("title", ""))
                if not self._title_kw.match(title):
                    p.videos_title_rejected += 1
                    continue
                cp.videos.append({
                    "id": video_id,
                    "title": title,
                    "channel": html.unescape(snippet.get("channelTitle", "")),
                })
            if self._title_kw:
                self._log("info", f"Checked {_count(len(seen), 'search result')}, {len(cp.videos):,} with matching titles")
            cp.search_page_token = data.get("nextPageToken", "")
            if (not items or not cp.search_page_token) and not self._next_window():
                cp.search_done = True
                break
            self._save_checkpoint()

        del cp.videos[opts.max_videos:]
        if len(cp.videos) >= opts.max_videos:
            cp.search_done = True
        found = len(cp.videos) - cp.batch_end
        cp.batch_end = len(cp.videos)
        if found:
            cp.batches_done += 1
        p.searching = False
        self._save_checkpoint()
        self._log_found(found)

    def _next_window(self) -> bool:
        """Move on to the next stretch of time to search. Returns False when there's none left."""
        cp = self.cp
        if not cp.batched:
            return False
        current = cp.search_window or cp.search_range
        if cp.window_results >= SATURATED_RESULTS and _days_in(current) > 1:
            cp.saturated_windows.append(current)
        cp.window_results = 0
        if not cp.pending_windows and cp.saturated_windows:
            # Only windows with more than YouTube would show are worth looking at more closely
            remaining = self.options.max_videos - len(cp.videos)
            parts = max(2, math.ceil(remaining / (BATCH_SIZE * len(cp.saturated_windows))))
            cp.pending_windows = [w for window in cp.saturated_windows for w in _split_window(window, parts)]
            cp.saturated_windows = []
        if not cp.pending_windows:
            return False
        cp.search_window = cp.pending_windows.pop(0)
        self._log("info", f"Searching videos published {cp.search_window[0]} to {cp.search_window[1]}…")
        return True

    def _log_found(self, found: int) -> None:
        cp, opts, p = self.cp, self.options, self.progress
        rejected = f"{p.videos_title_rejected:,} skipped because the title didn't match"
        if cp.batched:
            if found:
                note = f" ({rejected} so far)" if p.videos_title_rejected else ""
                self._log("info", f"Batch {p.batch}: found {_count(found, 'new video')}{note}. Fetching comments…")
            if cp.search_done and len(cp.videos) < opts.max_videos:
                self._log("warn", f"YouTube's search has no more new videos: found {len(cp.videos):,} of the {opts.max_videos:,} you asked for")
        elif cp.videos:
            notes = []
            if len(cp.videos) < opts.max_videos:
                notes.append("that's all the search returned")
            if p.videos_title_rejected:
                notes.append(rejected)
            note = f" ({'; '.join(notes)})" if notes else ""
            self._log("info", f"Found {_count(len(cp.videos), 'video')}{note}. Fetching comments…")

    def _process_video(self, index: int) -> None:
        cp, p = self.cp, self.progress
        video = cp.videos[index]
        p.current_video = video["title"]
        label = f'[{index + 1:,}/{p.videos_total:,}] "{video["title"]}"'
        # Only the first video after a (re)start can already have rows in the CSV
        self._saved_ids = self._load_saved_ids(video["id"]) if index == self._entry_index else set()
        scanned_before, comments_before, replies_before = p.comments_scanned, p.comments, p.replies

        def stats() -> str:
            return self._video_stats(
                p.comments_scanned - scanned_before, p.comments - comments_before, p.replies - replies_before
            )

        try:
            self._fetch_comments(video)
        except ApiError as e:
            if cp.video_scanned == 0:
                p.videos_skipped += 1
                self._log("warn", f"{label} skipped: {e}")
            else:
                self._log("warn", f"{label} interrupted ({e}) after {stats()}")
        except (_Stopped, FatalError, KeyboardInterrupt):
            if p.comments_scanned > scanned_before:
                self._log("info", f"{label} paused partway: {stats()}")
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
        cp, opts, p = self.cp, self.options, self.progress
        params = {
            "part": "snippet,replies" if opts.include_replies else "snippet",
            "videoId": video["id"],
            "maxResults": 100,
            "order": "relevance",
            "textFormat": "plainText",
        }
        restarted = False
        while cp.video_scanned < opts.max_comments:
            self._check_stop()
            try:
                data = self._get("commentThreads", {**params, "pageToken": cp.video_page_token})
            except ApiError as e:
                if not (cp.video_page_token and e.status == 400 and not restarted):
                    raise
                # Saved positions can expire; start the video over (comments already saved are skipped)
                self._log("warn", "The saved position in this video's comments has expired; checking it again from the start")
                p.comments_scanned -= cp.video_scanned
                cp.video_page_token, cp.video_scanned, restarted = "", 0, True
                continue
            page_start = cp.video_scanned
            try:
                for thread in data.get("items", []):
                    if cp.video_scanned >= opts.max_comments:
                        break
                    cp.video_scanned += 1
                    p.comments_scanned += 1
                    snippet = thread["snippet"]
                    top = snippet["topLevelComment"]
                    rows = []
                    if self._comment_kw.match(top.get("snippet", {}).get("textDisplay", "")):
                        rows.append(self._row(video, top, "comment", reply_count=snippet.get("totalReplyCount", 0)))
                    # Replies are checked on their own, whether or not the comment above them matched
                    rows += self._replies(video, thread)
                    self._write(rows)
            except BaseException:
                # This page will be fetched again next time, so it doesn't count as checked yet
                p.comments_scanned -= cp.video_scanned - page_start
                cp.video_scanned = page_start
                raise
            cp.video_page_token = data.get("nextPageToken", "")
            if not cp.video_page_token:
                break
            self._save_checkpoint()

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
            if row["Comment ID"] in self._saved_ids:
                continue  # already saved before a stop or restart
            self._saved_ids.add(row["Comment ID"])
            self._writer.writerow(row)
            if row["Type"] == "comment":
                self.progress.comments += 1
            else:
                self.progress.replies += 1
        self._out.flush()  # on disk before the checkpoint says this page is done

    def _load_saved_ids(self, video_id: str) -> set:
        path = Path(self.cp.csv_path)
        if not path.exists():
            return set()
        with open(path, newline="", encoding="utf-8-sig") as f:
            return {row["Comment ID"] for row in csv.DictReader(f) if row.get("Video ID") == video_id}

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
                add_quota(cost)
                if resp.ok:
                    try:
                        return resp.json()
                    except ValueError:
                        raise ApiError(resp.status_code, set(), "YouTube sent a response that couldn't be read") from None
                error = ApiError.from_response(resp)
                fatal = error.fatal_error()
                if fatal:
                    raise fatal
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

    def _wait_for_quota_reset(self) -> None:
        self._save_checkpoint()
        resume_at = next_quota_reset() + timedelta(minutes=5)  # a little slack after the reset
        local = resume_at.astimezone()
        when = f"{local:%H:%M}" + (" tomorrow" if local.date() != datetime.now().date() else "")
        self.progress.current_video = ""
        self.progress.waiting_until = when
        self._log("warn", f"Today's quota is used up. Waiting until {when} (your time), after it resets, "
                          "then continuing automatically. Keep the app open and the computer awake")
        # Wait in short steps against the wall clock, so time the computer spends asleep still counts
        while True:
            remaining = (resume_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            if self._stop.wait(min(remaining, 60)):
                raise _Stopped
        self.progress.waiting_until = ""
        self._log("info", "The quota has reset. Continuing…")

    def _save_checkpoint(self) -> None:
        self.cp.progress = asdict(self.progress)
        self.cp.save()

    def _end(self, status: str, message: str) -> FetchResult:
        cp, p = self.cp, self.progress
        p.waiting_until = ""
        p.searching = False
        csv_exists = bool(self.result.output_path) and Path(self.result.output_path).exists()
        if status == "done" and not csv_exists:
            status, message = "empty", self._empty_message()
        # Worth continuing only if the search got somewhere
        resumable = status in ("stopped", "failed") and cp is not None and bool(cp.seen_video_ids)
        if cp is not None:
            if resumable:
                self._save_checkpoint()
            else:
                cp.delete()
        if csv_exists:
            self._export_excel()
            message += ". " + self._summary()
        else:
            self.result.output_path = None
        if resumable:
            message += ". You can continue this job later"
            self.result.resumable, self.result.job_id = True, cp.job_id
        return self._finish(status, message)

    def _resume_message(self) -> str:
        cp, p = self.cp, self.progress
        if not cp.search_done and cp.video_index >= cp.batch_end:
            text = (f'Continuing "{cp.options.get("topic")}": picking the search back up '
                    f"({len(cp.videos):,} videos found so far, {_count(p.comments, 'comment')} saved)…")
        else:
            text = (f'Continuing "{cp.options.get("topic")}": {p.videos_done:,} of {_count(self._videos_total(), "video")} '
                    f"finished, {_count(p.comments, 'comment')} saved so far…")
        if p.comments + p.replies and not Path(cp.csv_path).exists():
            text += f" Note: the CSV file from earlier runs is missing, so new comments go into a new file at {cp.csv_path}"
        return text

    def _export_excel(self) -> None:
        self._log("info", "Creating the Excel file…")
        try:
            excel_path, left_out = csv_to_excel(Path(self.result.output_path))
            self.result.excel_path = str(excel_path)
            if left_out:
                self._log("warn", f"An Excel sheet holds at most {EXCEL_MAX_ROWS:,} rows, so the Excel file leaves out "
                                  f"the last {left_out:,}. The CSV file has everything")
        except Exception as e:  # Excel is just for convenience; the CSV is complete either way
            self._log("warn", f"Couldn't create the Excel file ({e}); the CSV file is fine")

    def _summary(self) -> str:
        cp, p = self.cp, self.progress
        total = self._videos_total()
        finished = _count(total, "video") if p.videos_done >= total else f"{p.videos_done:,} of {_count(total, 'video')}"
        if cp.batched and cp.search_done and total < self.options.max_videos:
            finished += f" (you asked for {self.options.max_videos:,}, but YouTube's search found no more)"
        parts = [f"Finished {finished}"]
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


def _restore_progress(data: dict) -> Progress:
    known = {f.name for f in fields(Progress)}
    progress = Progress(**{k: v for k, v in data.items() if k in known})
    progress.current_video = progress.waiting_until = ""
    progress.searching = False
    return progress


def _days_in(window: list) -> int:
    return (date.fromisoformat(window[1]) - date.fromisoformat(window[0])).days + 1


def _split_window(window: list, parts: int) -> list:
    """Split a [first day, last day] window into equal windows that don't overlap, newest first."""
    first = date.fromisoformat(window[0])
    days = _days_in(window)
    parts = min(parts, days)
    windows = []
    for i in range(parts - 1, -1, -1):
        start = first + timedelta(days=days * i // parts)
        end = first + timedelta(days=days * (i + 1) // parts - 1)
        windows.append([start.isoformat(), end.isoformat()])
    return windows


def csv_to_excel(csv_path: Path) -> tuple:
    """Convert the CSV to .xlsx. Returns the file's path and how many rows didn't fit in the sheet.

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
        written = left_out = 0
        for row in reader:
            if written >= EXCEL_MAX_ROWS:
                left_out += 1
                continue
            written += 1
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
    return excel_path, left_out


def _prepare_folder(output_dir: str) -> Path:
    folder = Path(output_dir).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FatalError(f"Can't create the folder {folder} ({e.strerror or e})") from None
    if not os.access(folder, os.W_OK):
        raise FatalError(f"No permission to write to {folder}")
    return folder


def _new_file_path(folder: Path, topic: str, taken: set) -> Path:
    """A file name that's not in use, and not reserved by an unfinished job that hasn't written yet."""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", topic).strip("._")[:40] or "youtube"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = folder / f"{name}_{stamp}.csv"
    n = 2
    while path.exists() or path.with_suffix(".xlsx").exists() or str(path) in taken:
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
