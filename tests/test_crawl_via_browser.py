import importlib.util
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "script" / "crawl_via_browser.py"
SPEC = importlib.util.spec_from_file_location("crawl_via_browser", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BrowserCrawlerTests(unittest.TestCase):
    def test_browser_helpers_use_opencli_tab_contract(self):
        opened = subprocess.CompletedProcess(
            [], 0,
            stdout='{"url":"https://x.com/search","page":"tab-123"}\nUpdate available',
            stderr="",
        )
        succeeded = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with patch.object(
            MODULE.subprocess, "run", side_effect=[opened, succeeded, succeeded, succeeded]
        ) as run:
            tab_id = MODULE.browser_open("https://x.com/search")
            self.assertEqual("tab-123", tab_id)
            MODULE.browser_eval(tab_id, "JSON.stringify([])")
            MODULE.browser_scroll(tab_id)
            MODULE.browser_close(tab_id)

        self.assertEqual(
            ["opencli", "browser", "open", "https://x.com/search"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            [
                "opencli", "browser", "eval", "JSON.stringify([])",
                "--tab", "tab-123",
            ],
            run.call_args_list[1].args[0],
        )
        self.assertEqual(
            ["opencli", "browser", "scroll", "down", "--tab", "tab-123"],
            run.call_args_list[2].args[0],
        )
        self.assertEqual(
            ["opencli", "browser", "tab", "close", "--tab", "tab-123"],
            run.call_args_list[3].args[0],
        )

    def test_search_query_applies_date_operators_and_exact_window(self):
        start = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        end = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
        tweets = [
            {"id": "before", "created_at": "2026-07-15T11:59:59Z"},
            {"id": "inside", "created_at": "2026-07-15T12:00:01Z"},
            {"id": "after", "created_at": "2026-07-16T01:00:01Z"},
        ]
        with (
            patch.object(MODULE, "browser_open", return_value="tab-1") as opened,
            patch.object(
                MODULE, "browser_eval", side_effect=["0", json.dumps(tweets)]
            ),
            patch.object(MODULE, "browser_close") as closed,
            patch.object(MODULE.time, "sleep"),
        ):
            results = MODULE.search_query("GPT-Image-2 prompt", start, end, 3)

        query_url = unquote(opened.call_args.args[0])
        self.assertIn("since:2026-07-15", query_url)
        self.assertIn("until:2026-07-17", query_url)
        self.assertIn("-filter:retweets", query_url)
        self.assertEqual(["inside"], [tweet["id"] for tweet in results])
        closed.assert_called_once_with("tab-1")


if __name__ == "__main__":
    unittest.main()
