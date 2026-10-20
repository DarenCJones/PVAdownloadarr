import asyncio
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import http_transport
from scraper import AccessBlocked, FIELDS


class HttpTests(unittest.TestCase):
    def test_html_text_preserves_owner_lines_and_ignores_scripts(self):
        parser = http_transport.PageText()
        parser.feed("<script>captcha</script><h3>Owner</h3><div>Primary Owner</div>"
                    "<div>Jane &amp; John</div><div>123 Main St<br>Frankfort KY 40601</div>")
        owner = http_transport.parse_owner_text(parser.text())
        self.assertEqual(owner["owner_name"], "Jane & John")

    def test_http_session_cookies_resume_and_block_stops_without_retry(self):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get("Cookie", "")))
                self.send_response(403 if self.path == "/blocked" else 200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "session=test; Path=/")
                self.end_headers()
                self.wfile.write(b"<h3>Owner</h3><p>Jane Doe<br>123 Main St<br>Frankfort KY 40601</p>")

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                self.assertIn("Jane Doe", http_transport.HttpSession(state).fetch(base + "/first", 5))
                options = SimpleNamespace(session_dir=state, interval=0, retries=2, timeout=5, max_failures=3)
                output = io.StringIO()
                csv.DictWriter(output, fieldnames=FIELDS).writeheader()
                with patch.object(http_transport, "build_url", return_value=base + "/second"):
                    self.assertEqual(asyncio.run(http_transport.run_http(
                        [{"county": "Franklin", "parcel_id": "001"}], options, output)), 0)
                self.assertIn("session=test", hits[-1][1])
                self.assertEqual(list(csv.DictReader(io.StringIO(output.getvalue())))[0]["status"], "OK")
                with patch.object(http_transport, "build_url", return_value=base + "/blocked"):
                    self.assertEqual(asyncio.run(http_transport.run_http(
                        [{"county": "Franklin", "parcel_id": "002"}], options, output)), 1)
                self.assertEqual(sum(path == "/blocked" for path, _ in hits), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
