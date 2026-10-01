"""Local MBOX->EML application server (stdlib only).

Serves the vanilla JS frontend from ./web and a small JSON API.
No database, no web framework. Run:  python3 server.py [--host 127.0.0.1 --port 8000]
"""
import argparse
import datetime
import json
import os
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mbox2eml import COLLISION_POLICIES, convert_detailed, inspect_mbox
from modify_eml import read_and_modify_eml, validate_header_name, validate_header_value
from read_eml import parse_eml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
MAX_JSON_BYTES = 1_000_000
MAX_JOBS = 100
MAX_FINISHED_JOBS = 50

_jobs = {}
_jobs_lock = threading.Lock()

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}
STATIC_FILES = {"index.html", "app.js", "styles.css"}


# ---------- validation helpers ----------

def _bad(message):
    return ValueError(message)


def validate_mbox_path(raw):
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise _bad("mbox_path must be a non-empty string")
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Mbox file not found: {path}")
    return path


def validate_output_dir(raw, create=False):
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise _bad("output_dir must be a non-empty string")
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if os.path.isfile(path):
        raise _bad(f"Output path is an existing file: {path}")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def validate_collision(raw):
    value = (raw or "overwrite")
    if value not in COLLISION_POLICIES:
        raise _bad(f"Unknown collision policy: {value!r}")
    return value


def _parse_iso_date(raw, name):
    """Parse YYYY-MM-DD to a date, or None when absent. Raises 400 on garbage."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise _bad(f"{name} must be YYYY-MM-DD")
    try:
        return datetime.date.fromisoformat(raw.strip())
    except ValueError:
        raise _bad(f"{name} must be YYYY-MM-DD")


def _date_bounds(date_from, date_to):
    """Inclusive UTC day boundaries as epoch ranges.

    date_from 00:00:00 UTC <= instant < (date_to + 1 day) 00:00:00 UTC.
    Timezone-aware message dates are compared by their true UTC instant;
    naive ones were normalized to UTC at extraction. Missing/malformed
    dates (date_ts None) never match an active range.
    """
    start = None
    end_exclusive = None
    if date_from is not None:
        start = datetime.datetime(date_from.year, date_from.month, date_from.day,
                                  tzinfo=datetime.timezone.utc).timestamp()
    if date_to is not None:
        end_exclusive = (
            datetime.datetime(date_to.year, date_to.month, date_to.day,
                              tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)
        ).timestamp()
    return start, end_exclusive


def validate_job_filename(raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise _bad("Invalid filename")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise _bad("Invalid filename (control characters)")
    if "/" in raw or "\\" in raw or raw != os.path.basename(raw):
        raise _bad("Invalid filename")
    if raw in (".", "..") or ".." in raw.split(os.sep):
        raise _bad("Invalid filename")
    if len(raw) > 255:
        raise _bad("Filename too long")
    return raw


def resolve_job_file(job, filename):
    filename = validate_job_filename(filename)
    output_dir = os.path.realpath(job["output_dir"])
    target = os.path.realpath(os.path.join(output_dir, filename))
    if os.path.commonpath([output_dir, target]) != output_dir:
        raise _bad("Filename escapes output directory")
    if not os.path.isfile(target):
        raise FileNotFoundError(f"EML not found: {filename}")
    return target


def job_status_payload(job):
    with job["lock"]:
        elapsed = (job["finished_at"] or time.time()) - job["started_at"]
        total = job["total"]
        processed = job["processed"]
        progress = (processed / total) if total else 0.0
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "mbox_path": job["mbox_path"],
            "output_dir": job["output_dir"],
            "collision": job["collision"],
            "total": total,
            "processed": processed,
            "succeeded": job["succeeded"],
            "failed": job["failed"],
            "skipped": job["skipped"],
            "progress": progress,
            "percentage": round(progress * 100, 1),
            "elapsed_secs": round(elapsed, 1),
            "current_filename": job["current_filename"],
            "current_index": job["current_index"],
            "done": job["status"] in ("done", "error", "cancelled"),
            "cancelled": job["status"] == "cancelled",
            "error": job["fatal_error"],
        }


# ---------- job execution ----------

def _append_result_line(job, item):
    """Append one result item to the job's JSONL spill file (best effort)."""
    path = job.get("results_path")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item) + "\n")
    except OSError:
        pass


def _read_result_lines(job):
    """Read spilled result lines, tolerating a concurrently appended file."""
    path = job.get("results_path")
    if not path or not os.path.isfile(path):
        with job["lock"]:
            return list(job.get("items") or [])
    out = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # tolerate torn trailing write
    except OSError:
        with job["lock"]:
            return list(job.get("items") or [])
    return out


def _evict_jobs_locked():
    """Bound _jobs lifetime: drop oldest finished jobs beyond MAX_FINISHED_JOBS."""
    if len(_jobs) <= MAX_JOBS:
        return
    finished = [j for j in _jobs.values() if j["status"] in ("done", "error", "cancelled")]
    finished.sort(key=lambda j: j.get("finished_at") or 0)
    while len(_jobs) > MAX_JOBS and len(finished) > MAX_FINISHED_JOBS:
        oldest = finished.pop(0)
        _jobs.pop(oldest["job_id"], None)


def _run_conversion_job(job):
    def on_progress(index, filename, total):
        with job["lock"]:
            job["total"] = total
            job["processed"] = max(job["processed"], index)
            job["succeeded"] += 1
            job["current_filename"] = filename
            job["current_index"] = index

    def on_error(index, error, total):
        with job["lock"]:
            job["total"] = total
            job["processed"] = max(job["processed"], index)
            job["failed"] += 1
            job["current_index"] = index

    def should_stop():
        return job["cancel_event"].is_set()

    try:
        report = convert_detailed(
            job["mbox_path"],
            job["output_dir"],
            progress_callback=on_progress,
            error_callback=on_error,
            collision=job["collision"],
            should_stop=should_stop,
            on_item=lambda item: _append_result_line(job, item),
            store_items=False,
        )
        with job["lock"]:
            job["total"] = report["total"]
            job["succeeded"] = report["succeeded"]
            job["failed"] = report["failed"]
            job["skipped"] = report["skipped_count"]
            job["processed"] = report["total"] if not report["cancelled"] else max(
                job["processed"], report["succeeded"] + report["failed"] + report["skipped_count"]
            )
            # items live in the JSONL spill file, not in RAM
            job["finished_at"] = time.time()
            # A late cancel racing a fully completed run must not rewrite done->cancelled.
            if report["cancelled"]:
                job["status"] = "cancelled"
            else:
                job["status"] = "done"
                # fix processed for skip-only runs (skips don't fire progress callbacks)
                accounted = report["succeeded"] + report["failed"] + report["skipped_count"]
                if accounted == report["total"]:
                    job["processed"] = report["total"]
    except Exception as exc:
        with job["lock"]:
            job["status"] = "error"
            job["fatal_error"] = f"{type(exc).__name__}: {exc}"
            job["finished_at"] = time.time()


def create_job(mbox_path, output_dir, collision):
    mbox_path = validate_mbox_path(mbox_path)
    output_dir = validate_output_dir(output_dir, create=True)
    collision = validate_collision(collision)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "running",
        "mbox_path": mbox_path,
        "output_dir": output_dir,
        "collision": collision,
        "total": 0,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "current_filename": "",
        "current_index": 0,
        "started_at": time.time(),
        "finished_at": None,
        "fatal_error": "",
        "items": [],
        "results_path": os.path.join(output_dir, f".mbox2eml-results-{job_id}.jsonl"),
        "lock": threading.Lock(),
        "cancel_event": threading.Event(),
        "thread": None,
    }
    # Start with an empty spill file so readers never race file creation.
    try:
        with open(job["results_path"], "w", encoding="utf-8"):
            pass
    except OSError:
        pass
    thread = threading.Thread(target=_run_conversion_job, args=(job,), daemon=True)
    job["thread"] = thread
    with _jobs_lock:
        _jobs[job_id] = job
        _evict_jobs_locked()
    thread.start()
    return job


def get_job(job_id):
    if not isinstance(job_id, str) or not job_id:
        return None
    with _jobs_lock:
        return _jobs.get(job_id)


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "mbox2eml/1.0"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code, body, ctype, filename=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            safe = filename.replace('"', "_")
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_JSON_BYTES:
            raise _bad("Request body too large")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise _bad("Invalid JSON body")

    def _serve_static(self, name):
        if name not in STATIC_FILES:
            self._send_json(404, {"error": "Not found"})
            return
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            self._send_json(404, {"error": "Frontend file missing"})
            return
        _, ext = os.path.splitext(name)
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----- routing -----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path in ("/app.js", "/styles.css"):
                return self._serve_static(path.lstrip("/"))
            if path == "/api/health":
                return self._send_json(200, {"ok": True})
            if path.startswith("/api/jobs/") and path.endswith("/events"):
                parts = path.split("/")
                # /api/jobs/<id>/events
                if len(parts) != 5:
                    return self._send_json(404, {"error": "Not found"})
                return self._handle_job_events(parts[3])
            if path.startswith("/api/jobs/") and path.endswith("/results"):
                parts = path.split("/")
                if len(parts) != 5:
                    return self._send_json(404, {"error": "Not found"})
                return self._handle_job_results(parts[3], query)
            if path.startswith("/api/jobs/"):
                parts = path.split("/")
                if len(parts) != 4 or not parts[3]:
                    return self._send_json(404, {"error": "Not found"})
                return self._handle_job_status(parts[3])
            if path == "/api/eml":
                return self._handle_eml_get(query)
            if path == "/api/eml/download":
                return self._handle_eml_download(query)
            return self._send_json(404, {"error": "Not found"})
        except (ValueError, TypeError, AttributeError, FileNotFoundError, OSError) as exc:
            code = 404 if isinstance(exc, FileNotFoundError) else 400
            return self._send_json(code, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            return self._send_json(500, {"error": "Internal server error"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/inspect":
                return self._handle_inspect()
            if path == "/api/convert":
                return self._handle_convert()
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                parts = path.split("/")
                if len(parts) != 5:
                    return self._send_json(404, {"error": "Not found"})
                return self._handle_cancel(parts[3])
            if path == "/api/eml/modify":
                return self._handle_eml_modify()
            return self._send_json(404, {"error": "Not found"})
        except (ValueError, TypeError, AttributeError, FileNotFoundError, OSError) as exc:
            code = 404 if isinstance(exc, FileNotFoundError) else 400
            return self._send_json(code, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            return self._send_json(500, {"error": "Internal server error"})

    # ----- handlers -----
    def _handle_inspect(self):
        data = self._read_json()
        raw_path = data.get("mbox_path", "")
        if not isinstance(raw_path, str):
            raise _bad("mbox_path must be a string")
        mbox_path = raw_path.strip()
        limit = data.get("limit", 50)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise _bad("limit must be an integer")
        info = inspect_mbox(validate_mbox_path(mbox_path), limit=limit)
        return self._send_json(200, info)

    def _handle_convert(self):
        data = self._read_json()
        job = create_job(data.get("mbox_path", ""), data.get("output_dir", ""), data.get("collision", "overwrite"))
        return self._send_json(202, {"job_id": job["job_id"]})

    def _handle_job_status(self, job_id):
        job = get_job(job_id)
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        return self._send_json(200, job_status_payload(job))

    def _handle_job_results(self, job_id, query):
        job = get_job(job_id)
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        try:
            offset = int((query.get("offset") or ["0"])[0])
            limit = int((query.get("limit") or ["100"])[0])
        except (ValueError, TypeError):
            raise _bad("offset/limit must be integers")
        offset = max(0, offset)
        limit = max(1, min(limit, 500))
        status_filter = (query.get("status") or ["all"])[0]
        search = ((query.get("q") or [""])[0] or "").strip().lower()
        date_from = _parse_iso_date((query.get("date_from") or [""])[0], "date_from")
        date_to = _parse_iso_date((query.get("date_to") or [""])[0], "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise _bad("date_from must not be after date_to")
        start, end_exclusive = _date_bounds(date_from, date_to)
        items = _read_result_lines(job)
        if status_filter in ("ok", "error", "skipped"):
            items = [it for it in items if it.get("status") == status_filter]
        if search:
            items = [
                it for it in items
                if search in str(it.get("subject", "")).lower()
                or search in str(it.get("from", "")).lower()
                or search in str(it.get("to", "")).lower()
                or search in str(it.get("filename", "")).lower()
            ]
        if start is not None or end_exclusive is not None:
            kept = []
            for it in items:
                ts = it.get("date_ts")
                if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                    continue  # missing/malformed dates never match a range
                if start is not None and ts < start:
                    continue
                if end_exclusive is not None and ts >= end_exclusive:
                    continue
                kept.append(it)
            items = kept
        total = len(items)
        page = items[offset: offset + limit]
        payload = job_status_payload(job)
        payload.update({"results_total": total, "offset": offset, "limit": limit, "items": page})
        return self._send_json(200, payload)

    def _handle_job_events(self, job_id):
        job = get_job(job_id)
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = job_status_payload(job)
                chunk = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                if payload["done"]:
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_cancel(self, job_id):
        job = get_job(job_id)
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        job["cancel_event"].set()
        return self._send_json(200, job_status_payload(job))

    def _handle_eml_get(self, query):
        job_id = (query.get("job_id") or [""])[0]
        filename = (query.get("filename") or [""])[0]
        job = get_job(job_id)
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        target = resolve_job_file(job, filename)
        info = parse_eml(target)
        info["filename"] = os.path.basename(target)
        return self._send_json(200, info)

    def _handle_eml_download(self, query):
        job_id = (query.get("job_id") or [""])[0]
        filename = (query.get("filename") or [""])[0]
        job = get_job(job_id)
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        target = resolve_job_file(job, filename)
        with open(target, "rb") as handle:
            body = handle.read()
        return self._send_bytes(200, body, "message/rfc822", filename=os.path.basename(target))

    def _handle_eml_modify(self):
        data = self._read_json()
        job = get_job(data.get("job_id", ""))
        if job is None:
            return self._send_json(404, {"error": "Job not found"})
        filename = validate_job_filename((data.get("filename") or ""))
        header = data.get("header", "")
        value = data.get("value", "")
        if not isinstance(header, str) or not isinstance(value, str):
            raise _bad("header and value must be strings")
        validate_header_name(header)
        validate_header_value(value)
        target = resolve_job_file(job, filename)
        out_name = data.get("output_filename") or filename
        if not isinstance(out_name, str):
            raise _bad("output_filename must be a string")
        out_name = validate_job_filename(out_name)
        output_dir = os.path.realpath(job["output_dir"])
        out_target = os.path.realpath(os.path.join(output_dir, out_name))
        if os.path.commonpath([output_dir, out_target]) != output_dir:
            raise _bad("Output filename escapes output directory")
        read_and_modify_eml(target, out_target, header_name=header, header_value=value)
        return self._send_json(200, {"filename": out_name, "header": header, "path": out_target})

    # Consistent JSON errors for unsupported methods (instead of default HTML 501).
    def do_PUT(self):
        self._send_json(405, {"error": "Method not allowed"})

    def do_DELETE(self):
        self._send_json(405, {"error": "Method not allowed"})

    def do_PATCH(self):
        self._send_json(405, {"error": "Method not allowed"})

    def do_HEAD(self):
        self._send_json(405, {"error": "Method not allowed"})

    def do_OPTIONS(self):
        self._send_json(405, {"error": "Method not allowed"})


def run(host="127.0.0.1", port=8000):
    if not os.path.isdir(WEB_DIR):
        raise SystemExit(f"Frontend directory missing: {WEB_DIR}")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"mbox2eml app at http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the local mbox2eml web app (stdlib server)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    run(host=args.host, port=args.port)
