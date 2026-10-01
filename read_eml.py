import argparse
import html as _html
import re
import sys
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_BODY_CHARS = 200_000

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
_REMOTE_ATTRS = frozenset({"src", "srcset", "poster", "background", "data", "lowsrc"})
_URL_ATTRS = frozenset({"href", "src", "srcset", "poster", "background", "data", "action", "lowsrc"})
_DANGEROUS_SCHEMES = ("javascript:", "vbscript:", "data:text/html")


def _is_remote_url(value):
    text = (value or "").strip().lower()
    if text.startswith(("http://", "https://", "ftp://", "//")):
        return True
    return False


def _is_dangerous_url(value):
    text = (value or "").strip().lower().lstrip("\x00 ")
    return text.startswith(_DANGEROUS_SCHEMES)


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
            if name in _URL_ATTRS and isinstance(value, str):
                if _is_dangerous_url(value):
                    self.blocked += 1
                    continue
                if name in _REMOTE_ATTRS and _is_remote_url(value):
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
                    text_body = _safe_get_content(part)
                elif ctype == "text/html" and html_body is None:
                    html_body = _safe_get_content(part)
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
                text_body = _safe_get_content(message)
            elif ctype == "text/html":
                html_body = _safe_get_content(message)
            elif ctype.startswith("text/"):
                text_body = _safe_get_content(message)
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
        try:
            payload = part.get_payload(decode=True)
            size = len(payload) if isinstance(payload, (bytes, bytearray)) else 0
        except Exception:
            size = 0
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
