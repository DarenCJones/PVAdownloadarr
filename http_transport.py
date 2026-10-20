"""Experimental browser-free transport for ordinary server-rendered parcel pages."""

import asyncio
import csv
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from scraper import AccessBlocked, CHALLENGE, FIELDS, RequestGate, build_url, parse_owner_text
from counties import COUNTY_APP_IDS


class PageText(HTMLParser):
    """Turn visible HTML blocks into lines without executing scripts."""
    breaks = {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3", "h4", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag in self.breaks and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in self.breaks and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


class HttpSession:
    def __init__(self, directory):
        self.jar = MozillaCookieJar(str(directory / "http-cookies.txt") if directory else None)
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
            if (directory / "http-cookies.txt").exists():
                self.jar.load(ignore_discard=True)
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def fetch(self, url, timeout):
        try:
            request = Request(url, headers={"User-Agent": "PVAdownloadarr/0.2", "Accept": "text/html"})
            with self.opener.open(request, timeout=timeout) as response:
                html = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code in {401, 403, 429}:
                raise AccessBlocked(f"HTTP {exc.code}: stopped; no browser opened and no block bypass attempted") from exc
            raise
        finally:
            if self.jar.filename:
                self.jar.save(ignore_discard=True)
        parser = PageText()
        parser.feed(html)
        text = parser.text()
        if CHALLENGE.search(text):
            raise AccessBlocked("Access challenge detected in HTTP mode; stopped without opening a browser")
        return text


async def run_http(records, options, output):
    session = HttpSession(options.session_dir)
    gate = RequestGate(options.interval)
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    successes, failures = 0, 0
    for index, record in enumerate(records, 1):
        result = {field: "" for field in FIELDS}
        result.update(record, app_id=COUNTY_APP_IDS[record["county"]],
                      url=build_url(record["county"], record["parcel_id"]))
        for attempt in range(options.retries + 1):
            await gate.wait()
            try:
                text = await asyncio.to_thread(session.fetch, result["url"], options.timeout)
                owner = parse_owner_text(text)
                result.update(owner)
                result["status"] = "OK" if owner["owner_name"] else "OWNER NOT FOUND IN HTTP RESPONSE"
                if result["status"] == "OK":
                    break
                if any(line.strip().casefold() in {"i agree", "agree", "accept"} for line in text.splitlines()):
                    raise AccessBlocked("Agreement page requires the normal browser interface; HTTP mode stopped")
            except AccessBlocked as exc:
                print(str(exc), flush=True)
                return 1
            except (OSError, URLError, ValueError) as exc:
                result["status"] = f"ERROR: {type(exc).__name__}: {exc}"
            if attempt < options.retries:
                await asyncio.sleep(min(2 ** attempt, 10))
        writer.writerow(result)
        output.flush()
        if result["status"] == "OK":
            successes += 1
            failures = 0
        else:
            failures += 1
        print(f"[{index}/{len(records)}] {record['county']} {record['parcel_id']}: {result['status']}", flush=True)
        if failures >= options.max_failures:
            print(f"Stopped after {failures} consecutive failed results.", flush=True)
            return 1
    return 0 if successes == len(records) else 1
