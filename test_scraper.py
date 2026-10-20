import asyncio
import csv
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import scraper


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "records.csv"

    def test_bom_headers_county_case_duplicates_and_leading_zeros(self):
        self.path.write_text("\ufeff County , PARCEL_ID \nMCCRACKEN,001.02\nmccracken,001.02\nFranklin,0003\n", encoding="utf-8")
        self.assertEqual(scraper.load_records_from_csv(self.path), [
            {"county": "McCracken", "parcel_id": "001.02"},
            {"county": "Franklin", "parcel_id": "0003"},
        ])

    def test_invalid_inputs_fail_before_browser_launch(self):
        for text in ("county,parcel_id\nUnknown,123\n", "county,parcel_id\nFranklin,\n",
                     "county,parcel_id\nFranklin,123,extra\n", "county,county,parcel_id\n"):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(ValueError):
                    scraper.load_records_from_csv(self.path)

    def test_resume_skips_only_successful_records(self):
        with self.path.open("w", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=scraper.FIELDS)
            writer.writeheader()
            for parcel, owner, status in (("001", "Owner", "OK"), ("002", "", "OWNER NOT FOUND"),
                                           ("003", "", "ERROR: timeout"), ("004", "", "OK")):
                writer.writerow(dict(county="MCCRACKEN", parcel_id=parcel, owner_name=owner, status=status))
        self.assertEqual(scraper.load_processed_records(self.path), {("McCracken", "001")})

    def test_incompatible_or_partial_output_is_rejected(self):
        for text in ("county,parcel_id\n", ",".join(scraper.FIELDS) + "\nFranklin,1025,123\n"):
            self.path.write_text(text)
            with self.assertRaises(ValueError):
                scraper.load_processed_records(self.path)

    def test_dry_run_does_not_create_output(self):
        self.path.write_text("county,parcel_id\nFranklin,001\n")
        output = self.path.with_name("output.csv")
        self.assertEqual(scraper.main(["--input", str(self.path), "--output", str(output), "--dry-run"]), 0)
        self.assertFalse(output.exists())

    def test_default_manual_wait_is_thirty_seconds(self):
        self.path.write_text("county,parcel_id\nFranklin,001\n")
        with patch.object(scraper, "run_scraper", new=AsyncMock(return_value=0)) as run:
            self.assertEqual(scraper.main(["--input", str(self.path), "--output",
                                          str(self.path.with_name("output.csv")), "--manual-review"]), 0)
        self.assertEqual(run.call_args.args[1].manual_timeout, 30)

    def test_url_round_trips_special_characters(self):
        parcel = "001 & 02/#?+"
        self.assertEqual(parse_qs(urlparse(scraper.build_url("Franklin", parcel)).query)["KeyValue"], [parcel])

    def test_offline_lookup_and_resume_never_launch_browser(self):
        self.path.write_text("county,parcel_id\nBoone,001\nBoone,002\n")
        owners = self.path.with_name("owners.csv")
        owners.write_text("county,parcel_id,owner_name,owner_addr1,city_state_zip\n"
                          "BOONE,001,Example Owner,123 Main St,Example KY 41000\n")
        output = self.path.with_name("results.csv")
        args = ["--input", str(self.path), "--owner-data", str(owners), "--output", str(output)]
        with patch.object(scraper, "run_scraper", side_effect=AssertionError("Browser must not run")):
            self.assertEqual(scraper.main(args), 1)
            self.assertEqual(scraper.main(args), 1)
        with output.open(newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["status"] for r in rows], ["OK", "NOT FOUND IN OWNER DATA", "NOT FOUND IN OWNER DATA"])
        self.assertEqual(rows[0]["url"], owners.resolve().as_uri())
        self.assertEqual(rows[0]["app_id"], "")

    def test_offline_conflicting_owners_rejected(self):
        self.path.write_text("county,parcel_id,owner_name,owner_addr1,city_state_zip\n"
                             "Franklin,001,Owner A,Street A,City A\n"
                             "Franklin,001,Owner B,Street B,City B\n")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            scraper.load_owner_data(self.path, [{"county": "Franklin", "parcel_id": "001"}])

    def test_offline_incomplete_address_remains_retryable(self):
        import io
        output = io.StringIO()
        record = {"county": "Franklin", "parcel_id": "001"}
        owners = {("Franklin", "001"): {"owner_name": "Jane", "owner_addr1": "", "city_state_zip": ""}}
        self.assertEqual(scraper.run_offline([record], owners, self.path, output), 1)
        self.assertIn("INCOMPLETE OWNER DATA", output.getvalue())


class OwnerTests(unittest.TestCase):
    def test_original_and_primary_owner_layouts(self):
        for prefix in ("Owner", "Owner:\nPrimary Owner"):
            owner = scraper.parse_owner_text(f"Navigation\n{prefix}\nJane Doe\n123 Main St\nFrankfort KY 40601-1234\nSales")
            self.assertEqual(owner["owner_name"], "Jane Doe")
            self.assertEqual(owner["city_state_zip"], "Frankfort KY 40601-1234")

    def test_missing_truncated_or_navigation_blocks_are_not_success(self):
        for text in ("No results", "Owner\nJane", "Owner\nSales\nHistory\nFrankfort KY 40601",
                     "Owner\nJane\n123 Main St\nSales History"):
            self.assertFalse(scraper.parse_owner_text(text)["owner_name"])

    def test_foreign_mailing_address_is_preserved(self):
        self.assertEqual(scraper.parse_owner_text("Owner\nJane Doe\n123 Example Road\nToronto ON M5V 3L9")["owner_name"], "Jane Doe")


class LookupTests(unittest.IsolatedAsyncioTestCase):
    def options(self):
        return SimpleNamespace(retries=1, owner_timeout=1)

    def page(self, responses, text):
        page = SimpleNamespace(goto=AsyncMock(side_effect=responses))
        page.get_by_text = lambda *_: SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()))
        page.locator = lambda *_: SimpleNamespace(inner_text=AsyncMock(return_value=text))
        return page

    async def test_transient_error_retried_then_returns_owner(self):
        page = self.page([RuntimeError("timeout"), SimpleNamespace(status=200)],
                         "Owner\nJane Doe\n123 Main St\nFrankfort KY 40601")
        gate = SimpleNamespace(wait=AsyncMock())
        with patch.object(scraper, "accept_agreement", new=AsyncMock()), patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.lookup(page, {"county": "Franklin", "parcel_id": "001"}, self.options(), gate)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(page.goto.await_count, 2)
        self.assertEqual(gate.wait.await_count, 2)

    async def test_rate_limit_stops_without_retries(self):
        page = self.page([SimpleNamespace(status=429)], "")
        with self.assertRaises(scraper.AccessBlocked):
            await scraper.lookup(page, {"county": "Franklin", "parcel_id": "001"}, self.options(),
                                 SimpleNamespace(wait=AsyncMock()))
        self.assertEqual(page.goto.await_count, 1)

    async def test_challenge_requires_manual_mode(self):
        with self.assertRaises(scraper.AccessBlocked):
            await scraper.manual_review(SimpleNamespace(), self.options())

    async def test_manual_review_resumes_only_after_challenge_and_owner_ready(self):
        body = SimpleNamespace(inner_text=AsyncMock(side_effect=[
            "Verify you are human", "Owner\nJane Doe\n123 Main St\nFrankfort KY 40601"]))
        page = SimpleNamespace(bring_to_front=AsyncMock(), locator=lambda *_: body)
        options = SimpleNamespace(manual_review=True, manual_timeout=2)
        with patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            owner = await scraper.manual_review(page, options)
        self.assertEqual(owner["owner_name"], "Jane Doe")
        self.assertEqual(body.inner_text.await_count, 2)

    async def test_manual_timeout_skips_without_retries(self):
        page = self.page([SimpleNamespace(status=403)], "Verify you are human")
        page.bring_to_front = AsyncMock()
        options = SimpleNamespace(retries=2, owner_timeout=1, manual_review=True, manual_timeout=.01)
        result = await scraper.lookup(page, {"county": "Franklin", "parcel_id": "001"}, options,
                                      SimpleNamespace(wait=AsyncMock()))
        self.assertEqual(result["status"], "SKIPPED: MANUAL REVIEW TIMED OUT")
        self.assertEqual(result["owner_name"], "")
        self.assertEqual(page.goto.await_count, 1)

    async def test_manual_timeout_bounds_slow_browser_calls(self):
        async def slow_read():
            await asyncio.sleep(10)

        page = SimpleNamespace(bring_to_front=AsyncMock(),
                               locator=lambda *_: SimpleNamespace(inner_text=slow_read))
        with self.assertRaises(scraper.ManualReviewTimedOut):
            await asyncio.wait_for(scraper.manual_review(page, SimpleNamespace(
                manual_review=True, manual_timeout=.01)), timeout=1)

    async def test_exhausted_failure_is_saved_as_failure(self):
        page = self.page([RuntimeError("timeout"), RuntimeError("timeout")], "")
        with patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.lookup(page, {"county": "Franklin", "parcel_id": "001"}, self.options(),
                                          SimpleNamespace(wait=AsyncMock()))
        self.assertTrue(result["status"].startswith("ERROR:"))
        self.assertEqual(result["owner_name"], "")

    async def test_gate_spaces_parallel_navigation_starts(self):
        gate = scraper.RequestGate(.02)
        starts = []

        async def visit():
            await gate.wait()
            starts.append(asyncio.get_running_loop().time())

        await asyncio.gather(*(visit() for _ in range(3)))
        self.assertGreaterEqual(starts[1] - starts[0], .018)
        self.assertGreaterEqual(starts[2] - starts[1], .018)


if __name__ == "__main__":
    unittest.main()
