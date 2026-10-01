import argparse
import datetime
import mailbox
import os
import re
import unicodedata
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

MAX_FILENAME_LENGTH = 120
FILENAME_FALLBACK = "message"
COLLISION_POLICIES = ("overwrite", "skip", "rename")


def _decode_header_value(value):
    """Decode RFC 2047 encoded headers into a readable string."""
    if not value:
        return ""

    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, ValueError, LookupError, AttributeError):
        try:
            return str(value)
        except Exception:
            return ""


def _sanitize_filename(name):
    """Remove characters unsafe for filenames."""
    if not name:
        return ""
    safe = _decode_header_value(name)
    safe = unicodedata.normalize("NFKD", safe).encode("ascii", "ignore").decode("ascii")
    safe = "".join(character if character.isprintable() else " " for character in safe)
    safe = re.sub(r'[\\/:*?"<>|]', '_', safe)
    safe = re.sub(r'\s+', ' ', safe).strip()
    safe = safe.strip(" ._-")
    return safe


def _format_message_date(value):
    """Parse a message date into a stable file-friendly format."""
    if not value:
        return ""

    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError, OverflowError, AttributeError):
        return _sanitize_filename(value)


def _make_filename(index, message, max_length=MAX_FILENAME_LENGTH):
    """Build a human-readable .eml filename from message metadata."""
    subject = _sanitize_filename(message.get("Subject", ""))
    date = _format_message_date(message.get("Date", ""))
    suffix = f" - {index}.eml"
    available_length = max(len(FILENAME_FALLBACK), max_length - len(suffix))

    if subject and date:
        date_segment = f" - {date}"
        subject_length = max(0, available_length - len(date_segment))
        trimmed_subject = subject[:subject_length].rstrip(" ._-")
        if trimmed_subject:
            return f"{trimmed_subject}{date_segment}{suffix}"
        return f"{date[:available_length].rstrip(' ._-') or FILENAME_FALLBACK}{suffix}"

    prefix = subject or date or FILENAME_FALLBACK
    prefix = prefix[:available_length].rstrip(" ._-") or FILENAME_FALLBACK
    return f"{prefix}{suffix}"


def _safe_subject(message):
    """Best-effort decoded subject for error reports; never raises."""
    try:
        return _decode_header_value(message.get("Subject", ""))
    except Exception:
        return ""


def _safe_addr(message, header):
    """Best-effort decoded address header for result metadata; never raises."""
    try:
        return _decode_header_value(message.get(header, ""))
    except Exception:
        return ""


def parse_date_to_timestamp(value):
    """Parse an RFC email date to a UTC epoch float, or None if missing/bad.

    Timezone-aware dates are normalized to their true instant; naive dates
    are assumed to be UTC. Never raises; never invents a date.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError, AttributeError):
        return None
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _write_rename_atomic(output_dir, filename, data):
    """Write with rename collision policy using O_EXCL to close TOCTOU races.

    Returns the filename actually used. Raises OSError after too many tries.
    """
    if not os.path.exists(os.path.join(output_dir, filename)):
        try:
            _write_bytes_atomic(os.path.join(output_dir, filename), data, exclusive=True)
            return filename
        except FileExistsError:
            pass
    stem, ext = os.path.splitext(filename)
    for n in range(2, 1000):
        extra = f" ({n})"
        allowed_stem = MAX_FILENAME_LENGTH - len(extra) - len(ext)
        trimmed = stem[: max(0, allowed_stem)].rstrip(" ._-")
        candidate = f"{trimmed}{extra}{ext}" if trimmed else f"message{extra}{ext}"
        if len(candidate) > MAX_FILENAME_LENGTH:
            candidate = candidate[:MAX_FILENAME_LENGTH]
        try:
            _write_bytes_atomic(os.path.join(output_dir, candidate), data, exclusive=True)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"Too many collisions for {filename!r}")


def _write_bytes_atomic(path, data, exclusive=False):
    """Write bytes, optionally failing if the path already exists (O_EXCL).

    Returns True on success. Raises FileExistsError if exclusive and present.
    """
    if not exclusive:
        with open(path, "wb") as handle:
            handle.write(data)
        return True
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        raise
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


def suggested_output_dir(mbox_path):
    """Default output directory next to the mbox file."""
    base = os.path.dirname(os.path.abspath(os.path.expanduser(mbox_path)))
    return os.path.join(base, "output")


def inspect_mbox(mbox_path, limit=50):
    """Bounded mailbox preview for UI; never loads full bodies into one response.

    Returns dict with size_bytes, total, and up to `limit` message summaries
    in mailbox order.
    """
    if not mbox_path or "\x00" in mbox_path:
        raise ValueError("Invalid mbox path")
    if not os.path.isfile(mbox_path):
        raise FileNotFoundError(f"Mbox file not found: {mbox_path}")
    limit = max(0, min(int(limit), 200))

    size_bytes = os.path.getsize(mbox_path)
    mbox = mailbox.mbox(mbox_path)
    try:
        total = len(mbox)
        messages = []
        for i, message in enumerate(mbox, start=1):
            if len(messages) >= limit:
                break
            try:
                messages.append(
                    {
                        "index": i,
                        "subject": _decode_header_value(message.get("Subject", "")),
                        "from": _decode_header_value(message.get("From", "")),
                        "to": _decode_header_value(message.get("To", "")),
                        "date": _decode_header_value(message.get("Date", "")),
                    }
                )
            except Exception as exc:  # per-message preview must not abort inspect
                messages.append({"index": i, "subject": "", "from": "", "to": "", "date": "", "error": str(exc)})
        return {
            "mbox_path": os.path.abspath(mbox_path),
            "size_bytes": size_bytes,
            "total": total,
            "preview_count": len(messages),
            "messages": messages,
            "suggested_output": suggested_output_dir(mbox_path),
        }
    finally:
        mbox.close()


def convert_detailed(
    mbox_path,
    output_dir,
    progress_callback=None,
    error_callback=None,
    collision="overwrite",
    should_stop=None,
    on_item=None,
    store_items=True,
):
    """Convert with per-message isolation and a full report.

    Fatal errors (missing mbox, bad collision, mkdir failure, mailbox open
    or count failure) still raise. Per-message failures are recorded in
    ``errors`` and processing continues.

    ``on_item`` is called per message with the item dict
    {index,status,subject,from,to,date,date_ts,filename,error} as soon as it
    is known, allowing callers to stream results to disk instead of holding
    them all in RAM.
    When ``store_items`` is False the returned ``items`` list is empty and
    the caller must rely on ``on_item``.

    ``date`` is the raw Date header ("" when missing); ``date_ts`` is the
    UTC epoch float or None when the header is missing or unparseable.

    Report: {created, errors[{index,subject,error}], skipped[{index,subject,filename}],
             items[{index,status,subject,from,to,date,date_ts,filename,error}],
             total, succeeded, failed, skipped_count, cancelled}
    """
    if collision not in COLLISION_POLICIES:
        raise ValueError(f"Unknown collision policy: {collision!r}")
    if not os.path.isfile(mbox_path):
        raise FileNotFoundError(f"Mbox file not found: {mbox_path}")

    # Create output directory if it doesn't exist (fatal if this fails)
    os.makedirs(output_dir, exist_ok=True)

    mbox = mailbox.mbox(mbox_path)
    try:
        total = len(mbox)
        created = []
        errors = []
        skipped = []
        items = []
        cancelled = False

        for i, message in enumerate(mbox, start=1):
            if should_stop is not None:
                try:
                    if should_stop():
                        cancelled = True
                        break
                except Exception:
                    pass

            def _emit(item):
                if store_items:
                    items.append(item)
                if on_item is not None:
                    try:
                        on_item(item)
                    except Exception:
                        pass

            try:
                subject_hint = _safe_subject(message)
                from_hint = _safe_addr(message, "From")
                to_hint = _safe_addr(message, "To")
                date_raw = _decode_header_value(message.get("Date", ""))
                date_ts = parse_date_to_timestamp(message.get("Date", ""))
                data = message.as_bytes()
                eml_filename = _make_filename(i, message)
                if collision == "overwrite":
                    eml_path = os.path.join(output_dir, eml_filename)
                    _write_bytes_atomic(eml_path, data, exclusive=False)
                elif collision == "skip":
                    if os.path.exists(os.path.join(output_dir, eml_filename)):
                        skipped.append(
                            {"index": i, "subject": subject_hint, "filename": eml_filename}
                        )
                        _emit(
                            {"index": i, "status": "skipped", "subject": subject_hint,
                             "from": from_hint, "to": to_hint,
                             "date": date_raw, "date_ts": date_ts,
                             "filename": eml_filename, "error": ""}
                        )
                        continue
                    eml_path = os.path.join(output_dir, eml_filename)
                    _write_bytes_atomic(eml_path, data, exclusive=False)
                elif collision == "rename":
                    eml_filename = _write_rename_atomic(output_dir, eml_filename, data)
                    eml_path = os.path.join(output_dir, eml_filename)
                else:  # pragma: no cover - validated above
                    raise ValueError(f"Unknown collision policy: {collision!r}")

                created.append(eml_path)
                _emit(
                    {"index": i, "status": "ok", "subject": subject_hint,
                     "from": from_hint, "to": to_hint,
                     "date": date_raw, "date_ts": date_ts,
                     "filename": eml_filename, "error": ""}
                )

                if progress_callback:
                    progress_callback(i, eml_filename, total)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                try:
                    err_date_raw = _decode_header_value(message.get("Date", ""))
                    err_date_ts = parse_date_to_timestamp(message.get("Date", ""))
                    err_from = _safe_addr(message, "From")
                    err_to = _safe_addr(message, "To")
                    err_subject = _safe_subject(message)
                except Exception:
                    err_date_raw, err_date_ts, err_from, err_to, err_subject = "", None, "", "", ""
                errors.append({"index": i, "subject": err_subject, "error": err})
                _emit(
                    {"index": i, "status": "error", "subject": err_subject,
                     "from": err_from, "to": err_to,
                     "date": err_date_raw, "date_ts": err_date_ts,
                     "filename": "", "error": err}
                )
                if error_callback:
                    try:
                        error_callback(i, err, total)
                    except Exception:
                        pass
                continue

        return {
            "created": created,
            "errors": errors,
            "skipped": skipped,
            "items": items,
            "total": total,
            "succeeded": len(created),
            "failed": len(errors),
            "skipped_count": len(skipped),
            "cancelled": cancelled,
        }
    finally:
        mbox.close()


def convert(mbox_path, output_dir, progress_callback=None, error_callback=None, collision="overwrite", should_stop=None):
    """
    Convert an mbox file into individual .eml files.

    Args:
        mbox_path: Path to the input .mbox file.
        output_dir: Directory where .eml files will be written.
        progress_callback: Optional callable(index, filename, total) for progress updates.
        error_callback: Optional callable(index, error_message, total) for per-message failures.
        collision: One of "overwrite" (default), "skip", "rename".
        should_stop: Optional callable() -> bool; when True, stop after current message.

    Returns:
        A list of paths to the created .eml files (successes only).

    Raises:
        FileNotFoundError: If the mbox file does not exist.
        PermissionError: If the output directory cannot be created or written to.
        ValueError: For an unknown collision policy.
        Exception: For other fatal mailbox-related errors.
    """
    report = convert_detailed(
        mbox_path,
        output_dir,
        progress_callback=progress_callback,
        error_callback=error_callback,
        collision=collision,
        should_stop=should_stop,
    )
    return report["created"]


def mbox_to_eml(mbox_file, output_dir, collision="overwrite"):
    """CLI-friendly wrapper around convert()."""
    failures = []

    def on_progress(index, filename, total):
        print(f"[{index}/{total}] Converted to {filename}")

    def on_error(index, error, total):
        failures.append((index, error))
        print(f"[{index}/{total}] FAILED: {error}")

    try:
        report = convert_detailed(
            mbox_file, output_dir, progress_callback=on_progress, error_callback=on_error, collision=collision
        )
        print(f"\nDone. {report['succeeded']}/{report['total']} written to '{output_dir}'.", end="")
        if report["failed"]:
            print(f" {report['failed']} failed.", end="")
        if report["skipped_count"]:
            print(f" {report['skipped_count']} skipped.", end="")
        print()
        if report["cancelled"]:
            print("Conversion was cancelled.")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1)
    except PermissionError as e:
        print(f"Error: permission denied – {e}")
        raise SystemExit(1)
    except ValueError as e:
        print(f"Error: {e}")
        raise SystemExit(2)
    except Exception as e:
        print(f"Error during conversion: {e}")
        raise SystemExit(1)
    if report["total"] > 0 and report["succeeded"] == 0 and report["failed"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert mbox to eml')
    parser.add_argument('--file', '-f', type=str, required=True, help='Path to the input mbox file, eg. 1.mbox')
    parser.add_argument('--output_dir', '-o', type=str, required=True, help='Path to the output directory, eg. output')
    parser.add_argument(
        '--collision',
        type=str,
        default="overwrite",
        choices=list(COLLISION_POLICIES),
        help='What to do when an output file exists (default: overwrite)',
    )

    args = parser.parse_args()
    mbox_to_eml(args.file, args.output_dir, collision=args.collision)
