import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "script" / "crawl_x_prompts.py"
SPEC = importlib.util.spec_from_file_location("crawl_x_prompts", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeProvider:
    def __init__(self, tweets):
        self.tweets = tweets

    def search(self, query, start, end, limit):
        del query, start, end
        return self.tweets[:limit]


class QueryIgnoringThreadProvider:
    def __init__(self, root, thread):
        self.root = root
        self.thread = thread

    def search(self, query, start, end, limit):
        del start, end
        if "conversation_id:" in query:
            return self.thread[:limit]
        return [self.root]


class CrawlerTests(unittest.TestCase):
    def test_normalizes_x_api_payload(self):
        payload = json.loads(
            (ROOT / "tests" / "fixtures" / "x_api_search.json").read_text()
        )
        tweets = MODULE.normalize_x_api_payload(payload)
        self.assertEqual(2, len(tweets))
        self.assertEqual("fixture_artist", tweets[0].author_handle)
        self.assertEqual(25, tweets[0].likes)
        self.assertEqual(["https://pbs.twimg.com/media/FIXTURE.jpg"], tweets[0].media_urls)

    def test_extracts_prompt_and_context(self):
        tweet = MODULE.Tweet(
            tweet_id="1",
            author_handle="artist",
            text="A quick demo\n\nPrompt:\nCreate a cinematic product photograph with soft side lighting, realistic materials, a clean studio background, and precise packaging typography.",
        )
        prompt, context = MODULE.extract_prompt(tweet)
        self.assertTrue(prompt.startswith("Create a cinematic product"))
        self.assertEqual("A quick demo", context)

    def test_extracts_prompt_after_marker_on_its_own_line(self):
        tweet = MODULE.Tweet(
            tweet_id="4",
            author_handle="artist",
            text="Demo\nPrompt\nCreate a detailed poster with realistic lighting, balanced typography, rich material texture, clean composition, and a controlled editorial color palette.",
        )
        prompt, context = MODULE.extract_prompt(tweet)
        self.assertTrue(prompt.startswith("Create a detailed poster"))
        self.assertEqual("Demo", context)

    def test_collects_prompt_from_author_reply(self):
        root = MODULE.Tweet(
            tweet_id="1",
            author_handle="artist",
            text="Prompt below",
            created_at="2026-07-15T00:00:00Z",
        )
        reply = MODULE.Tweet(
            tweet_id="2",
            author_handle="artist",
            text="Prompt: Create a detailed editorial storyboard with consistent identity, six cinematic panels, natural lighting, readable labels, realistic textures, and a clear visual narrative from opening frame to final hero shot.",
            created_at="2026-07-15T00:01:00Z",
        )
        prompt, _ = MODULE.extract_prompt(root, [reply])
        self.assertIn("six cinematic panels", prompt)

    def test_recovers_prompt_like_media_alt_text(self):
        tweet = MODULE.Tweet(
            tweet_id="3",
            author_handle="artist",
            text="Prompt is in the image description.",
            media_alt_texts=[
                "Create a cinematic portrait with realistic skin texture, controlled side lighting, an 85mm lens, subtle film grain, a clean background, balanced composition, and precise natural color grading."
            ],
        )
        prompt, _ = MODULE.extract_prompt(tweet)
        self.assertIn("85mm lens", prompt)

    def test_pipeline_deduplicates_existing_tweets(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "data").mkdir()
            (repo / "data" / "ingested_tweets.json").write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "tweet_url": "https://x.com/old/status/100"
                            }
                        ]
                    }
                )
            )
            (repo / "queries.txt").write_text("GPT-Image-2 prompt\n")
            output = repo / "tmp" / "run"
            tweets = [
                MODULE.Tweet(
                    tweet_id="100",
                    author_handle="old",
                    text="Prompt: old prompt that is already ingested",
                    created_at="2026-07-15T01:00:00Z",
                    media_urls=["https://example.com/old.jpg"],
                ),
                MODULE.Tweet(
                    tweet_id="200",
                    author_handle="new",
                    text="Prompt: Create an ultra-detailed product photography scene with controlled studio lighting, realistic reflections, clean packaging, a premium background, balanced composition, and high-fidelity commercial textures.",
                    created_at="2026-07-15T01:30:00Z",
                    likes=20,
                    media_urls=["https://example.com/new.jpg"],
                ),
            ]
            args = argparse.Namespace(
                repo=repo,
                provider="json",
                reviewer="heuristic",
                end="2026-07-15T02:00:00Z",
                window_hours=24,
                queries_file=repo / "queries.txt",
                per_query_limit=100,
                thread_limit=30,
                enrich_threads=False,
                min_prompt_chars=100,
                curation_base_url="",
                curation_api_key="",
                curation_model="",
                download_media="none",
                output_dir=output,
                summary_limit=30,
                publish_report=False,
            )
            result = MODULE.run_pipeline(args, FakeProvider(tweets))
            self.assertEqual(2, result["report"]["stats"]["raw_collected"])
            self.assertEqual(1, result["report"]["stats"]["after_dedup"])
            candidates = json.loads((output / "candidate_tweets.json").read_text())
            self.assertEqual("200", candidates[0]["tweet_id"])
            self.assertEqual("review", candidates[0]["suggested_action"])

    def test_accepts_retained_curation_report_as_replay_input(self):
        payload = {
            "candidates": [
                {
                    "tweet_url": "https://x.com/example/status/300",
                    "author_handle": "example",
                    "prompt_text": "Create a reusable editorial image prompt with precise composition, realistic lighting, controlled typography, detailed materials, a clean background, and a consistent visual hierarchy.",
                    "media_urls": ["https://pbs.twimg.com/media/example.jpg"],
                }
            ]
        }
        tweets = MODULE.normalize_payload(payload)
        self.assertEqual("300", tweets[0].tweet_id)
        self.assertTrue(tweets[0].explicit_prompt.startswith("Create a reusable"))

    def test_json_provider_filters_thread_before_applying_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            replay = Path(temp) / "replay.json"
            replay.write_text(
                json.dumps(
                    [
                        {
                            "id": "unrelated",
                            "author_handle": "artist",
                            "conversation_id": "other",
                            "text": "unrelated",
                        },
                        {
                            "id": "reply",
                            "author_handle": "artist",
                            "conversation_id": "thread-1",
                            "text": "matching reply",
                        },
                        {
                            "id": "other-author",
                            "author_handle": "someone_else",
                            "conversation_id": "thread-1",
                            "text": "wrong author",
                        },
                    ]
                )
            )
            provider = MODULE.JsonProvider(replay)
            results = provider.search(
                "conversation_id:thread-1 from:artist",
                datetime(2026, 7, 15, tzinfo=timezone.utc),
                datetime(2026, 7, 16, tzinfo=timezone.utc),
                1,
            )
            self.assertEqual(["reply"], [item.tweet_id for item in results])

    def test_pipeline_does_not_merge_media_from_unrelated_thread_results(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "data").mkdir()
            (repo / "queries.txt").write_text("GPT-Image-2 prompt\n")
            root = MODULE.Tweet(
                tweet_id="root",
                author_handle="artist",
                conversation_id="thread-1",
                text="Prompt below in this thread",
                created_at="2026-07-15T01:00:00Z",
            )
            reply = MODULE.Tweet(
                tweet_id="reply",
                author_handle="artist",
                conversation_id="thread-1",
                text="Prompt: Create a detailed editorial image with realistic lighting, controlled typography, precise composition, rich material texture, a clean background, and consistent visual hierarchy.",
                created_at="2026-07-15T01:01:00Z",
                media_urls=["https://example.com/correct.jpg"],
            )
            unrelated = MODULE.Tweet(
                tweet_id="unrelated",
                author_handle="other",
                conversation_id="other-thread",
                text="unrelated",
                created_at="2026-07-15T01:02:00Z",
                media_urls=["https://example.com/unrelated.jpg"],
            )
            args = argparse.Namespace(
                repo=repo,
                provider="json",
                reviewer="heuristic",
                end="2026-07-15T02:00:00Z",
                window_hours=24,
                queries_file=repo / "queries.txt",
                per_query_limit=100,
                thread_limit=30,
                enrich_threads=True,
                min_prompt_chars=100,
                curation_base_url="",
                curation_api_key="",
                curation_model="",
                download_media="none",
                output_dir=repo / "tmp" / "run",
                summary_limit=30,
                publish_report=False,
            )
            MODULE.run_pipeline(
                args, QueryIgnoringThreadProvider(root, [unrelated, reply])
            )
            candidates = json.loads(
                (args.output_dir / "candidate_tweets.json").read_text()
            )
            self.assertEqual(["https://example.com/correct.jpg"], candidates[0]["media_urls"])
            self.assertEqual(["reply"], candidates[0]["thread_tweet_ids"])


if __name__ == "__main__":
    unittest.main()
