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


class NativePickerTests(unittest.TestCase):
    def _server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, t, f"http://127.0.0.1:{httpd.server_address[1]}"

    def _pick(self, base, initial_dir=""):
        req = urllib.request.Request(
            base + "/api/browse/native",
            data=json.dumps({"initial_dir": initial_dir}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_native_pick_returns_validated_path(self):
        import server as srv
        with tempfile.TemporaryDirectory() as td:
            orig = srv.choose_output_directory
            srv.choose_output_directory = lambda initial_dir=None: td
            httpd, t, base = self._server()
            try:
                code, data = self._pick(base, td)
                self.assertEqual(code, 200)
                self.assertEqual(data["path"], os.path.realpath(td))
            finally:
                srv.choose_output_directory = orig
                httpd.shutdown()
                t.join(timeout=5)

    def test_native_pick_cancel_is_silent_success(self):
        import server as srv
        orig = srv.choose_output_directory
        srv.choose_output_directory = lambda initial_dir=None: ""
        httpd, t, base = self._server()
        try:
            code, data = self._pick(base)
            self.assertEqual(code, 200)
            self.assertTrue(data.get("cancelled"))
            self.assertIsNone(data.get("path"))
        finally:
            srv.choose_output_directory = orig
            httpd.shutdown()
            t.join(timeout=5)

    def test_native_pick_busy_and_unavailable(self):
        import server as srv
        from native_picker import PickerBusy, PickerUnavailable
        httpd, t, base = self._server()
        try:
            orig = srv.choose_output_directory
            def busy(initial_dir=None):
                raise PickerBusy("A folder picker is already open")
            srv.choose_output_directory = busy
            code, data = self._pick(base)
            self.assertEqual(code, 409)
            self.assertIn("error", data)
            def gone(initial_dir=None):
                raise PickerUnavailable("Native picker unavailable (no display)")
            srv.choose_output_directory = gone
            code, data = self._pick(base)
            self.assertEqual(code, 503)
            self.assertIn("type the folder path instead", data["error"])
            srv.choose_output_directory = orig
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_old_browse_api_is_gone(self):
        httpd, t, base = self._server()
        try:
            try:
                urllib.request.urlopen(base + "/api/browse?path=", timeout=5)
                self.fail("expected HTTP error")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)
            req = urllib.request.Request(
                base + "/api/browse/mkdir", data=json.dumps({"path": "", "name": "x"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("expected HTTP error")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)
        finally:
            httpd.shutdown()
            t.join(timeout=5)

    def test_picked_path_flows_into_conversion(self):
        import server as srv
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "s.mbox"
            with open(mp, "wb") as f:
                f.write(_mbox_bytes(1))
            out = str(Path(td) / "picked-out")
            os.makedirs(out)
            orig = srv.choose_output_directory
            srv.choose_output_directory = lambda initial_dir=None: out
            httpd, t, base = self._server()
            try:
                code, data = self._pick(base, out)
                self.assertEqual(code, 200)
                req = urllib.request.Request(
                    base + "/api/convert",
                    data=json.dumps({"mbox_path": str(mp), "output_dir": data["path"],
                                     "collision": "overwrite"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    self.assertEqual(r.status, 202)
                self.assertTrue(os.path.isdir(out))
            finally:
                srv.choose_output_directory = orig
                httpd.shutdown()
                t.join(timeout=5)


class ValidateChosenDirectoryTests(unittest.TestCase):
    def test_accepts_real_directory(self):
        from native_picker import validate_chosen_directory
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(validate_chosen_directory(td), os.path.realpath(td))

    def test_rejects_bad_paths(self):
        from native_picker import validate_chosen_directory
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                validate_chosen_directory("")
            with self.assertRaises(ValueError):
                validate_chosen_directory("a\x00b")
            with self.assertRaises(FileNotFoundError):
                validate_chosen_directory(os.path.join(td, "vanished"))
            f = os.path.join(td, "file.txt")
            with open(f, "w") as handle:
                handle.write("x")
            with self.assertRaises(NotADirectoryError):
                validate_chosen_directory(f)

    def test_rejects_inaccessible_directory(self):
        from native_picker import validate_chosen_directory
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "locked")
            os.makedirs(target, mode=0)
            try:
                if os.geteuid() == 0:
                    self.skipTest("root bypasses permission bits")
                with self.assertRaises(PermissionError):
                    validate_chosen_directory(target)
            finally:
                os.chmod(target, 0o700)


class NativeFrontendContractTests(unittest.TestCase):
    def test_browse_button_uses_native_bridge(self):
        html = Path("web/index.html").read_text()
        js = Path("web/app.js").read_text()
        css = Path("web/styles.css").read_text()
        self.assertIn('id="browseOutBtn"', html)
        self.assertIn("/api/browse/native", js)
        self.assertIn("Choosing", js)  # busy state while the OS dialog is open
        # custom in-browser folder browser is gone
        self.assertNotIn("browseDialog", html)
        self.assertNotIn("browseDialog", js)
        self.assertNotIn("/api/browse?", js)
        self.assertNotIn("/api/browse/mkdir", js)
        self.assertNotIn("browse-list", html + css)


if __name__ == "__main__":
    unittest.main()
