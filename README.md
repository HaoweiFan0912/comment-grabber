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

Run it without arguments to be asked for the topic and folder. Use `--help` to see every option (sort order, keyword filters, language, region, date range, replies, waiting for the quota).

## More than 500 videos

One YouTube search only reaches about 500 results, so when you ask for more (up to 10,000) the app works in batches:

1. The first batch searches your whole date range with your chosen sort order.
2. Later batches search stretches of that date range, newest first, with the same sort order. If there still aren't enough videos, stretches that had more results than YouTube would show are split into smaller ones.
3. Each batch's comments are collected before the next search starts, so you get data early and the files grow as the job runs.

Every video the search has returned is recorded in the job's progress, so no video is collected twice within a job, even across restarts. Separate jobs don't know about each other, so two jobs can still pick up the same video; `Comment ID` tells duplicates apart if you merge their files.

What to expect:

- The videos are spread across the date range, so this isn't the top N for the topic: YouTube can't rank that far down.
- Later searches return some videos already seen, and those search pages still cost quota. Budget roughly 1.5 times the search cost, and more with a title filter.
- If YouTube runs out of videos for the topic, the job ends with what it found and tells you how many that was.
- A very large job may pass the Excel sheet limit of 1,048,575 rows. The Excel file then stops there and the CSV keeps everything.

## Big jobs: continuing over several days

Progress is saved after every page of results, so a job that stops for any reason (you press Stop, the daily quota runs out, the network drops, you close the app) can be continued later:

- **Web UI:** stopped jobs appear under **Unfinished jobs**. Press **Continue** to pick up where it left off. It doesn't search again (the video list is saved) and never saves the same comment twice; new comments are added to the same CSV and the Excel file is rebuilt.
- **Command line:** `.venv/bin/python cli.py --resume` lists unfinished jobs and continues one.
- **Wait for the quota automatically:** tick *If the daily quota runs out, wait for it to reset and keep going* (or pass `--wait`). The app then waits until the quota resets (midnight Pacific Time) and carries on by itself. Keep the app open and the computer awake.

Saved progress lives in the `state/` folder. **Discard** forgets an unfinished job but keeps the comment files it already saved.

## Keyword filters

"Titles must include" and "Comments must include" are optional:

- Separate keywords with spaces or commas. **Every** keyword must appear, in any order, anywhere in the text.
- Put phrases with spaces in quotes: `"battery life" worse` means the phrase *battery life* and the word *worse*.
- English (and other languages written with spaces) matches from the start of a word: `tent` matches *tents* and *Tent*, but not *content*. A side effect: `yes` also matches *yesterday*.
- Chinese, Japanese, Thai and other languages written without spaces match anywhere: `奶` matches `牛奶`.
- Matching is literal: no translation or synonyms (`是` doesn't match *Yes*), Simplified and Traditional Chinese are different, and *café* doesn't match *cafe*.
- Not case-sensitive. Full-width and half-width characters, curly and straight quotes (*don’t* / *don't*) count as the same.

With a title filter, videos whose titles don't match don't count toward "Max videos"; the app keeps paging through search results until it has enough. For 500 videos or fewer that means one search (up to about 500 results); bigger jobs keep going through later batches.

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

The defaults (50 videos × 500 comments) use at most about 350 units. A title filter can add up to 900 more for extra search pages. Searching is the expensive part: after searching 500 videos (1,000 units) there's enough left to check about 900,000 comments the same day.

The header shows how much quota this app has used today. It can't see use of the same key by other apps.

If you regularly need more, apply for a higher quota with Google's *YouTube API Services Audit and Quota Extension Form*. Don't rotate several API keys or Google Cloud projects to get around the limit: YouTube's API policies forbid it, and keys can be suspended.

With 10,000 units a day, searching alone tops out at about 5,000 videos a day and leaves nothing for comments, so jobs of several thousand videos take more than a day. Jobs like that can continue across days: see [Big jobs](#big-jobs-continuing-over-several-days).
