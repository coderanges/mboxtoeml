"""UX-phase contract tests: search, download, sanitized preview, viewer metadata.

Covers new API/frontend contracts without screenshot tests.
"""
import json
import mailbox
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from pathlib import Path

from read_eml import parse_eml, sanitize_html_for_preview
from server import Handler, create_job


def _msg(subject="Subject", frm="alice@example.com", to="bob@example.com", body="hello"):
    m = EmailMessage()
    m["Subject"] = subject
    m["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"
    m["From"] = frm
    m["To"] = to
    m.set_content(body)
    return m


def _mbox(path, msgs):
    mbox = mailbox.mbox(str(path))
    try:
        for m in msgs:
            mbox.add(m)
        mbox.flush()
    finally:
        mbox.close()


class SanitizerTests(unittest.TestCase):
    def test_blocks_scripts_handlers_remote_and_dangerous_urls(self):
        evil = (
            '<script>alert(1)</script>'
            '<p onclick="x()" style="color:red">hi</p>'
            '<img src="http://tracker/x.png"><img srcset="http://t/a.png 1x">'
            '<a href="javascript:alert(2)">c</a>'
            '<a href="https://example.com/ok">ok</a>'
            '<link rel="stylesheet" href="http://t/s.css">'
            '<meta http-equiv="refresh" content="0;url=http://t/">'
            '<iframe src="http://t/f.html"></iframe>'
        )
        s, n = sanitize_html_for_preview(evil)
        self.assertNotIn("<script", s)
        self.assertNotIn("onclick", s)
        self.assertNotIn("style=", s)
        self.assertNotIn("http://tracker", s)
        self.assertNotIn("javascript:", s)
        self.assertNotIn("<iframe", s)
        self.assertNotIn("<link", s)
        self.assertNotIn("<meta", s)
        self.assertIn("hi", s)
        self.assertIn("https://example.com/ok", s)  # plain links preserved
        self.assertGreaterEqual(n, 5)

    def test_keeps_data_images_drops_remote(self):
        s, n = sanitize_html_for_preview(
            '<img src="data:image/png;base64,AAA"><img src="//tracker/x.png">'
        )
        self.assertIn("data:image", s)
        self.assertNotIn("//tracker", s)
        self.assertEqual(n, 1)

    def test_parse_eml_exposes_sanitized_preview(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "e.eml"
            m = EmailMessage()
            m["Subject"] = "t"
            m["From"] = "a@x"
            m["To"] = "b@x"
            m.set_content('<p>hi</p><img src="http://t/x.png">', subtype="html")
            p.write_bytes(m.as_bytes())
            info = parse_eml(str(p))
            self.assertIn("body_html_sanitized", info)
            self.assertNotIn("http://t/x.png", info["body_html_sanitized"])
            self.assertGreaterEqual(info["remote_blocked"], 1)
            self.assertIn("hi", info["body_html_sanitized"])


class ResultSearchTests(unittest.TestCase):
    def _live(self, td):
        mp = td / "s.mbox"
        _mbox(mp, [
            _msg("Project kickoff", frm="alice@example.com", to="bob@example.com"),
            _msg("Invoice March", frm="carol@example.com", to="dave@example.com"),
            _msg("Project retrospective", frm="erin@example.com", to="bob@example.com"),
        ])
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        return httpd, t, base, mp

    def _convert(self, base, mp, out):
        req = urllib.request.Request(
            base + "/api/convert",
            data=json.dumps({"mbox_path": str(mp), "output_dir": str(out), "collision": "rename"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())["job_id"]

    def _wait(self, base, jid):
        for _ in range(100):
            with urllib.request.urlopen(f"{base}/api/jobs/{jid}", timeout=5) as r:
                st = json.loads(r.read().decode())
            if st["done"]:
                return st
            time.sleep(0.1)
        self.fail("job did not finish")

    def _results(self, base, jid, **qs):
        q = urllib.parse.urlencode(qs)
        with urllib.request.urlopen(f"{base}/api/jobs/{jid}/results?{q}", timeout=5) as r:
            return json.loads(r.read().decode())

    def test_search_across_subject_sender_recipient_filename(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            httpd, t, base, mp = self._live(td)
            try:
                jid = self._convert(base, mp, td / "o")
                self._wait(base, jid)
                self.assertEqual(self._results(base, jid, q="project")["results_total"], 2)
                self.assertEqual(self._results(base, jid, q="carol")["results_total"], 1)
                self.assertEqual(self._results(base, jid, q="bob@example.com")["results_total"], 2)
                self.assertEqual(self._results(base, jid, q="nomatch-xyz")["results_total"], 0)
                # combined with status filter
                r = self._results(base, jid, q="project", status="ok")
                self.assertEqual(r["results_total"], 2)
            finally:
                httpd.shutdown()
                t.join(timeout=5)

    def test_result_items_carry_sender_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _mbox(mp, [_msg("Hi", frm="alice@example.com", to="bob@example.com")])
            httpd, t, base = self._live(td)[:3]
            try:
                jid = self._convert(base, mp, td / "o2")
                self._wait(base, jid)
                r = self._results(base, jid)
                self.assertEqual(r["items"][0]["from"], "alice@example.com")
                self.assertIn("bob@example.com", r["items"][0]["to"])
            finally:
                httpd.shutdown()
                t.join(timeout=5)


class DownloadTests(unittest.TestCase):
    def test_download_returns_bytes_with_containment(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _mbox(mp, [_msg("Hello download", body="download-body-123")])
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                req = urllib.request.Request(
                    base + "/api/convert",
                    data=json.dumps({"mbox_path": str(mp), "output_dir": str(td / "o"), "collision": "overwrite"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    jid = json.loads(r.read().decode())["job_id"]
                for _ in range(100):
                    with urllib.request.urlopen(f"{base}/api/jobs/{jid}", timeout=5) as r:
                        if json.loads(r.read().decode())["done"]:
                            break
                    time.sleep(0.1)
                with urllib.request.urlopen(f"{base}/api/jobs/{jid}/results?limit=5", timeout=5) as r:
                    fn = json.loads(r.read().decode())["items"][0]["filename"]
                url = f"{base}/api/eml/download?job_id={jid}&filename={urllib.parse.quote(fn)}"
                with urllib.request.urlopen(url, timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn("message/rfc822", r.headers.get("Content-Type", ""))
                    self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
                    body = r.read()
                self.assertIn(b"download-body-123", body)
                # traversal blocked
                bad = f"{base}/api/eml/download?job_id={jid}&filename=..%2Fevil.eml"
                try:
                    urllib.request.urlopen(bad, timeout=5)
                    self.fail("expected error")
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (400, 404))
            finally:
                httpd.shutdown()
                t.join(timeout=5)


class FrontendContractTests(unittest.TestCase):
    def test_workflow_and_defaults(self):
        html = Path("web/index.html").read_text()
        js = Path("web/app.js").read_text()
        # workflow order: source -> setup -> convert -> results
        order = [html.index(x) for x in ["Source mailbox", "Conversion setup", "Conversion</h", "Results</h", "Message</h"]]
        self.assertEqual(order, sorted(order))
        # Rename is the checked default
        self.assertIn('value="rename" checked', html)
        # Convert starts disabled (readiness gating)
        self.assertIn('id="startBtn"', html)
        self.assertIn("disabled", html.split('id="startBtn"')[1][:80])
        # inspection states exist
        for token in ["Idle", "Inspecting", "Ready", "Could not inspect"]:
            self.assertIn(token, js)
        # search + filter + pagination wired to backend q param
        self.assertIn("q: S.results.query", js)
        self.assertIn('name="filter"', html)
        self.assertIn('id="search"', html)
        # ETA + elapsed + cancelling states
        for token in ["Remaining", "Cancelling", "Partial output remains", "Conversion complete", "Conversion cancelled"]:
            self.assertIn(token, html if token in ("Remaining",) else js + html)
        # sandbox preserved, sanitized preview used, download link present
        self.assertIn('sandbox=""', html)
        self.assertNotIn("allow-scripts", html + js)
        self.assertIn("body_html_sanitized", js)
        self.assertIn("/api/eml/download", js)
        self.assertIn("Reply-To", html)
        self.assertIn('name="view"', html)


if __name__ == "__main__":
    unittest.main()
