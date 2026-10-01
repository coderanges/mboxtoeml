"""Pick File upload tests: staging, validation, and upload_id flows."""
import json
import mailbox
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from pathlib import Path

import server
from server import Handler


def _mbox_bytes(n=2):
    mbox_path = tempfile.mktemp(suffix=".mbox")
    mbox = mailbox.mbox(mbox_path)
    try:
        for i in range(n):
            m = EmailMessage()
            m["Subject"] = f"Upload {i}"
            m["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"
            m["From"] = "a@x"
            m["To"] = "b@x"
            m.set_content("body")
            mbox.add(m)
        mbox.flush()
    finally:
        mbox.close()
    try:
        with open(mbox_path, "rb") as f:
            return f.read()
    finally:
        for suffix in ("", ".lock"):
            try:
                os.unlink(mbox_path + suffix)
            except OSError:
                pass


class UploadTests(unittest.TestCase):
    def _server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, t, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _upload(self, base, body, filename="a.mbox", ctype="application/octet-stream", extra=None):
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "X-Filename": filename}
        headers.update(extra or {})
        req = urllib.request.Request(base + "/api/uploads", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _post(self, base, path, obj):
        req = urllib.request.Request(
            base + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_upload_inspect_convert_roundtrip(self):
        httpd, t, base = self._server()
        try:
            code, up = self._upload(base, _mbox_bytes(3), filename="pick.mbox")
            self.assertEqual(code, 201, up)
            self.assertRegex(up["upload_id"], r"^[0-9a-f]{16}$")
            code, info = self._post(base, "/api/inspect", {"upload_id": up["upload_id"], "limit": 5})
            self.assertEqual(code, 200)
            self.assertEqual(info["total"], 3)
            self.assertEqual(info["source_name"], "pick.mbox")
            with tempfile.TemporaryDirectory() as td:
                code, job = self._post(base, "/api/convert", {
                    "upload_id": up["upload_id"], "output_dir": str(Path(td) / "o"),
                    "collision": "rename"})
                self.assertEqual(code, 202)
                import time
                for _ in range(100):
                    with urllib.request.urlopen(f"{base}/api/jobs/{job['job_id']}", timeout=5) as r:
                        st = json.loads(r.read().decode())
                    if st["done"]:
                        break
                    time.sleep(0.1)
                self.assertEqual(st["status"], "done")
                self.assertEqual(st["succeeded"], 3)
                self.assertEqual(st["source_name"], "pick.mbox")
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_rejects_bad_uploads(self):
        httpd, t, base = self._server()
        try:
            good = _mbox_bytes(1)
            code, _ = self._upload(base, good, filename="evil.txt")
            self.assertEqual(code, 400)
            code, _ = self._upload(base, b"hello, definitely not mbox data....", filename="a.mbox")
            self.assertEqual(code, 400)
            code, _ = self._upload(base, b"", filename="a.mbox")
            self.assertEqual(code, 400)
            code, _ = self._upload(base, good, filename="a.mbox", ctype="text/plain")
            self.assertEqual(code, 400)
            code, _ = self._upload(base, good, filename="notmbox")
            self.assertEqual(code, 400)
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_oversized_upload_is_413(self):
        old = server._UPLOAD_MAX_BYTES
        server._UPLOAD_MAX_BYTES = 100
        try:
            httpd, t, base = self._server()
            try:
                code, data = self._upload(base, _mbox_bytes(2), filename="big.mbox")
                self.assertEqual(code, 413)
                self.assertIn("error", data)
            finally:
                httpd.shutdown()
                t.join(timeout=5)
        finally:
            server._UPLOAD_MAX_BYTES = old

    def test_invalid_and_expired_upload_ids(self):
        httpd, t, base = self._server()
        try:
            code, _ = self._post(base, "/api/inspect", {"upload_id": "../../etc/passwd"})
            self.assertEqual(code, 400)
            code, _ = self._post(base, "/api/inspect", {"upload_id": "zzzzzzzzzzzzzzzz"})
            self.assertEqual(code, 400)
            code, _ = self._post(base, "/api/inspect", {"upload_id": "a" * 16})
            self.assertEqual(code, 404)
            code, up = self._upload(base, _mbox_bytes(1), filename="gone.mbox")
            self.assertEqual(code, 201)
            # expire it by deleting the staged file
            with server._uploads_lock:
                staged = server._uploads[up["upload_id"]]["path"]
            os.unlink(staged)
            code, _ = self._post(base, "/api/inspect", {"upload_id": up["upload_id"]})
            self.assertEqual(code, 404)
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_upload_filename_cannot_escape_staging(self):
        httpd, t, base = self._server()
        try:
            code, up = self._upload(base, _mbox_bytes(1), filename="../../evil.mbox")
            self.assertEqual(code, 201, up)
            with server._uploads_lock:
                staged = server._uploads[up["upload_id"]]["path"]
            self.assertTrue(os.path.realpath(staged).startswith(
                os.path.realpath(server._UPLOAD_DIR) + os.sep))
            self.assertEqual(up["filename"], "evil.mbox")
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_path_flow_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "s.mbox"
            with open(mp, "wb") as f:
                f.write(_mbox_bytes(1))
            httpd, t, base = self._server()
            try:
                code, info = self._post(base, "/api/inspect", {"mbox_path": str(mp)})
                self.assertEqual(code, 200)
                self.assertEqual(info["total"], 1)
            finally:
                httpd.shutdown()
                t.join(timeout=5)


    def test_picker_is_primary_in_frontend(self):
        html = Path("web/index.html").read_text()
        js = Path("web/app.js").read_text()
        self.assertIn('id="pickBtn"', html)
        self.assertIn('id="fileInput"', html)
        self.assertIn('id="uploadBar"', html)
        self.assertIn("/api/uploads", js)
        # drop zone remains as a convenience alongside the picker
        self.assertIn('id="dropzone"', html)
        # manual server path kept as a fallback, not the primary control
        self.assertIn("enter a server path manually", html.lower())


class OutputFolderTests(unittest.TestCase):
    def _server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, t, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _mkdir(self, base, name):
        req = urllib.request.Request(
            base + "/api/output-folder", data=json.dumps({"name": name}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_output_folder_create_reuse_and_convert(self):
        import shutil
        import server as srv
        httpd, t, base = self._server()
        name = "mbox2eml-pick-test"
        target = os.path.join(srv._output_root(), name)
        try:
            code, data = self._mkdir(base, name)
            self.assertEqual(code, 200, data)
            self.assertEqual(data["path"], target)
            self.assertTrue(os.path.isdir(target))
            # existing folder is reused, not an error
            code, data = self._mkdir(base, name)
            self.assertEqual(code, 200)
            # picked folder flows straight into conversion
            mp = os.path.join(tempfile.mkdtemp(), "s.mbox")
            with open(mp, "wb") as f:
                f.write(_mbox_bytes(1))
            req = urllib.request.Request(
                base + "/api/convert",
                data=json.dumps({"mbox_path": mp, "output_dir": data["path"],
                                 "collision": "overwrite"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 202)
        finally:
            shutil.rmtree(target, ignore_errors=True)
            httpd.shutdown()
            t.join(timeout=5)

    def test_output_folder_rejects_bad_names(self):
        httpd, t, base = self._server()
        try:
            for bad in ("../x", "a/b", "a\\b", "", ".", "..", "a\x01b", "x" * 101):
                code, _ = self._mkdir(base, bad)
                self.assertEqual(code, 400, repr(bad))
            code, _ = self._mkdir(base, 123)
            self.assertEqual(code, 400)
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_old_picker_apis_are_gone(self):
        httpd, t, base = self._server()
        try:
            for method, path in (("GET", "/api/browse?path="),
                                 ("POST", "/api/browse/mkdir"),
                                 ("POST", "/api/browse/native")):
                req = urllib.request.Request(
                    base + path, data=b"{}" if method == "POST" else None,
                    headers={"Content-Type": "application/json"}, method=method)
                try:
                    urllib.request.urlopen(req, timeout=5)
                    self.fail(f"expected HTTP error for {method} {path}")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404, path)
        finally:
            httpd.shutdown()
            t.join(timeout=5)


class OutputPickerFrontendContractTests(unittest.TestCase):
    def test_output_uses_same_picker_interface_as_mbox(self):
        html = Path("web/index.html").read_text()
        js = Path("web/app.js").read_text()
        css = Path("web/styles.css").read_text()
        # MBOX control...
        self.assertIn('id="pickBtn"', html)
        self.assertIn('id="fileInput"', html)
        # ...and the output control mirrors it: button + directory-mode input.
        self.assertIn('id="pickOutBtn"', html)
        self.assertIn("Choose Output Folder", html)
        self.assertIn('webkitdirectory', html)
        self.assertIn("/api/output-folder", js)
        # no server-driven dialog anymore
        self.assertNotIn("browseDialog", html)
        self.assertNotIn("browseDialog", js)
        self.assertNotIn("/api/browse/native", js)
        self.assertNotIn("/api/browse/mkdir", js)
        self.assertNotIn("browse-list", html + css)
        self.assertNotIn("native_picker", open("server.py").read())


if __name__ == "__main__":
    unittest.main()
