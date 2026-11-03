# Comment Grabber

Search YouTube for a topic and save the comments from the videos it finds as Excel and CSV files.

## Setup

1. In the [Google Cloud console](https://console.cloud.google.com/apis/credentials), enable **YouTube Data API v3** and create an API key.
2. Open `.env` and set `YOUTUBE_API_KEY=your key`.
3. If you need a proxy to reach YouTube, the system proxy is used automatically; you can also add a line like `PROXY=http://127.0.0.1:7890` to `.env`.

## Usage

### Web UI (recommended)

Double-click `Start.command` in Finder. The first run installs the dependencies, then the app opens in your browser. Close the Terminal window that appears to quit.

Or run it from a terminal:

```bash
.venv/bin/python app.py
```

### Command line

```bash
.venv/bin/python cli.py "camping gear" ~/Desktop/comments --videos 50 --comments 500 --title-has "tent review"
```

Run it without arguments to be asked for the topic and folder. Use `--help` to see every option (sort order, keyword filters, language, region, date range, replies).

## Keyword filters

"Titles must include" and "Comments must include" are optional:

- Separate keywords with spaces or commas. **Every** keyword must appear, in any order, anywhere in the text.
- Put phrases with spaces in quotes: `"battery life" worse` means the phrase *battery life* and the word *worse*.
- English (and other languages written with spaces) matches from the start of a word: `tent` matches *tents* and *Tent*, but not *content*. A side effect: `yes` also matches *yesterday*.
- Chinese, Japanese, Thai and other languages written without spaces match anywhere: `奶` matches `牛奶`.
- Matching is literal: no translation or synonyms (`是` doesn't match *Yes*), Simplified and Traditional Chinese are different, and *café* doesn't match *cafe*.
- Not case-sensitive. Full-width and half-width characters, curly and straight quotes (*don’t* / *don't*) count as the same.

With a title filter, videos whose titles don't match don't count toward "Max videos"; the app keeps paging through search results (up to about 500) until it has enough.

With a comment filter, "Comments per video" becomes how many comments to **check** per video; only the matching ones are saved. When replies are included, each reply is checked on its own.

## Output

Each run creates two files named after the topic, date and time, so earlier files are never overwritten:

- `.xlsx` — open this one in Excel. (Excel misreads CSV cells that start with `@`, `-`, `+` or `=` as formulas, and YouTube author names all start with `@`.)
- `.csv` — written as comments arrive, so nothing is lost if a run fails or is stopped. Good for importing into other tools.

Columns: Type (comment/reply), Video Title, Channel, Author, Comment, Likes, Replies, Published, Video URL, Video ID, Comment ID, Parent Comment ID.

## Quota

The YouTube API allows 10,000 quota units per day, reset at midnight Pacific Time.

- Search: 100 units per page of 50 results
- Comments: 1 unit per 100 comments
- Replies: 1 extra unit per 100 replies, for comments with more than 5 replies

The defaults (50 videos × 500 comments) use at most about 350 units. A title filter can add up to 900 more for extra search pages. If the quota runs out midway, everything fetched so far is kept.
