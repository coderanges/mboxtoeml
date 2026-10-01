import argparse
import base64
import binascii
import codecs
import html as _html
import os
import quopri
import re
import sys
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

MAX_BODY_CHARS = 200_000
# EML files above this size are refused for preview (the stdlib parser must
# hold the whole file in RAM; refusing beats an OOM hang on a local utility).
MAX_EML_BYTES = 250 * 1024 * 1024
# Attachment/text payloads above this many encoded chars are sized/decoded
# incrementally so a huge part never gets fully materialized just for metadata
# or a 200KB preview. Small parts keep the exact legacy code path.
_BIG_PAYLOAD_CHARS = 2_000_000
_B64_CHUNK_CHARS = 4 * 256 * 1024  # multiple of 4
_QP_CHUNK_CHARS = 1024 * 1024
# Bodies are decoded only up to one char past the preview limit; anything
# longer sets the truncated flag without ever holding the full text.
_BODY_PREVIEW_LIMIT = MAX_BODY_CHARS + 1

# Tags that can never appear in a preview: code execution, nesting, or
# navigation/tracking primitives. Their content is dropped (script/style) or
# unwrapped (other tags fall through to text via handle_data).
_DROPPED_TAGS = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "applet", "form", "button", "input", "select", "textarea", "link",
    "meta", "base", "title",
})
# Void elements that never have children.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
# Attributes that load a resource (or navigate, for href-like names).
# The local name after any namespace prefix (e.g. xlink:href -> href) decides.
_RESOURCE_ATTRS = frozenset({"src", "srcset", "poster", "background", "data", "lowsrc", "href", "action"})
# Schemes that must never survive in any attribute value.
_DANGEROUS_SCHEMES = ("javascript:", "vbscript:", "file:", "about:", "data:text/html")
# data: URLs are only kept as small raster/vector images; everything else
# (html, scripts, huge blobs) is dropped. 1M chars ~= 750KB decoded.
_DATA_URL_MAX_CHARS = 1_000_000
_ALLOWED_DATA_PREFIXES = (
    "data:image/png", "data:image/jpeg", "data:image/gif",
    "data:image/webp", "data:image/bmp", "data:image/svg+xml",
)


def _normalize_url(value):
    """Unescape entities, strip C0 controls/whitespace (browsers ignore them
    inside schemes, e.g. ``java\\tscript:``), and lowercase for comparison."""
    try:
        text = _html.unescape(value or "")
    except Exception:
        text = value or ""
    text = re.sub(r"[\x00-\x20\x7f]+", "", text).lower()
    return text


def _is_dangerous_url(value):
    return _normalize_url(value).startswith(_DANGEROUS_SCHEMES)


def _is_allowed_data_url(value):
    norm = _normalize_url(value)
    if not norm.startswith("data:"):
        return False
    if len(value) > _DATA_URL_MAX_CHARS:
        return False
    return norm.startswith(_ALLOWED_DATA_PREFIXES)


def _is_safe_resource_url(value):
    """Allowlist for resource-loading attributes: relative paths, cid:,
    and small data:image/* only. Everything else (remote, file:, about:,
    executable data:) is blocked to prevent tracking and local access."""
    norm = _normalize_url(value)
    if not norm:
        return False
    if norm.startswith("cid:") or norm.startswith("#"):
        return True
    if norm.startswith("//"):
        return False  # protocol-relative remote reference
    if norm.startswith("data:"):
        return _is_allowed_data_url(value)
    # No scheme at all -> relative reference, safe.
    if ":" not in norm:
        return True
    return False


def _srcset_has_blocked(value):
    """Check every candidate in a srcset list; fail closed on any bad one."""
    for candidate in (value or "").split(","):
        url = candidate.strip().split()
        if not url:
            continue
        if _is_dangerous_url(url[0]) or not _is_safe_resource_url(url[0]):
            return True
    return False


class _PreviewSanitizer(HTMLParser):
    """Rebuild HTML for preview: drop executable tags, event handlers,
    embedded styles, remote resources, and dangerous URLs."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []
        self.blocked = 0
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in _DROPPED_TAGS or self._drop_depth:
            if tag not in _VOID_TAGS:
                self._drop_depth += 1
            self.blocked += 1
            return
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            if name.startswith("on") or name == "style":
                self.blocked += 1
                continue
            if isinstance(value, str):
                # Dangerous schemes are rejected in ANY attribute: browsers
                # decode entities and ignore C0 whitespace inside schemes, and
                # namespaced variants (xlink:href) must not slip through.
                if _is_dangerous_url(value):
                    self.blocked += 1
                    continue
                local = name.split(":")[-1]
                is_link_href = tag == "a" and local == "href"
                if local in _RESOURCE_ATTRS and not is_link_href:
                    if local == "srcset":
                        if _srcset_has_blocked(value):
                            self.blocked += 1
                            continue
                    elif not _is_safe_resource_url(value):
                        self.blocked += 1
                        continue
            if value is None:
                kept.append(name)
            else:
                kept.append(f'{name}="{_html.escape(value, quote=True)}"')
        suffix = " /" if tag in _VOID_TAGS else ""
        self.out.append(f"<{tag}{(' ' + ' '.join(kept)) if kept else ''}{suffix}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self._drop_depth:
            self._drop_depth -= 1
            return
        if tag in _DROPPED_TAGS or tag in _VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth:
            return
        self.out.append(_html.escape(data))

    def handle_entityref(self, name):
        if self._drop_depth:
            return
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        if self._drop_depth:
            return
        self.out.append(f"&#{name};")

    def result(self):
        return "".join(self.out)


def sanitize_html_for_preview(html_value):
    """Return (sanitized_html, blocked_count) safe for a sandboxed iframe.

    Blocks scripts, event handlers, embedded styles, executable tags, remote
    resource loads (tracking pixels), and dangerous URL schemes. Text content
    and harmless formatting are preserved.
    """
    if not html_value:
        return "", 0
    try:
        parser = _PreviewSanitizer()
        parser.feed(html_value)
        parser.close()
        return parser.result()[:MAX_BODY_CHARS], parser.blocked
    except Exception:
        return _html.escape(html_value)[:MAX_BODY_CHARS], 1


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())

    def text(self):
        return "\n".join(self.parts)


def html_to_text(html_value):
    """Best-effort HTML -> text fallback using stdlib only."""
    if not html_value:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(html_value)
        text = parser.text()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:MAX_BODY_CHARS]
    except Exception:
        return re.sub(r"<[^>]+>", " ", html_value)[:MAX_BODY_CHARS]


def _raw_payload(part):
    """Leaf payload string without triggering a full-size transient copy.

    On CPython 3.12 ``get_payload(decode=False)`` runs ``_has_surrogates``
    over the whole payload, transiently duplicating huge parts. Reading the
    long-standing ``_payload`` attribute directly avoids that copy; anything
    unexpected falls back to the public API.
    """
    try:
        payload = part._payload
        if isinstance(payload, str):
            return payload
    except AttributeError:
        pass
    try:
        return part.get_payload(decode=False)
    except Exception:
        return None


def _cte(part):
    try:
        return (part.get("Content-Transfer-Encoding") or "").lower().split(";")[0].strip()
    except Exception:
        return ""


def _b64_decoded_size(text):
    """Exact decoded size of base64 text with O(chunk) peak memory."""
    total = 0
    carry = ""
    n = len(text)
    i = 0
    while i < n:
        chunk = carry + re.sub(r"\s+", "", text[i:i + _B64_CHUNK_CHARS])
        i += _B64_CHUNK_CHARS
        if i < n:
            rem = len(chunk) % 4
            if rem:
                chunk, carry = chunk[:-rem], chunk[-rem:]
            else:
                carry = ""
        else:
            carry = ""
        if chunk:
            total += len(base64.b64decode(chunk, validate=False))
    if carry:
        total += len(base64.b64decode(carry, validate=False))
    return total


def _qp_split_head(buffer):
    """Split buffer so head never ends mid-escape (``=``, ``=X``) or mid
    soft-break (trailing CR that may precede LF). Returns (head, tail)."""
    cut = len(buffer)
    while cut > 0 and cut > len(buffer) - 4 and buffer[cut - 1] in "=\r\n":
        cut -= 1
    if cut <= 0:
        return "", buffer
    return buffer[:cut], buffer[cut:]


def _qp_decoded_size(text):
    """Exact decoded size of quoted-printable text with O(chunk) peak."""
    total = 0
    carry = ""
    n = len(text)
    i = 0
    while i < n:
        buffer = carry + text[i:i + _QP_CHUNK_CHARS]
        i += _QP_CHUNK_CHARS
        if i < n:
            head, carry = _qp_split_head(buffer)
        else:
            head, carry = buffer, ""
        if head:
            total += len(quopri.decodestring(head.encode("ascii", errors="ignore"), header=False))
    if carry:
        total += len(quopri.decodestring(carry.encode("ascii", errors="ignore"), header=False))
    return total


def _attachment_payload_size(part):
    """Decoded attachment size without materializing huge payloads.

    Small payloads use the exact legacy path. Large base64/QP payloads are
    measured with chunked decoders (exact for well-formed input, O(chunk)
    memory). Anything undecodable reports 0, matching legacy tolerance.
    """
    raw = _raw_payload(part)
    if raw is None or isinstance(raw, list):
        return 0
    if not isinstance(raw, str):
        try:
            return len(raw)
        except Exception:
            return 0
    if len(raw) <= _BIG_PAYLOAD_CHARS:
        try:
            payload = part.get_payload(decode=True)
            return len(payload) if isinstance(payload, (bytes, bytearray)) else 0
        except Exception:
            return 0
    try:
        cte = _cte(part)
        if cte == "base64":
            return _b64_decoded_size(raw)
        if cte in ("quoted-printable", "quotedprintable", "qp"):
            return _qp_decoded_size(raw)
        return len(raw.encode("ascii", errors="replace"))
    except (binascii.Error, ValueError):
        return 0
    except Exception:
        return 0


def _iter_decoded_bytes(raw, cte):
    """Yield decoded bytes for one part without holding the whole output."""
    if cte == "base64":
        text = re.sub(r"\s+", "", raw)
        step = _B64_CHUNK_CHARS
        carry = ""
        n = len(text)
        i = 0
        while i < n:
            chunk = carry + text[i:i + step]
            i += step
            if i < n:
                rem = len(chunk) % 4
                if rem:
                    chunk, carry = chunk[:-rem], chunk[-rem:]
                else:
                    carry = ""
            if chunk:
                yield base64.b64decode(chunk, validate=False)
        return
    if cte in ("quoted-printable", "quotedprintable", "qp"):
        carry = ""
        n = len(raw)
        i = 0
        while i < n:
            buffer = carry + raw[i:i + _QP_CHUNK_CHARS]
            i += _QP_CHUNK_CHARS
            if i < n:
                head, carry = _qp_split_head(buffer)
            else:
                head, carry = buffer, ""
            if head:
                yield quopri.decodestring(head.encode("ascii", errors="ignore"), header=False)
        if carry:
            yield quopri.decodestring(carry.encode("ascii", errors="ignore"), header=False)
        return
    head = raw[: (_BODY_PREVIEW_LIMIT + 1024) * 4]
    yield head.encode("ascii", errors="replace")


def _decode_text_preview(part, limit=_BODY_PREVIEW_LIMIT):
    """Decode a text part only until ``limit`` chars are available."""
    raw = _raw_payload(part)
    if not isinstance(raw, str):
        return _safe_get_content(part)
    if len(raw) <= _BIG_PAYLOAD_CHARS:
        return _safe_get_content(part)
    try:
        charset = part.get_content_charset() or "utf-8"
    except Exception:
        charset = "utf-8"
    try:
        decoder = codecs.getincrementaldecoder(charset)(errors="replace")
    except (LookupError, ValueError):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    out = []
    total = 0
    try:
        for chunk in _iter_decoded_bytes(raw, _cte(part)):
            try:
                text = decoder.decode(chunk, False)
            except Exception:
                text = chunk.decode("utf-8", errors="replace")
            if text:
                out.append(text)
                total += len(text)
                if total >= limit:
                    break
    except Exception:
        pass
    return "".join(out)[:limit]


def _safe_get_content(part):
    """Return decoded part content without raising on broken charsets."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                return content.decode(charset, errors="replace")
            except (LookupError, ValueError):
                return content.decode("utf-8", errors="replace")
        return str(content)
    except Exception:
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                raw = part.get_payload()
                return raw if isinstance(raw, str) else ""
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, ValueError):
                    return payload.decode("utf-8", errors="replace")
            return str(payload)
        except Exception:
            return ""


def _extract_bodies(message):
    """Return (body_text, body_html) preferring first plain part."""
    text_body = None
    html_body = None
    try:
        is_multi = message.is_multipart()
    except Exception:
        return None, None
    if is_multi:
        try:
            parts = list(message.walk())
        except Exception:
            return None, None
        for part in parts:
            try:
                if part.is_multipart():
                    continue
            except Exception:
                continue
            try:
                ctype = (part.get_content_type() or "").lower()
            except Exception:
                ctype = ""
            try:
                disp = (part.get_content_disposition() or "")
            except Exception:
                disp = ""
            if disp == "attachment":
                continue
            try:
                if ctype == "text/plain" and text_body is None:
                    text_body = _decode_text_preview(part)
                elif ctype == "text/html" and html_body is None:
                    html_body = _decode_text_preview(part)
            except Exception:
                continue
            if text_body and html_body:
                break
    else:
        try:
            ctype = (message.get_content_type() or "").lower()
        except Exception:
            ctype = ""
        try:
            if ctype == "text/plain":
                text_body = _decode_text_preview(message)
            elif ctype == "text/html":
                html_body = _decode_text_preview(message)
            elif ctype.startswith("text/"):
                text_body = _decode_text_preview(message)
        except Exception:
            pass
    if not text_body and html_body:
        try:
            derived = html_to_text(html_body)
        except Exception:
            derived = ""
        if derived:
            text_body = derived
    return text_body, html_body


def _extract_text_body(message):
    """Return the first readable text body, preferring plain text."""
    text_body, _ = _extract_bodies(message)
    return text_body


def _collect_attachments(message):
    attachments = []
    try:
        if not message.is_multipart():
            return attachments
    except Exception:
        return attachments
    try:
        parts = list(message.walk())
    except Exception:
        return attachments
    for part in parts:
        try:
            if part.is_multipart():
                continue
        except Exception:
            continue
        try:
            filename = part.get_filename()
        except Exception:
            filename = None
        try:
            disp = part.get_content_disposition()
        except Exception:
            disp = None
        try:
            ctype = part.get_content_type()
        except Exception:
            ctype = None
        is_attachment = disp == "attachment" or (filename and disp != "inline")
        is_inline_file = disp == "inline" and filename
        if not (is_attachment or is_inline_file):
            continue
        size = _attachment_payload_size(part)
        attachments.append(
            {
                "filename": filename or "unnamed",
                "content_type": ctype or "application/octet-stream",
                "size_bytes": size,
                "disposition": disp or "attachment",
            }
        )
    return attachments


def parse_eml(eml_file, max_body_chars=MAX_BODY_CHARS):
    """Parse an EML file into a JSON-safe dict for API/UI use."""
    try:
        if os.path.getsize(eml_file) > MAX_EML_BYTES:
            raise ValueError(
                f"EML file too large to preview (limit {MAX_EML_BYTES // (1024 * 1024)} MB)"
            )
    except OSError:
        pass
    with open(eml_file, "rb") as eml_data:
        msg = BytesParser(policy=policy.default).parse(eml_data)

    body_text, body_html = _extract_bodies(msg)
    truncated = False
    if body_text and len(body_text) > max_body_chars:
        body_text = body_text[:max_body_chars]
        truncated = True
    html_present = bool(body_html)
    if body_html and len(body_html) > max_body_chars:
        body_html = body_html[:max_body_chars]
        truncated = True

    defects = []
    try:
        defects = [str(d) for d in getattr(msg, "defects", [])]
    except Exception:
        defects = []

    try:
        body_html_sanitized, remote_blocked = sanitize_html_for_preview(body_html or "")
    except Exception:
        body_html_sanitized, remote_blocked = "", 0

    return {
        "subject": msg.get("Subject", "") or "",
        "from": msg.get("From", "") or "",
        "to": msg.get_all("To", []) or [],
        "cc": msg.get_all("Cc", []) or [],
        "bcc": msg.get_all("Bcc", []) or [],
        "reply_to": msg.get("Reply-To", "") or "",
        "date": msg.get("Date", "") or "",
        "message_id": msg.get("Message-ID", "") or "",
        "body_text": body_text or "",
        "body_html": body_html or "",
        "body_html_sanitized": body_html_sanitized,
        "remote_blocked": remote_blocked,
        "has_html": html_present,
        "has_body": bool(body_text),
        "attachments": _collect_attachments(msg),
        "truncated": truncated,
        "defects": defects,
    }


def read_eml(eml_file, out=None):
    out = out or sys.stdout
    # Parse the EML file
    info = parse_eml(eml_file)

    # Extract information from the parsed message
    subject = info["subject"] or "No Subject"
    sender = info["from"] or "No Sender"
    recipients = info["to"]
    date = info["date"] or "No Date"

    # Print email information
    print(f"Subject: {subject}", file=out)
    print(f"From: {sender}", file=out)
    print(f"To: {', '.join(recipients)}", file=out)
    print(f"Date: {date}", file=out)
    if info["cc"]:
        print(f"Cc: {', '.join(info['cc'])}", file=out)
    if info["attachments"]:
        print(f"Attachments: {len(info['attachments'])}", file=out)
        for att in info["attachments"]:
            print(f"  - {att['filename']} ({att['content_type']}, {att['size_bytes']} bytes)", file=out)
    elif info["has_html"] and not info["has_body"]:
        pass

    plain_text_body = info["body_text"]

    if plain_text_body:
        print("\nBody:", file=out)
        print(plain_text_body, file=out)
    else:
        print("\nNo plain text body found.", file=out)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Read and print an EML file")
    parser.add_argument("eml_file_path", help="Path to the EML file")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    read_eml(args.eml_file_path)
