#!/usr/bin/env python3
"""Browser-based X.com crawler → feeds into the curation pipeline.

Uses opencli browser to search X.com, expand truncated tweets, extract
structured data via JavaScript, then runs the main crawl pipeline.

No API token needed — just opencli + Chrome logged into x.com.

Usage:
  python3 script/crawl_via_browser.py [window_hours] [per_query_limit] [--pipeline] [--enrich-threads]

Examples:
  # Just collect JSON, no pipeline
  python3 script/crawl_via_browser.py 24 20

  # Collect + run curation pipeline
  python3 script/crawl_via_browser.py 24 20 --pipeline

  # Collect + pipeline + thread enrichment (opens tweet pages)
  python3 script/crawl_via_browser.py 24 15 --pipeline --enrich-threads
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent

QUERIES = (
    "GPT-Image-2 prompt",
    '"gpt-image-2" prompt',
    "gpt image 2 prompt",
    "gpt-image-2 prompts",
    "gpt image 2 prompts",
    "GPT Image 2 prompt",
    "gptimage2 prompt",
    "GPT Image 2 Prompts",
    "GPT-Image-2",
    "gpt image 2",
)

# Keywords that indicate a tweet is actually about GPT-Image-2 (not noise)
RELEVANCE_KEYWORDS = (
    "gpt-image-2", "gpt image 2", "gptimage2", "gpt-image2",
    "chatgpt image", "gpt 4o image", "gpt-4o image",
    "image 2 prompt", "image2 prompt",
)

# JS to click "Show more" buttons on all truncated tweets
JS_EXPAND = """
(() => {
  let clicked = 0;
  document.querySelectorAll('[role="button"]').forEach(btn => {
    const text = btn.innerText.trim();
    if (text === 'Show more' || text === '显示更多' || text === 'もっと見る') {
      btn.click();
      clicked++;
    }
  });
  return clicked;
})()
"""

# JS to extract tweet data after expansion
JS_EXTRACT = r"""
JSON.stringify(
  Array.from(document.querySelectorAll('[data-testid="tweet"]'))
    .filter(t => !t.closest('[data-testid="tweet"] [data-testid="tweet"]'))
    .filter(t => !t.querySelector('[data-testid="socialContext"]')?.innerText?.includes('Reposted'))
    .map(t => {
      // Tweet ID from the status link
      const link = t.querySelector('a[href*="/status/"]');
      const href = link?.getAttribute('href') || '';
      const id = href.split('/status/')[1]?.split(/[?#]/)[0] || '';
      if (!id) return null;

      // Author
      const authorEl = t.querySelector('[data-testid="User-Name"] a');
      const author = (authorEl?.getAttribute('href') || '').replace('/', '');

      // Text — try multiple selectors, pick longest
      const candidates = [];
      const textEl = t.querySelector('[data-testid="tweetText"]');
      if (textEl) candidates.push(textEl.innerText);
      const langDiv = t.querySelector('div[lang]');
      if (langDiv) candidates.push(langDiv.innerText);
      const text = candidates.sort((a,b) => b.length - a.length)[0] || '';

      // Created at
      const timeEl = t.querySelector('time');
      const created_at = timeEl?.getAttribute('datetime') || '';

      // Engagement metrics from aria-labels
      const getMetric = (testid) => {
        const el = t.querySelector(`[data-testid="${testid}"]`);
        const label = el?.getAttribute('aria-label') || '';
        const m = label.match(/[\d,]+/);
        return m ? parseInt(m[0].replace(/,/g, '')) : 0;
      };
      const replies = getMetric('reply');
      const retweets = getMetric('retweet');
      const likes = getMetric('like');
      const views = getMetric('views');

      // Media images (not profile pics)
      const images = Array.from(
        t.querySelectorAll('img[src*="pbs.twimg.com/media"]')
      ).map(i => {
        let src = i.src;
        // Get original size if possible
        if (src.includes('&name=')) {
          src = src.replace(/&name=\w+/, '&name=orig');
        } else if (src.includes('?format=')) {
          src = src.replace(/\?format=\w+&name=\w+/, '?format=jpg&name=orig');
        }
        return src;
      });

      return {
        id, author, text, created_at,
        url: 'https://x.com/' + author + '/status/' + id,
        likes, views, replies, retweets,
        media_urls: [...new Set(images)],
      };
    })
    .filter(t => t && t.id)
)
"""


def opencli_value(raw: str):
    """Parse the first JSON value, ignoring trailing update notices."""
    text = raw.lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"opencli returned invalid JSON: {exc}") from exc
    return value


def opencli_json(raw: str) -> list[dict]:
    value = opencli_value(raw)
    if not isinstance(value, list):
        raise ValueError("opencli returned a non-list value")
    return value


def browser_eval(tab_id: str, js: str, timeout: int = 30) -> Optional[str]:
    """Run JavaScript in one OpenCLI tab."""
    result = subprocess.run(
        ["opencli", "browser", "eval", js, "--tab", tab_id],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        print(f"    browser eval error: {result.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return result.stdout


def browser_open(url: str, timeout: int = 30) -> Optional[str]:
    """Open a URL and return the tab ID reported by OpenCLI."""
    result = subprocess.run(
        ["opencli", "browser", "open", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        print(f"    browser open error: {result.stderr.strip()[:200]}", file=sys.stderr)
        return None
    try:
        payload = opencli_value(result.stdout)
    except ValueError as exc:
        print(f"    browser open error: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict) or not payload.get("page"):
        print("    browser open error: response did not include a tab ID", file=sys.stderr)
        return None
    return str(payload["page"])


def browser_scroll(tab_id: str) -> bool:
    """Scroll down in one OpenCLI tab."""
    result = subprocess.run(
        ["opencli", "browser", "scroll", "down", "--tab", tab_id],
        capture_output=True, text=True, timeout=15,
    )
    return result.returncode == 0


def browser_close(tab_id: Optional[str]):
    """Close one OpenCLI tab without closing unrelated browser work."""
    if not tab_id:
        return
    subprocess.run(
        ["opencli", "browser", "tab", "close", "--tab", tab_id],
        capture_output=True, timeout=10,
    )


def is_relevant(tweet: dict) -> bool:
    """Check if a tweet is actually about GPT-Image-2 prompts."""
    text = (tweet.get("text", "") + " " + tweet.get("author", "")).lower()
    return any(kw in text for kw in RELEVANCE_KEYWORDS)


def has_real_media(tweet: dict) -> bool:
    """Check if tweet has actual media images (not just link previews)."""
    urls = tweet.get("media_urls", [])
    if not urls:
        return False
    # Must have at least one image from pbs.twimg.com/media (not profile_images)
    return any("pbs.twimg.com/media" in u for u in urls)


def twitter_date_query(query: str, start: datetime, end: datetime) -> str:
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    until_date = end_utc.date()
    end_midnight = datetime.combine(
        until_date, datetime.min.time(), tzinfo=timezone.utc
    )
    if end_utc > end_midnight:
        until_date += timedelta(days=1)
    return (
        f"({query}) since:{start_utc.date().isoformat()} "
        f"until:{until_date.isoformat()} -filter:retweets"
    )


def tweet_in_window(tweet: dict, start: datetime, end: datetime) -> bool:
    created_at = str(tweet.get("created_at", "")).strip()
    if not created_at:
        return False
    if created_at.endswith("Z"):
        created_at = created_at[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return start.astimezone(timezone.utc) <= parsed <= end.astimezone(timezone.utc)


def search_query(
    query: str, start: datetime, end: datetime, limit: int = 20
) -> list[dict]:
    """Search X.com and extract tweets via browser."""
    url = f"https://x.com/search?q={quote(twitter_date_query(query, start, end))}&f=live"
    print(f"    Opening: {query[:55]}...")
    tab_id = browser_open(url)
    if not tab_id:
        return []
    try:
        time.sleep(3)
        all_tweets: list[dict] = []
        seen_ids: set[str] = set()
        scrolls = 0
        max_scrolls = max(3, (limit // 5) + 2)

        while len(all_tweets) < limit and scrolls < max_scrolls:
            expanded = browser_eval(tab_id, JS_EXPAND)
            if expanded:
                try:
                    n = int(opencli_value(expanded))
                    if n > 0:
                        time.sleep(0.5)
                except (TypeError, ValueError):
                    pass

            raw = browser_eval(tab_id, JS_EXTRACT)
            if raw:
                try:
                    tweets = opencli_json(raw)
                    new_count = 0
                    for tweet in tweets:
                        tid = tweet["id"]
                        if tid and tid not in seen_ids:
                            seen_ids.add(tid)
                            all_tweets.append(tweet)
                            new_count += 1
                    if new_count:
                        print(f"      +{new_count} tweets (total {len(all_tweets)})")
                except (KeyError, ValueError) as exc:
                    print(f"      JSON error: {exc}", file=sys.stderr)

            if len(all_tweets) >= limit:
                break

            browser_scroll(tab_id)
            time.sleep(2)
            scrolls += 1

        return [
            tweet for tweet in all_tweets
            if tweet_in_window(tweet, start, end)
        ][:limit]
    finally:
        browser_close(tab_id)


def enrich_thread(tweet: dict) -> list[dict]:
    """Fetch thread replies for a tweet via browser."""
    url = tweet["url"]
    tab_id = browser_open(url)
    if not tab_id:
        return []
    try:
        time.sleep(2)
        browser_eval(tab_id, JS_EXPAND)
        time.sleep(0.5)
        raw = browser_eval(tab_id, JS_EXTRACT)
        if not raw:
            return []

        try:
            all_tweets = opencli_json(raw)
        except ValueError:
            return []

        author = tweet["author"].lower()
        return [
            item for item in all_tweets
            if item["author"].lower() == author and item["id"] != tweet["id"]
        ]
    finally:
        browser_close(tab_id)


def run_pipeline(input_json: Path, enrich: bool = False):
    """Run the main curation pipeline on extracted JSON."""
    cmd = [
        sys.executable, str(ROOT / "script" / "crawl_x_prompts.py"),
        "--provider", "json",
        "--input-json", str(input_json),
        "--download-media", "prefiltered",
        "--output-dir", str(input_json.parent / f"pipeline_{input_json.stem}"),
    ]
    if enrich:
        cmd.append("--enrich-threads")

    print(f"\n  Running pipeline: {' '.join(cmd[-6:])}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"  Pipeline error: {result.stderr.strip()}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    do_pipeline = "--pipeline" in args
    do_enrich = "--enrich-threads" in args
    args = [a for a in args if not a.startswith("--")]

    window_hours = int(args[0]) if len(args) > 0 else 24
    per_query = int(args[1]) if len(args) > 1 else 20
    output_file = args[2] if len(args) > 2 else None

    if output_file is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_file = str(ROOT / "tmp" / f"browser_crawl_{ts}.json")

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    all_tweets: list[dict] = []
    seen_ids: set[str] = set()
    total_raw = 0
    total_relevant = 0
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=window_hours)

    print(f"Browser crawl: {len(QUERIES)} queries × {per_query} limit, {window_hours}h window")
    print(f"Pipeline: {'yes' if do_pipeline else 'no'}, Threads: {'yes' if do_enrich else 'no'}")
    print()

    for i, query in enumerate(QUERIES):
        print(f"[{i+1}/{len(QUERIES)}] {query}")
        try:
            tweets = search_query(query, start, end, per_query)
            total_raw += len(tweets)

            # Filter: must be relevant AND have real media
            relevant = [t for t in tweets if is_relevant(t) and has_real_media(t)]
            total_relevant += len(relevant)

            for t in relevant:
                if t["id"] not in seen_ids:
                    seen_ids.add(t["id"])
                    all_tweets.append(t)

            print(f"    → {len(tweets)} raw, {len(relevant)} relevant ({len(all_tweets)} total unique)")

            # Thread enrichment
            if do_enrich and relevant:
                for t in relevant[:3]:  # Only top 3 per query
                    if len(t["text"]) < 100:
                        replies = enrich_thread(t)
                        if replies:
                            t["thread_replies"] = replies
                            print(f"      thread: {len(replies)} replies from @{t['author']}")

        except Exception as e:
            print(f"    ✖ Error: {e}", file=sys.stderr)
        time.sleep(1.5)

    # Save
    with open(output_path, "w") as f:
        json.dump(all_tweets, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"✓ Done: {total_raw} raw → {total_relevant} relevant → {len(all_tweets)} unique")
    print(f"  Saved: {output_path}")

    if do_pipeline:
        run_pipeline(output_path, enrich=do_enrich)

    return output_path


if __name__ == "__main__":
    main()
