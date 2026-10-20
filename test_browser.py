"""Opt-in Chromium integration test; all HTTP requests stay on localhost."""

import asyncio
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import scraper


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 to run Chromium fixtures")
class BrowserWorkflowTests(unittest.TestCase):
    def test_skipped_challenge_continues_to_next_record_even_at_failure_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "results.csv"
            records = [{"county": "Franklin", "parcel_id": "001"},
                       {"county": "Franklin", "parcel_id": "002"}]
            options = SimpleNamespace(workers=1, interval=0, timeout=5, headed=False,
                                      max_failures=1, output=target)
            results = []
            for record, status in zip(records, ["SKIPPED: MANUAL REVIEW TIMED OUT", "OK"]):
                result = dict.fromkeys(scraper.FIELDS, "")
                result.update(record, status=status)
                if status == "OK":
                    result["owner_name"] = "Example Owner"
                results.append(result)
            with target.open("w", newline="", encoding="utf-8") as output:
                csv.DictWriter(output, fieldnames=scraper.FIELDS).writeheader()
                with patch.object(scraper, "lookup", new=AsyncMock(side_effect=results)) as lookup:
                    self.assertEqual(asyncio.run(scraper.run_scraper(records, options, output)), 1)
                    self.assertEqual(lookup.await_count, 2)
            with target.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual([row["status"] for row in rows],
                             ["SKIPPED: MANUAL REVIEW TIMED OUT", "OK"])
            self.assertEqual(scraper.load_processed_records(target), {("Franklin", "002")})

    def test_parallel_workers_reuse_agreement_and_wait_for_delayed_owner_data(self):
        visits = {"agreement": 0, "owner": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if "agreed=yes" not in self.headers.get("Cookie", ""):
                    visits["agreement"] += 1
                    html = """<button onclick="document.cookie='agreed=yes; path=/'; location.reload()">I Agree</button>"""
                else:
                    visits["owner"] += 1
                    parcel = parse_qs(urlparse(self.path).query)["parcel"][0]
                    html = f"""<div>Owner</div><div id="data"></div><script>
                    setTimeout(() => document.querySelector('#data').innerHTML =
                    'Primary Owner<br>Owner {parcel}<br>123 Main St<br>Frankfort KY 40601', 300);
                    </script>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "results.csv"
                options = SimpleNamespace(workers=2, interval=.01, timeout=5, owner_timeout=3,
                                          retries=0, headed=False, max_failures=3, output=target,
                                          session_dir=Path(directory) / "sessions")
                records = [{"county": "Franklin", "parcel_id": f"00{i}"} for i in range(6)]
                with target.open("w", newline="", encoding="utf-8") as output:
                    csv.DictWriter(output, fieldnames=scraper.FIELDS).writeheader()
                    with patch.object(scraper, "build_url", side_effect=lambda county, parcel:
                                      f"http://127.0.0.1:{server.server_port}/?parcel={parcel}"):
                        self.assertEqual(asyncio.run(scraper.run_scraper(records, options, output)), 0)
                with target.open(newline="", encoding="utf-8") as source:
                    rows = list(csv.DictReader(source))
                self.assertEqual(len(rows), 6)
                self.assertEqual({row["parcel_id"] for row in rows}, {r["parcel_id"] for r in records})
                self.assertTrue(all(row["owner_name"] == f"Owner {row['parcel_id']}" for row in rows))
                self.assertEqual(len(scraper.load_processed_records(target)), 6)
                self.assertEqual(visits, {"agreement": 2, "owner": 6})
                self.assertEqual(len(list(options.session_dir.glob("*.json"))), 2)
                # A new browser restores both worker sessions without another agreement.
                with target.open("w", newline="", encoding="utf-8") as output:
                    with patch.object(scraper, "build_url", side_effect=lambda county, parcel:
                                      f"http://127.0.0.1:{server.server_port}/?parcel={parcel}"):
                        self.assertEqual(asyncio.run(scraper.run_scraper(records, options, output)), 0)
                self.assertEqual(visits, {"agreement": 2, "owner": 12})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
