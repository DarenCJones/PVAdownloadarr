# PVAdownloadarr

Look up Kentucky parcel owner mailing information on Beacon/Schneider county websites from a CSV. Results are saved after every parcel and successful lookups are skipped when you restart.

## Setup

Requires Python 3.11 or later. Default HTTP mode and offline exports use only the Python standard library:

```powershell
python PVADownloadarr.py
```

Browser mode is optional. To install it, from this project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Input and usage

The input CSV must have `county` and `parcel_id` columns. Parcel identifiers remain strings, so leading zeros are preserved. County names are case insensitive. Duplicate county/parcel pairs are processed once. Unsupported counties and malformed rows are reported before a browser opens.

```csv
county,parcel_id
Franklin,112-00-00-002.01
Floyd,045-30-05-001.00
```

Run the original entry point:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py
```

Defaults are `ParcelID.csv` and `owner_output.csv` beside the script. Override either path as needed:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py --input "C:\Data\parcels.csv" --output "C:\Data\owners.csv"
```

Validate inputs and see how many parcels remain without opening a browser or changing the output:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py --dry-run
```

## Offline owner exports: avoid dependence on Beacon

If you have an owner export from a county PVA/GIS office or a data provider, match parcels locally:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py --owner-data "C:\Data\county_owners.csv" --output "C:\Data\offline_results.csv"
```

This mode makes **no web requests**, requires no Playwright installation, and never falls back to Beacon. It supports counties outside the built-in Beacon mapping. The export must first be normalized to these columns (rename county/provider fields as needed):

```csv
county,parcel_id,owner_name,owner_addr1,city_state_zip
Franklin,112-00-00-002.01,EXAMPLE OWNER,123 EXAMPLE ROAD,EXAMPLE KY 40000
```

Match on exact parcel IDs, including leading zeros and punctuation. Only county casing and surrounding whitespace are normalized. Combine multiple export address lines into `owner_addr1` when needed, and use the owner's **mailing** address. Conflicting owner rows for the same requested parcel are rejected for review. Missing matches and incomplete mailing addresses remain retryable. Output `url` identifies the local source export; `app_id` is blank. Use a dated source filename and a new output file when refreshing data, because existing successful results are skipped.

Obtaining a current source export is a separate step.

## Default HTTP mode (experimental)

The default `--mode http` makes ordinary HTTP requests, reuses saved HTTP cookies, and parses server-rendered owner blocks. It launches no browser, runs one parcel at a time, and stops immediately on HTTP 401, 403, 429, a recognized access challenge, or an agreement page. Missing owner blocks may mean the page needs JavaScript; they remain retryable. HTTP cookie state and browser cookie state are stored separately and are not interchangeable.

A direct request for one sample Franklin parcel returned HTTP 403 from the development environment on October 20, 2026. The response alone does not establish why it was denied. **Current live Beacon extraction through HTTP has not been verified.** Local fixture tests validate the transport and resume behavior, not live access. It does not solve CAPTCHAs or guarantee that a browser session will make direct HTTP access work.

## Optional browser mode: speed and reliability

Select this mode explicitly after installing Playwright and Chromium:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py --mode browser
```

- One reusable worker page by default. Optional parallel workers each keep their own browser context. Cookies and agreement state survive between parcels in that worker, while workers keep separate sessions.
- Navigation starts are spaced at least two seconds apart across all workers. Retries use the same gate and exponential backoff. No setting guarantees that Beacon will not rate-limit or block access.
- Headless Chromium by default. Use `--headed` to see the browser.
- Owner data is polled until the mailing block appears, rather than assuming the page is ready when the document loads.
- Two extra attempts per failed parcel by default. The run stops after ten consecutive failed results or an access challenge/HTTP 401, 403, or 429. In-flight workers can finish and save their current records.
- Progress includes completed parcels and observed parcels per minute. Actual throughput depends on the county site; no live speedup benchmark has been established.

For one worker with a longer interval:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py --mode browser --workers 1 --interval 2
```

Use `--help` for retry, timeout, and failure-limit options. The tool does not bypass authentication or access challenges.

For current Beacon data with manual CAPTCHA handling:

```powershell
.\.venv\Scripts\python.exe PVADownloadarr.py --manual-review
```

`--manual-review` selects browser mode and uses one visible browser. If a recognized challenge or HTTP 403 appears, it waits up to **30 seconds** for **you** to resolve it through the normal site interface, then resumes when an owner block is visible. If the wait expires, it saves the parcel with status `SKIPPED: MANUAL REVIEW TIMED OUT` and moves to the next record without retrying that parcel during the same run. Skipped parcels remain eligible for retry on the next run and do not count toward the consecutive-failure limit. `--manual-timeout` changes the wait. HTTP 401 and 429 stop the run. Challenge detection depends on page text, so unfamiliar challenge screens may still be recorded as missing owner data. This is not an automated CAPTCHA solver or a guarantee of uninterrupted access.

Cookies and local storage are saved under `.pva-sessions` when worker contexts close and restored on the next run. Sessions can expire or be rejected by the server; this does not remove server access controls. The default directory is ignored by Git. Keep custom `--session-dir` paths private as well. Remove the session directory when the tool is stopped if you need to start with a clean browser session. Only run one process per session directory.

## Saved results and resume

Output columns retain the original format:

```text
county,app_id,parcel_id,owner_name,owner_addr1,city_state_zip,url,status
```

Each result is flushed immediately. Press Ctrl+C to stop; rerun the same command to resume. Only rows with `status=OK` and a nonempty owner name count as completed. Previous failure rows remain as an attempt history, and later retry results are appended. Parallel workers write results in completion order, so use county/parcel ID to match results to input.

Existing output from the original script is supported if it has the same eight columns. Malformed output is rejected to prevent corrupt appends. Use a new output filename when you want to refresh successful parcels. Run only one scraper process per output file.

Exit codes: `0` means all pending parcels succeeded (or validation/no work); `1` means failed or stopped work remains; `2` means a configuration/file/dependency error; `130` means interrupted.

## Limits

County application IDs are carried over from the original project and have not been independently reverified. Extraction retains the original three-line mailing block and optional `Primary Owner` line, with checks for common section headings. Other owner layouts, extra address lines, and page redesigns can require parser changes. Review sample results before relying on a full export. Live Beacon extraction and real-world performance still need validation.

## Tests

Standard-library tests cover input normalization, duplicates, URL escaping, output compatibility, resume after failure, owner parsing, retries, and request spacing:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

An opt-in Chromium integration test uses a local HTTP fixture to check parallel workers, agreement cookies, delayed owner content, CSV output, and resume state. It sends no requests to Beacon:

```powershell
$env:RUN_BROWSER_TESTS = '1'
.\.venv\Scripts\python.exe -m unittest test_browser -v
```

Implementation: `scraper.py`; county mapping: `counties.py`; compatibility entry point: `PVADownloadarr.py`.
