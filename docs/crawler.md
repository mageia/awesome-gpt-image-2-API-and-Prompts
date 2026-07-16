# X Prompt Crawler

`script/crawl_x_prompts.py` reconstructs the repository's lost daily prompt
collection pipeline from the retained reports and ingestion records.

## Reconstructed workflow

1. Search 10-13 GPT-Image-2 queries over a 24-hour UTC window.
2. Merge duplicate query results by tweet ID.
3. Drop tweets already present in `data/ingested_tweets.json` or repository Markdown.
4. Recover prompt text from the post and, optionally, self-authored thread replies.
5. Require a reusable inline prompt and at least one output image.
6. Suggest a title, category, layout shape, and keep/review/drop decision.
7. Download media into a temporary review directory.
8. Emit candidate, prefilter, review, report, and summary JSON artifacts.

The crawler does not edit README or category files. Approved candidates should be
integrated in a separate reviewed step.

## X API usage

Use an X API v2 bearer token with recent-search access:

```bash
export X_BEARER_TOKEN="..."
python3 script/crawl_x_prompts.py \
  --provider x-api \
  --end 2026-07-15T02:00:00Z \
  --window-hours 24 \
  --enrich-threads
```

JSON 回放启用 `--enrich-threads` 时，线程结果会按 `conversation_id` 和作者过滤后再应用 `--thread-limit`；媒体和 alt-text 合并前也会再次校验这两个边界。

Without `--end`, the window ends at the current UTC time. Output is written to a
timestamped directory under `tmp/`.

## Existing local X CLI

Any command that prints a JSON array/object can be used. The template supports
`{query}`, `{start}`, `{end}`, and `{limit}` placeholders:

```bash
export X_SEARCH_COMMAND='bird search --json -n {limit} {query}'
python3 script/crawl_x_prompts.py --provider command
```

The normalizer accepts X API v2 responses and common crawler fields such as
`id`, `tweet_id`, `text`, `full_text`, `author`, `media`, `images`, `likes`, and
`views`.

## Offline replay

The committed fixture exercises the complete extraction/report path without
network access:

```bash
python3 script/crawl_x_prompts.py \
  --provider json \
  --input-json tests/fixtures/x_api_search.json \
  --queries-file tests/fixtures/queries.txt \
  --end 2026-07-15T02:00:00Z \
  --download-media none \
  --output-dir tmp/crawler-fixture
```

## Optional semantic reviewer

Heuristics recover the mechanical pipeline but cannot reliably judge novelty
against hundreds of existing prompts. An OpenAI-compatible chat-completions
endpoint can perform the semantic keep/drop pass:

```bash
export CURATION_BASE_URL="https://your-provider.example/v1"
export CURATION_API_KEY="..."
export CURATION_MODEL="your-model"
python3 script/crawl_x_prompts.py --provider x-api --reviewer openai
```

The model must return JSON with `action`, `title`, `category`, and `reason`.

## Artifacts

Each run writes:

- `candidate_tweets.json`: all new deduplicated candidates.
- `prefiltered_candidates.json`: candidates with prompt text and media.
- `review_queue.json`: keep/review candidates.
- `search_meta.json`: queries, IDs returned per query, and time window.
- `curation_report.json`: full report compatible with retained curation data.
- `summary.json`: compact cron/job summary.
- `media/<tweet_id>/`: downloaded result images.

Use `--publish-report` only after review to additionally write dated files under
`data/` and `result/`.
