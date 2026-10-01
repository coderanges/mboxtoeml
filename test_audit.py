"""Regression tests from the stress/concurrency/lifecycle/security audit.

No major features; each test pins a real bug found by measurement.
"""
import json
import mailbox
import os
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

import server
from server import Handler, create_job, get_job
from server import _read_result_lines as read_lines
from server import resolve_job_file, validate_job_filename


def _msg(subject="s", body="b", date="Mon, 01 Jan 2024 10:30:00 +0000"):
    m = EmailMessage()
    m["Subject"] = subject
    m["Date"] = date
    m["From"] = "a@x"
    m["To"] = "b@x"
    m.set_content(body)
    return m


def _make_mbox(path, n, subject_prefix="m"):
    mbox = mailbox.mbox(str(path))
    try:
        for i in range(n):
            mbox.add(_msg(f"{subject_prefix}{i}"))
        mbox.flush()
    finally:
        mbox.close()


class ConcurrentRenameTests(unittest.TestCase):
    def test_concurrent_rename_loses_nothing(self):
        # Regression: check-then-act rename lost one file (59/60) under 3x20 concurrency.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            out = td / "out"
            out.mkdir()
            _make_mbox(mp, 20, subject_prefix="Same subject ")
            # same subject for all to force rename collisions within each job too
            results = {}
            errs = {}

            def run(tag):
                try:
                    from mbox2eml import convert_detailed

                    results[tag] = convert_detailed(str(mp), str(out), collision="rename")
                except Exception as exc:  # noqa: BLE001
                    errs[tag] = exc

            ths = [threading.Thread(target=run, args=(i,)) for i in range(3)]
            for t in ths:
                t.start()
            for t in ths:
                t.join(60)
            self.assertFalse(errs, errs)
            files = [p for p in out.iterdir() if p.suffix == ".eml"]
            # 3 jobs x 20 messages must yield 60 distinct files with atomic O_EXCL
            self.assertEqual(len(files), 60)
            self.assertEqual(len({p.name for p in files}), 60)


class CancellationTests(unittest.TestCase):
    def test_immediate_cancel_reports_cancelled_with_partials_kept(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 50)
            job = create_job(str(mp), str(td / "o1"), "overwrite")
            job["cancel_event"].set()
            job["thread"].join(15)
            self.assertEqual(job["status"], "cancelled")
            self.assertNotEqual(job["status"], "done")
            # partial outputs remain on disk deliberately (resumable, inspectable)
            emls = list((td / "o1").glob("*.eml")) if (td / "o1").exists() else []
            self.assertLessEqual(len(emls), 50)

    def test_late_cancel_after_full_completion_stays_done(self):
        # A cancel racing a fully completed run must not rewrite done -> cancelled.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 5)
            job = create_job(str(mp), str(td / "o2"), "overwrite")
            job["thread"].join(15)
            self.assertEqual(job["status"], "done")
            job["cancel_event"].set()  # too late: work is finished
            time.sleep(0.1)
            # status must remain done since report["cancelled"] was False
            self.assertEqual(job["status"], "done")

    def test_cancel_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 3)
            job = create_job(str(mp), str(td / "o3"), "overwrite")
            job["thread"].join(15)
            from server import get_job as _get

            self.assertIsNotNone(_get(job["job_id"]))
            # cancelling twice is safe
            job["cancel_event"].set()
            job["cancel_event"].set()
            self.assertIn(job["status"], ("done", "cancelled"))


class CollisionEdgeTests(unittest.TestCase):
    def test_rename_near_max_length(self):
        from mbox2eml import MAX_FILENAME_LENGTH, convert_detailed

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            long_subject = "x" * 200
            _make_mbox(mp, 1)
            # rewrite subject to be very long
            mbox = mailbox.mbox(str(mp))
            try:
                keys = list(mbox.keys())
                msg = mbox[keys[0]]
                del msg["Subject"]
                msg["Subject"] = long_subject
                mbox[keys[0]] = msg
                mbox.flush()
            finally:
                mbox.close()
            out = td / "out"
            r1 = convert_detailed(str(mp), str(out), collision="overwrite")
            self.assertEqual(r1["succeeded"], 1)
            r2 = convert_detailed(str(mp), str(out), collision="rename")
            self.assertEqual(r2["succeeded"], 1)
            name = Path(r2["created"][0]).name
            self.assertLessEqual(len(name), MAX_FILENAME_LENGTH)
            self.assertIn("(2)", name)


class FilesystemSecurityTests(unittest.TestCase):
    def test_control_chars_rejected(self):
        for bad in ["a\x01b.eml", "a\x7fb.eml", "a\nb.eml", "a\rb.eml"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_job_filename(bad)

    def test_symlink_containment_uses_realpath(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "out"
            out.mkdir()
            (out / "a.eml").write_bytes(b"x")
            job = {"output_dir": str(out)}
            self.assertTrue(resolve_job_file(job, "a.eml").endswith("a.eml"))
            # subdir slash traversal still blocked even if a symlink exists inside
            (out / "link").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                resolve_job_file(job, "link/evil.eml")


class HttpConsistencyTests(unittest.TestCase):
    def _server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, t, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _code(self, base, method, path, body=None):
        req = urllib.request.Request(base + path, data=body, headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw or "{}")
            except ValueError:
                return e.code, {"_raw": raw[:200]}

    def test_wrong_types_are_400_not_500(self):
        httpd, t, base = self._server()
        try:
            code, data = self._code(base, "POST", "/api/inspect", json.dumps({"mbox_path": 123}).encode())
            self.assertEqual(code, 400)
            self.assertIn("error", data)
            code, data = self._code(base, "POST", "/api/eml/modify", json.dumps({"job_id": 123}).encode())
            self.assertIn(code, (400, 404))
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_unsupported_methods_are_json_405(self):
        httpd, t, base = self._server()
        try:
            for method in ("PUT", "DELETE", "PATCH"):
                code, data = self._code(base, method, "/api/inspect", b"{}")
                self.assertEqual(code, 405, method)
                self.assertIn("error", data)
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_encoded_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 1)
            httpd, t, base = self._server()
            try:
                # create a real job first
                req = urllib.request.Request(
                    base + "/api/convert",
                    data=json.dumps({"mbox_path": str(mp), "output_dir": str(td / "o"), "collision": "overwrite"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    job_id = json.loads(r.read().decode())["job_id"]
                for evil in ["..%2Fevil.eml", "%252e%252e%252fetc", "..%5Cevil.eml", "%00.eml"]:
                    code, _ = self._code(base, "GET", f"/api/eml?job_id={job_id}&filename={evil}")
                    self.assertIn(code, (400, 404), evil)
            finally:
                httpd.shutdown()
                t.join(timeout=5)

    def test_sse_repeated_and_polling_coexist(self):
        import socket

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 5)
            httpd, t, base = self._server()
            try:
                req = urllib.request.Request(
                    base + "/api/convert",
                    data=json.dumps({"mbox_path": str(mp), "output_dir": str(td / "o"), "collision": "overwrite"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    job_id = json.loads(r.read().decode())["job_id"]

                def fetch_sse_once():
                    s = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=10)
                    s.sendall(f"GET /api/jobs/{job_id}/events HTTP/1.0\r\nHost: x\r\n\r\n".encode())
                    data = b""
                    s.settimeout(5)
                    try:
                        while b"data:" not in data:
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                    finally:
                        s.close()
                    return data

                first = fetch_sse_once()
                self.assertIn(b"data:", first)
                second = fetch_sse_once()  # repeated SSE must also work
                self.assertIn(b"data:", second)
                # polling while SSE active
                with urllib.request.urlopen(f"{base}/api/jobs/{job_id}", timeout=5) as r:
                    st = json.loads(r.read().decode())
                self.assertIn("status", st)
            finally:
                httpd.shutdown()
                t.join(timeout=5)


class HostileEmlTests(unittest.TestCase):
    def _parse_raw(self, raw):
        from read_eml import parse_eml

        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as f:
            f.write(raw)
            p = f.name
        try:
            return parse_eml(p)
        finally:
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_hostile_inputs_never_raise(self):
        cases = [
            b"Subject: \xff\xfe broken\r\nFrom: \r\nTo: a\r\n\r\nbody",
            b"Content-Type: multipart/mixed; boundary=XXX\r\n\r\n--XXX\r\nContent-Type: text/plain\r\n\r\nhi\r\n--XXX--garbage",
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: bogus-xyz\r\n\r\nhello",
            b"Subject: t\r\nFrom: a@x\r\nTo: b@x\r\nContent-Type: multipart/mixed; boundary=Z\r\n\r\n--Z\r\nContent-Type: text/plain\r\n\r\nhi\r\n--Z\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=\"big.bin\"\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!not-base64!!!!\r\n--Z--\r\n",
            b"Subject: one\r\nSubject: two\r\nTo: a@x\r\nTo: b@x\r\n\r\nbody",
            b"Subject: nct\r\nFrom: a@x\r\nTo: b@x\r\n\r\njust body",
        ]
        for raw in cases:
            info = self._parse_raw(raw)
            self.assertIsInstance(info["body_text"], str)
            self.assertIsInstance(info["attachments"], list)


class HtmlIsolationTests(unittest.TestCase):
    def test_iframe_sandbox_has_no_allowances(self):
        html = Path("web/index.html").read_text()
        self.assertIn('sandbox=""', html)
        self.assertNotIn("allow-scripts", html)
        self.assertNotIn("allow-same-origin", html)
        js = Path("web/app.js").read_text()
        self.assertIn(".srcdoc =", js)
        self.assertNotIn("innerHTML", js)


class ResultSpillTests(unittest.TestCase):
    def test_file_backed_pagination(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 30)
            job = create_job(str(mp), str(td / "o"), "overwrite")
            job["thread"].join(15)
            self.assertEqual(job["status"], "done")
            # RAM no longer holds the full graph
            self.assertEqual(job["items"], [])
            lines = read_lines(job)
            self.assertEqual(len(lines), 30)
            self.assertTrue(os.path.isfile(job["results_path"]))
            # status filter works from disk
            ok = [x for x in lines if x["status"] == "ok"]
            self.assertEqual(len(ok), 30)

    def test_core_streaming_without_ram(self):
        from mbox2eml import convert_detailed

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 10)
            seen = []
            rep = convert_detailed(str(mp), str(td / "o"), on_item=seen.append, store_items=False)
            self.assertEqual(rep["items"], [])
            self.assertEqual(len(seen), 10)
            self.assertEqual(rep["succeeded"], 10)

    def test_job_eviction_bounds_registry(self):
        import server as srv

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mp = td / "s.mbox"
            _make_mbox(mp, 1)
            old_max = srv.MAX_JOBS
            old_fin = srv.MAX_FINISHED_JOBS
            srv.MAX_JOBS = 5
            srv.MAX_FINISHED_JOBS = 2
            try:
                jobs = [create_job(str(mp), str(td / f"o{i}"), "overwrite") for i in range(8)]
                for j in jobs:
                    j["thread"].join(15)
                # trigger one more eviction pass via a new job
                extra = create_job(str(mp), str(td / "ox"), "overwrite")
                extra["thread"].join(15)
                self.assertLessEqual(len(srv._jobs), srv.MAX_JOBS)
            finally:
                srv.MAX_JOBS = old_max
                srv.MAX_FINISHED_JOBS = old_fin
                # cleanup registry noise for other tests
                with srv._jobs_lock:
                    for j in list(srv._jobs.values()):
                        if str(j["output_dir"]).startswith(str(td)):
                            srv._jobs.pop(j["job_id"], None)


if __name__ == "__main__":
    unittest.main()
