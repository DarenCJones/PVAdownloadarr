"""Resumable Beacon parcel owner lookups. Importing this module performs no IO."""

import argparse
import asyncio
import csv
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from counties import COUNTY_APP_IDS

FIELDS = ["county", "app_id", "parcel_id", "owner_name", "owner_addr1",
          "city_state_zip", "url", "status"]
COUNTIES = {name.casefold(): name for name in COUNTY_APP_IDS}
CHALLENGE = re.compile(r"captcha|access denied|too many requests|verify (?:that )?you are human|checking your browser", re.I)


def canonical_county(value):
    value = value.strip()
    return COUNTIES.get(value.casefold(), value.title())


def load_records_from_csv(path, allow_other_counties=False):
    records, seen = [], set()
    with open(path, newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        headers = [name.strip().lower() for name in (reader.fieldnames or [])]
        if len(set(headers)) != len(headers) or not {"county", "parcel_id"} <= set(headers):
            raise ValueError("Input must have unique headers including county, parcel_id")
        reader.fieldnames = headers
        for row in reader:
            if None in row:
                raise ValueError(f"Input line {reader.line_num}: too many columns")
            county = canonical_county(row.get("county") or "")
            parcel = (row.get("parcel_id") or "").strip()
            if not county and not parcel:
                continue
            if not county or not parcel:
                raise ValueError(f"Input line {reader.line_num}: county and parcel_id are required")
            if county not in COUNTY_APP_IDS and not allow_other_counties:
                raise ValueError(f"Input line {reader.line_num}: unsupported county {county!r}")
            key = county, parcel
            if key not in seen:
                seen.add(key)
                records.append({"county": county, "parcel_id": parcel})
    return records


def build_url(county, parcel_id):
    return "https://beacon.schneidercorp.com/Application.aspx?" + urlencode({
        "AppID": COUNTY_APP_IDS[county], "PageTypeID": 4, "KeyValue": parcel_id,
    })


def load_processed_records(path):
    """Only successful results are complete; legacy eight-column CSVs are accepted."""
    processed = set()
    if not path.exists() or path.stat().st_size == 0:
        return processed
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != FIELDS:
            raise ValueError("Output headers do not match. Choose a new --output file.")
        for row in reader:
            if None in row or any(row.get(field) is None for field in FIELDS):
                raise ValueError(f"Output line {reader.line_num} is incomplete; repair it before resuming")
            if row["status"].strip() == "OK" and row["owner_name"].strip():
                processed.add((canonical_county(row["county"]), row["parcel_id"].strip()))
    return processed


def load_owner_data(path, records):
    """Index only requested parcels from a normalized county/provider CSV export."""
    needed = {(r["county"], r["parcel_id"]) for r in records}
    owners = {}
    required = {"county", "parcel_id", *FIELDS[3:6]}
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        headers = [name.strip().lower() for name in (reader.fieldnames or [])]
        if len(set(headers)) != len(headers) or not required <= set(headers):
            raise ValueError("Owner data needs unique headers including: " + ", ".join(sorted(required)))
        reader.fieldnames = headers
        for row in reader:
            if None in row or any(row.get(field) is None for field in required):
                raise ValueError(f"Owner data line {reader.line_num}: malformed row")
            key = canonical_county(row["county"]), row["parcel_id"].strip()
            if key not in needed:
                continue
            owner = {field: row[field].strip() for field in FIELDS[3:6]}
            if key in owners and owner != owners[key]:
                raise ValueError(f"Conflicting owner data for {key[0]} parcel {key[1]}; resolve before running")
            owners[key] = owner
    return owners


def run_offline(records, owners, source, output):
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    successes = 0
    for record in records:
        result = {field: "" for field in FIELDS}
        result.update(record, url=source.resolve().as_uri())
        owner = owners.get((record["county"], record["parcel_id"]))
        if owner is None:
            result["status"] = "NOT FOUND IN OWNER DATA"
        else:
            result.update(owner)
            result["status"] = "OK" if all(owner.values()) else "INCOMPLETE OWNER DATA"
        writer.writerow(result)
        output.flush()
        successes += result["status"] == "OK"
    print(f"Offline results: {successes} complete, {len(records) - successes} unresolved. No web requests made.")
    return 0 if successes == len(records) else 1


def parse_owner_text(text):
    """Preserve the existing three-line mailing block, including Primary Owner."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    empty = dict.fromkeys(FIELDS[3:6], "")
    section_names = {"owner", "primary owner", "ownership", "parcel", "parcel information",
                     "property information", "valuation", "assessment", "sales", "sales history",
                     "tax information", "land", "buildings", "legal description"}
    for index, line in enumerate(lines):
        if line.casefold().rstrip(":") != "owner":
            continue
        block = lines[index + 1:]
        if block and block[0].casefold() == "primary owner":
            block = block[1:]
        if len(block) < 3 or any(value.casefold().rstrip(":") in section_names for value in block[:3]):
            continue
        # Reject navigation/section text that could otherwise become an apparent success.
        return dict(zip(FIELDS[3:6], block[:3]))
    return empty


class RequestGate:
    """Space navigation starts across all workers, including retries."""
    def __init__(self, interval):
        self.interval = interval
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next_start - time.monotonic()))
            self.next_start = time.monotonic() + self.interval


class AccessBlocked(Exception):
    pass


class ManualReviewTimedOut(Exception):
    pass


async def manual_review(page, options):
    if not getattr(options, "manual_review", False):
        raise AccessBlocked("Access challenge detected. Rerun with --manual-review to resolve it in the browser.")
    print(f"Resolve the access challenge in the visible browser. Waiting up to {options.manual_timeout}s.", flush=True)
    try:
        # Bound the entire wait, including browser calls that can otherwise take longer.
        async with asyncio.timeout(options.manual_timeout):
            await page.bring_to_front()
            while True:
                text = await page.locator("body").inner_text()
                owner = parse_owner_text(text)
                if not CHALLENGE.search(text) and owner["owner_name"]:
                    return owner
                await asyncio.sleep(1)
    except TimeoutError as exc:
        raise ManualReviewTimedOut("SKIPPED: MANUAL REVIEW TIMED OUT") from exc


async def accept_agreement(page):
    # One exact-match locator avoids clicking unrelated links containing "agree".
    controls = page.locator("button, a").filter(has_text=re.compile(r"^\s*(?:I\s+)?(?:Agree|Accept)\s*$", re.I))
    inputs = page.locator("input[value='Agree' i], input[value='I Agree' i], input[value='Accept' i]")
    for control in (controls.first, inputs.first):
        if await control.is_visible():
            await control.click()
            await page.wait_for_load_state("domcontentloaded")
            return True
    return False


async def lookup(page, record, options, gate):
    result = {field: "" for field in FIELDS}
    result.update(record, app_id=COUNTY_APP_IDS[record["county"]],
                  url=build_url(record["county"], record["parcel_id"]))
    for attempt in range(options.retries + 1):
        try:
            await gate.wait()
            response = await page.goto(result["url"], wait_until="domcontentloaded")
            if response and response.status in {401, 429}:
                raise AccessBlocked(f"HTTP {response.status}; stopped for access/rate limit")
            if response and response.status == 403:
                result.update(await manual_review(page, options), status="OK")
                return result
            if response and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            await accept_agreement(page)
            # Poll the owner block: its heading may render before the address arrives.
            deadline = time.monotonic() + options.owner_timeout
            while True:
                text = await page.locator("body").inner_text()
                if CHALLENGE.search(text):
                    result.update(await manual_review(page, options), status="OK")
                    return result
                owner = parse_owner_text(text)
                if owner["owner_name"] or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(.2)
            result.update(owner)
            result["status"] = "OK" if owner["owner_name"] else "OWNER NOT FOUND"
            if result["status"] == "OK":
                return result
        except ManualReviewTimedOut as exc:
            result["status"] = str(exc)
            return result
        except AccessBlocked:
            raise
        except Exception as exc:
            result["status"] = f"ERROR: {type(exc).__name__}: {exc}"
        if attempt < options.retries:
            await asyncio.sleep(min(2 ** attempt, 10))
    return result


async def run_scraper(records, options, output):
    # Lazy import allows CSV validation, --help, and unit tests without Playwright.
    from playwright.async_api import async_playwright

    queue = asyncio.Queue()
    for record in records:
        queue.put_nowait(record)
    gate, stop = RequestGate(options.interval), asyncio.Event()
    count, failures, successes = 0, 0, 0
    started = time.monotonic()
    writer = csv.DictWriter(output, fieldnames=FIELDS)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not options.headed)

        async def worker():
            nonlocal count, failures, successes
            # Each worker reuses its own page and cookies without sharing ASP.NET session state.
            state_path = None
            if getattr(options, "session_dir", None):
                options.session_dir.mkdir(parents=True, exist_ok=True)
                state_path = options.session_dir / f"worker-{asyncio.current_task().get_name()}.json"
            context = await browser.new_context(**(
                {"storage_state": str(state_path)} if state_path and state_path.exists() else {}))
            context.set_default_timeout(options.timeout * 1000)
            page = await context.new_page()
            try:
                while not stop.is_set():
                    try:
                        record = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        result = await lookup(page, record, options, gate)
                    except AccessBlocked as exc:
                        stop.set()
                        print(str(exc), flush=True)
                        break
                    # No await between writing and flushing: CSV writes are serialized.
                    writer.writerow(result)
                    output.flush()
                    count += 1
                    if result["status"] == "OK":
                        successes += 1
                        failures = 0
                    elif result["status"] != "SKIPPED: MANUAL REVIEW TIMED OUT":
                        failures += 1
                    elapsed = time.monotonic() - started
                    print(f"[{count}/{len(records)}] {record['county']} {record['parcel_id']}: "
                          f"{result['status']} | {count / max(elapsed, .001) * 60:.1f} parcels/min",
                          flush=True)
                    if failures >= options.max_failures:
                        stop.set()
                        print(f"Stopped after {failures} consecutive failed results.", flush=True)
            finally:
                try:
                    if state_path:
                        await context.storage_state(path=str(state_path))
                finally:
                    await context.close()

        workers = [asyncio.create_task(worker(), name=str(i)) for i in range(min(options.workers, len(records)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await browser.close()
    print(f"Saved {successes} successful and {count - successes} failed results to {options.output}")
    return 1 if stop.is_set() or successes != len(records) else 0


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def nonnegative_float(value):
    number = float(value)
    if not 0 <= number < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite number at least 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    directory = Path(__file__).resolve().parent
    parser.add_argument("--input", type=Path, default=directory / "ParcelID.csv")
    parser.add_argument("--output", type=Path, default=directory / "owner_output.csv")
    parser.add_argument("--owner-data", type=Path,
                        help="Match a normalized owner export CSV locally; makes no web requests")
    parser.add_argument("--mode", choices=("http", "browser"), default="http",
                        help="HTTP makes no browser launches; browser uses Playwright (default: http)")
    parser.add_argument("--workers", type=positive_int, default=1, help="Reusable browser sessions (default: 1)")
    parser.add_argument("--interval", type=nonnegative_float, default=2,
                        help="Minimum seconds between navigation starts across workers (default: 2)")
    parser.add_argument("--retries", type=nonnegative_int, default=2, help="Extra attempts per parcel (default: 2)")
    parser.add_argument("--timeout", type=positive_int, default=45, help="Browser operation timeout in seconds")
    parser.add_argument("--owner-timeout", type=positive_int, default=10, help="Owner section wait in seconds")
    parser.add_argument("--max-failures", type=positive_int, default=10)
    parser.add_argument("--headed", action="store_true", help="Show browser windows")
    parser.add_argument("--manual-review", action="store_true",
                        help="Show the browser and wait for you to resolve an access challenge (one worker)")
    parser.add_argument("--manual-timeout", type=positive_int, default=30,
                        help="Seconds to wait for manual review before skipping the parcel (default: 30)")
    parser.add_argument("--session-dir", type=Path, default=directory / ".pva-sessions",
                        help="Local browser cookie/storage directory, reused between runs")
    parser.add_argument("--dry-run", action="store_true", help="Validate files and show work without opening browser")
    options = parser.parse_args(argv)
    if options.headed:
        options.mode = "browser"
    if options.manual_review:
        options.headed = True
        options.mode = "browser"
        if options.workers != 1:
            parser.error("--manual-review requires --workers 1")
    try:
        if options.input.resolve() == options.output.resolve():
            raise ValueError("Input and output must be different files")
        if options.owner_data and options.owner_data.resolve() == options.output.resolve():
            raise ValueError("Owner data and output must be different files")
        records = load_records_from_csv(options.input, allow_other_counties=bool(options.owner_data))
        processed = load_processed_records(options.output)
        pending = [r for r in records if (r["county"], r["parcel_id"]) not in processed]
        print(f"Unique input parcels: {len(records)} | Already successful: {len(records) - len(pending)} | "
              f"Remaining: {len(pending)}")
        owners = load_owner_data(options.owner_data, pending) if options.owner_data else None
        if options.dry_run or not pending:
            return 0
        needs_header = not options.output.exists() or options.output.stat().st_size == 0
        with options.output.open("a", newline="", encoding="utf-8") as output:
            if needs_header:
                csv.DictWriter(output, fieldnames=FIELDS).writeheader()
                output.flush()
            if owners is not None:
                return run_offline(pending, owners, options.owner_data, output)
            if options.mode == "http":
                if options.workers != 1:
                    raise ValueError("HTTP mode currently requires --workers 1")
                from http_transport import run_http
                return asyncio.run(run_http(pending, options, output))
            return asyncio.run(run_scraper(pending, options, output))
    except KeyboardInterrupt:
        print("Stopped. Saved rows are preserved; run again to resume.")
        return 130
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
