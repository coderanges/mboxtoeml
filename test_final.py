"""Final-hardening tests: large attachments, HTML security, date filtering."""
import base64
import json
import mailbox
import os
import quopri
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from pathlib import Path

import read_eml
from read_eml import _attachment_payload_size, parse_eml, sanitize_html_for_preview
from server import Handler


def _eml_file(path, msg):
    Path(path).write_bytes(msg.as_bytes())


class LargeAttachmentTests(unittest.TestCase):
    def _part_with(self, cte, raw_str):
        from email.message import Message

        part = Message()
        part["Content-Type"] = "application/octet-stream"
        part["Content-Transfer-Encoding"] = cte
        part.set_payload(raw_str)
        return part

    def test_small_attachments_exact(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.eml"
            m = EmailMessage()
            m["Subject"] = "t"
            m["From"] = "a@x"
            m["To"] = "b@x"
            m.set_content("hi")
            m.add_attachment(b"12345", maintype="text", subtype="plain", filename="n.txt")
            _eml_file(p, m)
            info = parse_eml(str(p))
            self.assertEqual(len(info["attachments"]), 1)
            self.assertEqual(info["attachments"][0]["size_bytes"], 5)
            self.assertEqual(info["attachments"][0]["filename"], "n.txt")

    def test_large_base64_exact_without_materializing(self):
        data = os.urandom(20 * 1024 * 1024)
        raw = base64.b64encode(data).decode("ascii")
        part = self._part_with("base64", raw)
        tracemalloc.start()
        try:
            size = _attachment_payload_size(part)
        finally:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        self.assertEqual(size, len(data))
        self.assertLess(peak, 8 * 1024 * 1024)  # chunked, far below 20MB payload

    def test_large_qp_exact(self):
        data = (b"hello world\n" * 200000)  # ~2.4MB
        raw = quopri.encodestring(data).decode("ascii")
        part = self._part_with("quoted-printable", raw)
        self.assertEqual(_attachment_payload_size(part), len(data))

    def test_malformed_large_base64_reports_zero(self):
        part = self._part_with("base64", "!!!!" * 700000)  # ~2.8MB of garbage
        self.assertEqual(_attachment_payload_size(part), 0)

    def test_missing_cte_large_uses_length(self):
        part = self._part_with("", "x" * 3_000_000)
        part._headers = [(k, v) for k, v in part._headers if k.lower() != "content-transfer-encoding"]
        self.assertEqual(_attachment_payload_size(part), 3_000_000)

    def test_huge_text_part_capped_with_truncation_flag(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "big.eml"
            m = EmailMessage()
            m["Subject"] = "t"
            m["From"] = "a@x"
            m["To"] = "b@x"
            m.set_content("A" * 10_000_000)
            _eml_file(p, m)
            info = parse_eml(str(p))
            self.assertEqual(len(info["body_text"]), read_eml.MAX_BODY_CHARS)
            self.assertTrue(info["truncated"])

    def test_eml_size_cap_refuses_gracefully(self):
        old = read_eml.MAX_EML_BYTES
        read_eml.MAX_EML_BYTES = 100
        try:
            with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as f:
                f.write(b"x" * 200)
                p = f.name
            with self.assertRaises(ValueError):
                parse_eml(p)
        finally:
            read_eml.MAX_EML_BYTES = old
            try:
                os.unlink(p)
            except OSError:
                pass


class HtmlSecurityTests(unittest.TestCase):
    def _assert_blocked(self, html, *absent):
        out, _ = sanitize_html_for_preview(html)
        for token in absent:
            self.assertNotIn(token, out)
        return out

    def test_mixed_case_and_handlers(self):
        out = self._assert_blocked('<ScRiPt>alert(1)</ScRiPt><IMG SRC="http://t/x.png" ONERROR="y()">', "<script", "onerror", "http://t")
        self.assertNotIn("alert(1)", out)

    def test_encoded_and_whitespace_schemes(self):
        self._assert_blocked('<a href="&#106;avascript:alert(1)">c</a>', "javascript:")
        self._assert_blocked('<a href="java\tscript:alert(1)">c</a>', "java")
        self._assert_blocked('<a href="java\nscript:alert(1)">c</a>', "java")

    def test_executable_and_local_schemes(self):
        self._assert_blocked('<a href="data:text/html,<b>x</b>">c</a>', "data:text/html")
        self._assert_blocked('<a href="data:text/html;base64,SGk=">c</a>', "data:text/html")
        self._assert_blocked('<a href="vbscript:x">c</a>', "vbscript:")
        self._assert_blocked('<img src="file:///etc/passwd">', "file:")
        self._assert_blocked('<img src="about:blank">', "about:")

    def test_svg_attack_surface(self):
        self._assert_blocked("<svg><script>alert(1)</script></svg>", "alert(1)")
        self._assert_blocked('<svg onload="alert(1)"><circle r="5">', "onload")
        self._assert_blocked('<svg><a xlink:href="javascript:alert(1)"><text>c</text></a></svg>', "javascript:")
        self._assert_blocked('<svg><image xlink:href="http://t/x.png" /></svg>', "http://t")

    def test_css_and_structural_tags(self):
        out = self._assert_blocked('<div style="background:url(http://t/x.png)">hi</div>', "http://t")
        self.assertIn("hi", out)
        out = self._assert_blocked('<style>@import "http://t/a.css"</style><p>hi</p>', "http://t")
        self.assertIn("hi", out)
        self._assert_blocked('<form action="http://t/s"><input name="q"></form>', "<form", "<input")
        self._assert_blocked('<base href="http://t/"><a href="/x">c</a>', "<base")
        self._assert_blocked('<object data="http://t/x"></object>', "<object")
        self._assert_blocked('<embed src="http://t/x">', "<embed")
        self._assert_blocked('<iframe src="http://t/f">x</iframe>', "<iframe")

    def test_resource_attrs(self):
        self._assert_blocked('<img srcset="/a.png 1x, http://t/b.png 2x">', "http://t")
        self._assert_blocked('<video poster="http://t/p.png"><source src="/v.mp4"></video>', "http://t")
        self._assert_blocked('<img src="//tracker/x.png">', "//tracker")
        self._assert_blocked('<img src="http://t/pixel.gif" width="1">', "http://t")

    def test_data_url_policy(self):
        out, _ = sanitize_html_for_preview('<img src="data:image/png;base64,AAA">')
        self.assertIn("data:image", out)  # small images retained
        out, n = sanitize_html_for_preview('<img src="data:image/png;base64,' + "A" * 2_000_000 + '">')
        self.assertNotIn("data:image", out)  # oversized dropped
        self.assertGreaterEqual(n, 1)
        self._assert_blocked('<img src="data:font/woff;base64,AAA">', "data:font")
        self._assert_blocked('<a href="DATA:TEXT/HTML,hi">c</a>', "DATA:")

    def test_malformed_and_nested(self):
        out, _ = sanitize_html_for_preview("<script>alert(1)")
        self.assertNotIn("alert(1)", out)
        out = self._assert_blocked('<div><svg><g><a href="JaVaScRiPt:alert(1)">x</a></g></svg></div>', "JaVaScRiPt")
        self.assertIn("x", out)

    def test_safe_formatting_preserved(self):
        out, _ = sanitize_html_for_preview("<p>hi <b>there</b></p><a href=\"https://example.com\">ok</a>")
        self.assertIn("hi", out)
        self.assertIn("https://example.com", out)


class DateFilterTests(unittest.TestCase):
    @classmethod
    def _fixture(cls, td):
        from email.message import EmailMessage

        def msg(subject, date=None, frm="a@x", to="b@x"):
            m = EmailMessage()
            m["Subject"] = subject
            if date is not None:
                # allow raw broken values by direct header injection
                m["Date"] = date
            m["From"] = frm
            m["To"] = to
            m.set_content("body")
            return m

        mp = td / "s.mbox"
        mbox = mailbox.mbox(str(mp))
        try:
            mbox.add(msg("new year", "Mon, 01 Jan 2024 10:30:00 +0000"))
            mbox.add(msg("mid jan", "Mon, 15 Jan 2024 12:00:00 +0000"))
            mbox.add(msg("end jan", "Wed, 31 Jan 2024 23:59:59 +0000"))
            mbox.add(msg("feb one", "Thu, 01 Feb 2024 00:00:00 +0000"))
            mbox.add(msg("tz plus", "Mon, 15 Jan 2024 01:00:00 +0200"))  # = 14 Jan 23:00 UTC
            mbox.add(msg("no date", None))
            bad = msg("bad date", "Mon, 01 Jan 2024 10:30:00 +0000")
            del bad["Date"]
            bad["Date"] = "not a date at all {{{"
            mbox.add(bad)
            mbox.flush()
        finally:
            mbox.close()
        return mp

    def _serve(self, mp, out):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        req = urllib.request.Request(
            base + "/api/convert",
            data=json.dumps({"mbox_path": str(mp), "output_dir": str(out), "collision": "overwrite"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            jid = json.loads(r.read().decode())["job_id"]
        for _ in range(100):
            with urllib.request.urlopen(f"{base}/api/jobs/{jid}", timeout=5) as r:
                if json.loads(r.read().decode())["done"]:
                    break
            time.sleep(0.1)
        return httpd, t, base, jid

    def _get(self, base, jid, **qs):
        url = f"{base}/api/jobs/{jid}/results?{urllib.parse.urlencode(qs)}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return 200, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _subjects(self, base, jid, **qs):
        code, data = self._get(base, jid, **qs)
        self.assertEqual(code, 200, data)
        return sorted(i["subject"] for i in data["items"]), data

    def test_date_filters(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            httpd, t, base, jid = self._serve(self._fixture(td), td / "o")
            try:
                # 1. date_from only
                subs, _ = self._subjects(base, jid, date_from="2024-01-15")
                self.assertEqual(subs, ["end jan", "feb one", "mid jan"])
                # 2. date_to only
                subs, _ = self._subjects(base, jid, date_to="2024-01-15")
                self.assertEqual(subs, ["mid jan", "new year", "tz plus"])
                # 3. inclusive exact boundary (mid jan at 2024-01-15)
                subs, _ = self._subjects(base, jid, date_from="2024-01-15", date_to="2024-01-15")
                self.assertEqual(subs, ["mid jan"])
                # 4. range
                subs, _ = self._subjects(base, jid, date_from="2024-01-02", date_to="2024-01-31")
                self.assertEqual(subs, ["end jan", "mid jan", "tz plus"])
                # 5/6. no-date + malformed excluded from ranges, present unfiltered
                subs, data = self._subjects(base, jid, date_from="2020-01-01", date_to="2030-01-01")
                self.assertNotIn("no date", subs)
                self.assertNotIn("bad date", subs)
                subs_all, _ = self._subjects(base, jid)
                self.assertIn("no date", subs_all)
                self.assertIn("bad date", subs_all)
                # 7. timezone: 15 Jan 01:00 +0200 == 14 Jan 23:00 UTC
                subs, _ = self._subjects(base, jid, date_from="2024-01-14", date_to="2024-01-14")
                self.assertEqual(subs, ["tz plus"])
                # 8. from > to rejected
                code, data = self._get(base, jid, date_from="2024-02-01", date_to="2024-01-01")
                self.assertEqual(code, 400)
                # 9. invalid format rejected
                code, _ = self._get(base, jid, date_from="15-01-2024")
                self.assertEqual(code, 400)
                code, _ = self._get(base, jid, date_to="yesterday")
                self.assertEqual(code, 400)
                # 10. date + text search
                subs, _ = self._subjects(base, jid, q="jan", date_from="2024-01-15")
                self.assertEqual(subs, ["end jan", "mid jan"])
                # 11. date + status filter
                subs, _ = self._subjects(base, jid, date_from="2024-01-01", status="ok")
                self.assertIn("mid jan", subs)
                # 12. date + pagination
                _, d1 = self._subjects(base, jid, date_from="2024-01-01", limit=2, offset=0)
                _, d2 = self._subjects(base, jid, date_from="2024-01-01", limit=2, offset=2)
                self.assertEqual(d1["results_total"], 5)
                got = [i["subject"] for i in d1["items"]] + [i["subject"] for i in d2["items"]]
                self.assertEqual(len(got), 4)
                self.assertEqual(len(set(got)), 4)
                # 13. empty date-filtered result
                subs, data = self._subjects(base, jid, date_from="2031-01-01", date_to="2031-12-31")
                self.assertEqual(subs, [])
                self.assertEqual(data["results_total"], 0)
            finally:
                httpd.shutdown()
                t.join(timeout=5)


class ErrorHygieneTests(unittest.TestCase):
    def test_internal_errors_hide_details(self):
        import server as srv

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            mbox = mailbox.mbox(str(mp))
            try:
                m = EmailMessage()
                m["Subject"] = "x"
                m["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"
                m["From"] = "a@x"
                m["To"] = "b@x"
                m.set_content("b")
                mbox.add(m)
                mbox.flush()
            finally:
                mbox.close()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                req = urllib.request.Request(
                    base + "/api/convert",
                    data=json.dumps({"mbox_path": str(mp), "output_dir": str(td / "o"),
                                     "collision": "overwrite"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    jid = json.loads(r.read().decode())["job_id"]
                orig = srv._read_result_lines
                srv._read_result_lines = lambda job: (_ for _ in ()).throw(RuntimeError("boom-secret"))
                try:
                    try:
                        urllib.request.urlopen(f"{base}/api/jobs/{jid}/results", timeout=5)
                        self.fail("expected HTTP error")
                    except urllib.error.HTTPError as e:
                        self.assertEqual(e.code, 500)
                        body = e.read().decode()
                        self.assertNotIn("boom-secret", body)
                        self.assertNotIn("Traceback", body)
                        self.assertIn("Internal server error", body)
                finally:
                    srv._read_result_lines = orig
            finally:
                httpd.shutdown()
                t.join(timeout=5)


class DateFrontendContractTests(unittest.TestCase):
    def test_date_controls_present_and_wired(self):
        html = Path("web/index.html").read_text()
        js = Path("web/app.js").read_text()
        self.assertIn('id="dateFrom"', html)
        self.assertIn('id="dateTo"', html)
        self.assertIn('id="clearFilters"', html)
        self.assertIn("date_from", js)
        self.assertIn("date_to", js)
        self.assertIn("dateFrom", js)


if __name__ == "__main__":
    unittest.main()
