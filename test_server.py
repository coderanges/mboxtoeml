import json
import mailbox
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from pathlib import Path

import server
from server import (
    Handler,
    create_job,
    get_job,
    resolve_job_file,
    validate_collision,
    validate_job_filename,
    validate_mbox_path,
    validate_output_dir,
)


def _msg(subject="Hi", date="Mon, 01 Jan 2024 10:30:00 +0000", body="hello"):
    m = EmailMessage()
    m["Subject"] = subject
    m["Date"] = date
    m["From"] = "a@example.com"
    m["To"] = "b@example.com"
    m.set_content(body)
    return m


class ValidationTests(unittest.TestCase):
    def test_mbox_path_validation(self):
        with self.assertRaises(ValueError):
            validate_mbox_path("")
        with self.assertRaises(ValueError):
            validate_mbox_path("a\x00b")
        with self.assertRaises(FileNotFoundError):
            validate_mbox_path("/nonexistent-xyz-123.mbox")

    def test_output_dir_rejects_file(self):
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(ValueError):
                validate_output_dir(f.name)

    def test_collision_validation(self):
        self.assertEqual(validate_collision("rename"), "rename")
        with self.assertRaises(ValueError):
            validate_collision("bogus")

    def test_filename_validation_blocks_traversal(self):
        for bad in ["", "../evil.eml", "a/b.eml", "a\\b.eml", "/abs.eml", ".."]:
            with self.assertRaises(ValueError, msg=bad):
                validate_job_filename(bad)
        self.assertEqual(validate_job_filename("ok - 1.eml"), "ok - 1.eml")

    def test_resolve_containment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "out"
            out.mkdir()
            (out / "a.eml").write_bytes(b"x")
            job = {"output_dir": str(out)}
            self.assertTrue(resolve_job_file(job, "a.eml").endswith("a.eml"))
            with self.assertRaises(ValueError):
                resolve_job_file(job, "../evil.eml")
            with self.assertRaises(FileNotFoundError):
                resolve_job_file(job, "missing.eml")


class JobFlowTests(unittest.TestCase):
    def test_convert_job_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "s.mbox"
            out = temp_path / "out"
            mbox = mailbox.mbox(str(mbox_path))
            try:
                mbox.add(_msg("First", body="one"))
                mbox.add(_msg("Second", body="two"))
                mbox.flush()
            finally:
                mbox.close()
            job = create_job(str(mbox_path), str(out), "overwrite")
            job["thread"].join(timeout=10)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["succeeded"], 2)
            items = server._read_result_lines(job)
            self.assertEqual(len(items), 2)
            # eml fetch scoped to job
            target = resolve_job_file(job, items[0]["filename"])
            self.assertTrue(Path(target).is_file())

    def test_convert_job_bad_mbox_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileNotFoundError):
                create_job("/nope.mbox", str(Path(temp_dir) / "o"), "overwrite")


class LiveHttpSmokeTests(unittest.TestCase):
    def test_inspect_convert_results_eml_modify_over_http(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "s.mbox"
            out = temp_path / "out"
            mbox = mailbox.mbox(str(mbox_path))
            try:
                mbox.add(_msg("Hello HTTP", body="http body"))
                mbox.flush()
            finally:
                mbox.close()

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{port}"

                def post(path, obj):
                    req = urllib.request.Request(
                        base + path,
                        data=json.dumps(obj).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, json.loads(r.read().decode())

                def get(path):
                    with urllib.request.urlopen(base + path, timeout=10) as r:
                        return r.status, json.loads(r.read().decode())

                code, info = post("/api/inspect", {"mbox_path": str(mbox_path), "limit": 5})
                self.assertEqual(code, 200)
                self.assertEqual(info["total"], 1)

                code, payload = post(
                    "/api/convert",
                    {"mbox_path": str(mbox_path), "output_dir": str(out), "collision": "overwrite"},
                )
                self.assertEqual(code, 202)
                job_id = payload["job_id"]

                # poll for completion
                status = None
                for _ in range(50):
                    code, status = get(f"/api/jobs/{job_id}")
                    if status["done"]:
                        break
                    time.sleep(0.1)
                self.assertTrue(status["done"], status)
                self.assertEqual(status["succeeded"], 1)

                code, results = get(f"/api/jobs/{job_id}/results?offset=0&limit=10")
                self.assertEqual(len(results["items"]), 1)
                filename = results["items"][0]["filename"]

                code, eml = get(f"/api/eml?job_id={job_id}&filename={urllib.parse.quote(filename)}")
                self.assertIn("http body", eml["body_text"])

                code, mod = post(
                    "/api/eml/modify",
                    {"job_id": job_id, "filename": filename, "header": "X-Test", "value": "yes"},
                )
                self.assertEqual(code, 200)

                # traversal blocked
                try:
                    get(f"/api/eml?job_id={job_id}&filename=..%2Fevil.eml")
                    self.fail("expected HTTP error")
                except Exception as exc:
                    self.assertIn("400", str(exc))
            finally:
                httpd.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
