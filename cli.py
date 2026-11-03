"""Command-line version.

Usage: python cli.py TOPIC OUTPUT_FOLDER [options]
Run it without arguments to be asked for the topic and folder; other options use their defaults.
Press Ctrl+C to stop at any time; whatever was fetched so far is kept.
"""

from __future__ import annotations

import argparse
import sys

from fetcher import (
    DEFAULT_OUTPUT_DIR,
    MAX_COMMENTS_LIMIT,
    MAX_VIDEOS_LIMIT,
    ORDER_CHOICES,
    CommentFetcher,
    FetchOptions,
    load_api_key,
)

PREFIX = {"warn": "! ", "error": "✗ ", "success": "✓ "}

KEYWORD_HELP = (
    "separate keywords with spaces or commas; all must appear, in any order. "
    'Quote phrases with spaces, e.g. \'"battery life" worse\'. English words match from the start of a word'
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Search YouTube for a topic and save the comments as CSV and Excel")
    parser.add_argument("topic", nargs="?", help="what to search for")
    parser.add_argument("output_dir", nargs="?", help=f"folder to save to (default {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--videos", type=int, default=50, help=f"max videos, 1–{MAX_VIDEOS_LIMIT} (default 50)")
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
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print("✗ No API key yet: set YOUTUBE_API_KEY in .env")
        return 1

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
    )
    try:
        options.validate()
    except ValueError as e:
        print(f"✗ {e}")
        return 1

    fetcher = CommentFetcher(api_key, options, log=lambda level, message: print(PREFIX.get(level, "") + message))
    result = fetcher.run()
    if result.excel_path:
        print(f"Excel: {result.excel_path}")
    if result.output_path:
        print(f"CSV:   {result.output_path}")
    return 0 if result.status in ("done", "stopped") else 1


if __name__ == "__main__":
    sys.exit(main())
