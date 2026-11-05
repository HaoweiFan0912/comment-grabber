"""Command-line version.

Usage: python cli.py TOPIC OUTPUT_FOLDER [options]
       python cli.py --resume [N]
Run it without arguments to be asked for the topic and folder; other options use their defaults.
Press Ctrl+C to stop at any time; what was fetched so far is kept and the job can be continued.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Optional

from fetcher import (
    BATCH_SIZE,
    DEFAULT_OUTPUT_DIR,
    MAX_COMMENTS_LIMIT,
    MAX_VIDEOS_LIMIT,
    ORDER_CHOICES,
    CommentFetcher,
    FetchOptions,
    load_api_key,
)
from jobs import next_quota_reset, quota_used_today, unfinished_jobs

PREFIX = {"warn": "! ", "error": "✗ ", "success": "✓ "}

KEYWORD_HELP = (
    "separate keywords with spaces or commas; all must appear, in any order. "
    'Quote phrases with spaces, e.g. \'"battery life" worse\'. English words match from the start of a word'
)


def log(level: str, message: str) -> None:
    print(PREFIX.get(level, "") + message)


def pick_job(choice: str) -> Optional[str]:
    """Return the job ID to continue, asking if there's more than one and no number was given."""
    jobs = unfinished_jobs()
    if not jobs:
        print("No unfinished jobs.")
        return None
    print("Unfinished jobs:")
    for n, cp in enumerate(jobs, 1):
        s = cp.summary()
        if s["search_done"] or s["batched"]:
            where = f"{s['videos_done']:,} of {s['videos_total']:,} videos done"
        else:
            where = f"search not finished ({s['videos_total']:,} videos found so far)"
        updated = datetime.fromisoformat(s["updated"]).strftime("%Y-%m-%d %H:%M")
        print(f'  {n}. "{s["topic"]}": {where}, {s["comments"]:,} comments saved, last run {updated}')
    if not choice:
        if len(jobs) == 1:
            choice = "1"
        else:
            try:
                choice = input(f"Which one? (1–{len(jobs)}): ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return None
    if not choice.isdigit() or not 1 <= int(choice) <= len(jobs):
        print(f"✗ Pick a number from 1 to {len(jobs)}")
        return None
    return jobs[int(choice) - 1].job_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Search YouTube for a topic and save the comments as CSV and Excel")
    parser.add_argument("topic", nargs="?", help="what to search for")
    parser.add_argument("output_dir", nargs="?", help=f"folder to save to (default {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--videos", type=int, default=50,
                        help=f"max videos, 1–{MAX_VIDEOS_LIMIT:,} (default 50); over {BATCH_SIZE} runs in batches")
    parser.add_argument("--comments", type=int, default=500,
                        help=f"comments per video, not counting replies, 1–{MAX_COMMENTS_LIMIT:,} (default 500); "
                             "with --comment-has, how many to check")
    parser.add_argument("--order", choices=list(ORDER_CHOICES), default="relevance",
                        help="sort videos by relevance (default), viewCount or date")
    parser.add_argument("--title-has", default="", metavar="KEYWORDS", help=f"video titles must include: {KEYWORD_HELP}")
    parser.add_argument("--comment-has", default="", metavar="KEYWORDS", help=f"saved comments must include: {KEYWORD_HELP}")
    parser.add_argument("--lang", default="", help="prefer videos in this language, e.g. en, es, zh-Hans")
    parser.add_argument("--region", default="", help="region code, e.g. US, GB")
    parser.add_argument("--after", default="", help="only videos published on or after this date, e.g. 2026-01-01")
    parser.add_argument("--before", default="", help="only videos published on or before this date")
    parser.add_argument("--replies", action="store_true", help="also fetch replies to comments (uses more quota)")
    parser.add_argument("--wait", action="store_true",
                        help="if the daily quota runs out, wait for it to reset and keep going")
    parser.add_argument("--resume", nargs="?", const="", metavar="N",
                        help="continue an unfinished job instead of starting a new one; "
                             "without N, list them (and ask which, if there are several)")
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print("✗ No API key yet: set YOUTUBE_API_KEY in .env")
        return 1
    reset = next_quota_reset().astimezone()
    print(f"Quota used today by this app: {quota_used_today():,} / 10,000 (resets at {reset:%H:%M} your time)")

    if args.resume is not None:
        job_id = pick_job(args.resume)
        if not job_id:
            return 1
        fetcher = CommentFetcher.resume(api_key, job_id, log=log, wait_for_quota=True if args.wait else None)
    else:
        try:
            topic = args.topic or input("Topic: ").strip()
            output_dir = (
                args.output_dir
                or input(f"Folder to save to (press Enter for {DEFAULT_OUTPUT_DIR}): ").strip()
                or str(DEFAULT_OUTPUT_DIR)
            )
        except (KeyboardInterrupt, EOFError):
            print()
            return 1

        options = FetchOptions(
            topic=topic,
            output_dir=output_dir,
            max_videos=args.videos,
            max_comments=args.comments,
            order=args.order,
            language=args.lang,
            region=args.region.upper(),
            published_after=args.after,
            published_before=args.before,
            include_replies=args.replies,
            title_must_include=args.title_has,
            comment_must_include=args.comment_has,
            wait_for_quota=args.wait,
        )
        try:
            options.validate()
        except ValueError as e:
            print(f"✗ {e}")
            return 1
        fetcher = CommentFetcher(api_key, options, log=log)

    result = fetcher.run()
    if result.excel_path:
        print(f"Excel: {result.excel_path}")
    if result.output_path:
        print(f"CSV:   {result.output_path}")
    if result.resumable:
        print("Continue later with: python cli.py --resume")
    return 0 if result.status in ("done", "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
