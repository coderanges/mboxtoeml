import io
import mailbox
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from mbox2eml import _make_filename, convert, convert_detailed, inspect_mbox
from modify_eml import read_and_modify_eml, validate_header_name, validate_header_value
from read_eml import parse_eml, read_eml


class MailToolTests(unittest.TestCase):
    def test_make_filename_keeps_index_when_truncated(self):
        message = EmailMessage()
        message["Subject"] = "Very long subject " * 20
        message["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"

        filename = _make_filename(42, message, max_length=60)

        self.assertLessEqual(len(filename), 60)
        self.assertIn("2024-01-01", filename)
        self.assertTrue(filename.endswith(" - 42.eml"))

    def test_convert_creates_eml_files_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "sample.mbox"
            output_dir = temp_path / "output"
            progress_updates = []

            mbox = mailbox.mbox(mbox_path)
            try:
                first = EmailMessage()
                first["Subject"] = "Project / kickoff"
                first["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"
                first["From"] = "sender@example.com"
                first["To"] = "receiver@example.com"
                first.set_content("first body")
                mbox.add(first)

                second = EmailMessage()
                second["Subject"] = "=?utf-8?q?R=C3=A9sum=C3=A9_update?="
                second["Date"] = "Tue, 02 Jan 2024 09:00:00 +0000"
                second["From"] = "sender@example.com"
                second["To"] = "receiver@example.com"
                second.set_content("second body")
                mbox.add(second)
                mbox.flush()
            finally:
                mbox.close()

            created = convert(
                str(mbox_path),
                str(output_dir),
                progress_callback=lambda index, filename, total: progress_updates.append(
                    (index, filename, total)
                ),
            )

            self.assertEqual(len(created), 2)
            self.assertEqual(
                [Path(path).name for path in created],
                [
                    "Project _ kickoff - 2024-01-01 - 1.eml",
                    "Resume update - 2024-01-02 - 2.eml",
                ],
            )
            self.assertEqual(
                progress_updates,
                [
                    (1, "Project _ kickoff - 2024-01-01 - 1.eml", 2),
                    (2, "Resume update - 2024-01-02 - 2.eml", 2),
                ],
            )

    def test_modify_eml_replaces_existing_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input.eml"
            output_path = temp_path / "output.eml"

            message = EmailMessage()
            message["Subject"] = "Header update"
            message["From"] = "sender@example.com"
            message["To"] = "old@example.com"
            message.set_content("body")
            input_path.write_bytes(message.as_bytes())

            read_and_modify_eml(
                str(input_path),
                str(output_path),
                header_name="To",
                header_value="new@example.com",
            )

            with output_path.open("rb") as eml_data:
                updated = BytesParser(policy=policy.default).parse(eml_data)

            self.assertEqual(updated.get_all("To"), ["new@example.com"])

    def test_read_eml_handles_single_part_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "single-part.eml"
            output = io.StringIO()

            message = EmailMessage()
            message["Subject"] = "Single part"
            message["From"] = "sender@example.com"
            message["To"] = "receiver@example.com"
            message.set_content("hello from the body")
            eml_path.write_bytes(message.as_bytes())

            read_eml(str(eml_path), out=output)

            rendered = output.getvalue()
            self.assertIn("Subject: Single part", rendered)
            self.assertIn("Body:", rendered)
            self.assertIn("hello from the body", rendered)


def _write_mbox(mbox_path, messages):
    mbox = mailbox.mbox(mbox_path)
    try:
        for msg in messages:
            mbox.add(msg)
        mbox.flush()
    finally:
        mbox.close()


def _msg(subject="", date="", frm="sender@example.com", to="receiver@example.com", body="body"):
    m = EmailMessage()
    if subject:
        m["Subject"] = subject
    if date:
        m["Date"] = date
    m["From"] = frm
    m["To"] = to
    m.set_content(body)
    return m


class FilenameTests(unittest.TestCase):
    def test_rfc2047_subject_decoded(self):
        m = EmailMessage()
        m["Subject"] = "=?utf-8?q?R=C3=A9sum=C3=A9_update?="
        m["Date"] = "Tue, 02 Jan 2024 09:00:00 +0000"
        self.assertIn("Resume update", _make_filename(2, m))

    def test_malformed_date_does_not_crash(self):
        m = EmailMessage()
        m["Subject"] = "Hello"
        m["Date"] = "not a real date {{{"
        name = _make_filename(1, m)
        self.assertTrue(name.endswith(" - 1.eml"))
        self.assertIn("Hello", name)

    def test_empty_and_cjk_subjects_fall_back(self):
        m = EmailMessage()
        m["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"
        self.assertTrue(_make_filename(1, m).startswith("2024-01-01"))
        cjk = EmailMessage()
        cjk["Subject"] = "日本語テスト"
        cjk["Date"] = "Mon, 01 Jan 2024 10:30:00 +0000"
        name = _make_filename(1, cjk)
        self.assertTrue(name.endswith(" - 1.eml"))
        self.assertLessEqual(len(name), 120)

    def test_unsafe_chars_replaced(self):
        m = EmailMessage()
        m["Subject"] = 'a/b\\c:d*e?f"g<h>i|j'
        name = _make_filename(1, m)
        for ch in '/\\:*?"<>|':
            self.assertNotIn(ch, name.replace(" - 1.eml", ""))

    def test_duplicate_subjects_stay_unique(self):
        m1 = _msg("Same subject", "Mon, 01 Jan 2024 10:30:00 +0000")
        m2 = _msg("Same subject", "Mon, 01 Jan 2024 10:30:00 +0000")
        n1 = _make_filename(1, m1)
        n2 = _make_filename(2, m2)
        self.assertNotEqual(n1, n2)
        self.assertTrue(n1.endswith(" - 1.eml"))
        self.assertTrue(n2.endswith(" - 2.eml"))


class ConversionRobustnessTests(unittest.TestCase):
    def test_malformed_message_continues_and_reports(self):
        import mbox2eml as core

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "sample.mbox"
            output_dir = temp_path / "output"
            _write_mbox(
                str(mbox_path),
                [
                    _msg("first", "Mon, 01 Jan 2024 10:30:00 +0000", body="one"),
                    _msg("second", "Mon, 01 Jan 2024 10:30:00 +0000", body="two"),
                    _msg("third", "Mon, 01 Jan 2024 10:30:00 +0000", body="three"),
                ],
            )
            orig = core._make_filename

            def flaky(index, message, max_length=120):
                if index == 2:
                    raise RuntimeError("simulated corrupt message")
                return orig(index, message, max_length=max_length)

            core._make_filename = flaky
            try:
                progress, errors = [], []
                report = convert_detailed(
                    str(mbox_path),
                    str(output_dir),
                    progress_callback=lambda i, f, t: progress.append((i, f, t)),
                    error_callback=lambda i, e, t: errors.append((i, e, t)),
                )
            finally:
                core._make_filename = orig

            self.assertEqual(report["succeeded"], 2)
            self.assertEqual(report["failed"], 1)
            self.assertEqual(report["total"], 3)
            self.assertEqual(len(report["items"]), 3)
            self.assertEqual(report["items"][1]["status"], "error")
            self.assertEqual(len(progress), 2)
            self.assertEqual(len(errors), 1)
            # convert() back-compat still returns successes list
            self.assertEqual(len(report["created"]), 2)

    def test_collision_skip_and_rename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "sample.mbox"
            output_dir = temp_path / "output"
            _write_mbox(str(mbox_path), [_msg("Hello", "Mon, 01 Jan 2024 10:30:00 +0000")])

            first = convert_detailed(str(mbox_path), str(output_dir), collision="overwrite")
            self.assertEqual(first["succeeded"], 1)
            names = [Path(p).name for p in first["created"]]

            skipped = convert_detailed(str(mbox_path), str(output_dir), collision="skip")
            self.assertEqual(skipped["succeeded"], 0)
            self.assertEqual(skipped["skipped_count"], 1)
            self.assertEqual(skipped["items"][0]["status"], "skipped")

            renamed = convert_detailed(str(mbox_path), str(output_dir), collision="rename")
            self.assertEqual(renamed["succeeded"], 1)
            new_names = [Path(p).name for p in renamed["created"]]
            self.assertNotEqual(names, new_names)
            self.assertIn("(2)", new_names[0])
            self.assertLessEqual(len(new_names[0]), 120)

            with self.assertRaises(ValueError):
                convert_detailed(str(mbox_path), str(output_dir), collision="bogus")

    def test_cancel_stops_conversion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "sample.mbox"
            output_dir = temp_path / "output"
            _write_mbox(
                str(mbox_path),
                [_msg(f"m{i}", "Mon, 01 Jan 2024 10:30:00 +0000") for i in range(5)],
            )
            calls = {"n": 0}

            def stop_after_one():
                return calls["n"] >= 1

            def on_progress(i, f, t):
                calls["n"] += 1

            report = convert_detailed(
                str(mbox_path), str(output_dir), progress_callback=on_progress, should_stop=stop_after_one
            )
            self.assertTrue(report["cancelled"])
            self.assertLess(report["succeeded"], 5)

    def test_inspect_bounded_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            mbox_path = temp_path / "sample.mbox"
            _write_mbox(
                str(mbox_path),
                [_msg(f"m{i}", "Mon, 01 Jan 2024 10:30:00 +0000") for i in range(5)],
            )
            info = inspect_mbox(str(mbox_path), limit=2)
            self.assertEqual(info["total"], 5)
            self.assertEqual(len(info["messages"]), 2)
            self.assertEqual(info["messages"][0]["index"], 1)
            self.assertIn("suggested_output", info)


class ParseEmlTests(unittest.TestCase):
    def test_multipart_prefers_plain_and_lists_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "alt.eml"
            m = EmailMessage()
            m["Subject"] = "alt"
            m["From"] = "a@example.com"
            m["To"] = "b@example.com"
            m.set_content("plain version")
            m.add_alternative("<b>html version</b>", subtype="html")
            eml_path.write_bytes(m.as_bytes())
            info = parse_eml(str(eml_path))
            self.assertIn("plain version", info["body_text"])
            self.assertTrue(info["has_html"])
            self.assertIn("html version", info["body_html"])
            self.assertEqual(info["attachments"], [])

    def test_html_only_derives_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "html.eml"
            m = EmailMessage()
            m["Subject"] = "html only"
            m["From"] = "a@example.com"
            m["To"] = "b@example.com"
            m.set_content("<p>hello <b>world</b></p>", subtype="html")
            eml_path.write_bytes(m.as_bytes())
            info = parse_eml(str(eml_path))
            self.assertTrue(info["has_html"])
            self.assertIn("hello", info["body_text"])

    def test_attachments_and_recipients(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "att.eml"
            m = EmailMessage()
            m["Subject"] = "with files"
            m["From"] = "a@example.com"
            m["To"] = "b@example.com, c@example.com"
            m["Cc"] = "d@example.com"
            m.set_content("see attached")
            m.add_attachment(b"12345", maintype="text", subtype="plain", filename="notes.txt")
            eml_path.write_bytes(m.as_bytes())
            info = parse_eml(str(eml_path))
            self.assertEqual(len(info["attachments"]), 1)
            self.assertEqual(info["attachments"][0]["filename"], "notes.txt")
            self.assertEqual(info["attachments"][0]["size_bytes"], 5)
            self.assertIn("d@example.com", info["cc"])
            out = io.StringIO()
            read_eml(str(eml_path), out=out)
            rendered = out.getvalue()
            self.assertIn("Cc:", rendered)
            self.assertIn("notes.txt", rendered)

    def test_broken_charset_does_not_raise(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            eml_path = Path(temp_dir) / "broken.eml"
            raw = (
                b"Subject: broken\r\nFrom: a@example.com\r\nTo: b@example.com\r\n"
                b"Content-Type: text/plain; charset=unknown-charset-xyz\r\n\r\nhello bytes"
            )
            eml_path.write_bytes(raw)
            info = parse_eml(str(eml_path))
            self.assertIn("hello", info["body_text"])


class ModifyValidationTests(unittest.TestCase):
    def test_rejects_header_injection_and_bad_names(self):
        with self.assertRaises(ValueError):
            validate_header_name("Bad\nHeader")
        with self.assertRaises(ValueError):
            validate_header_name("1bad")
        with self.assertRaises(ValueError):
            validate_header_name("")
        with self.assertRaises(ValueError):
            validate_header_value("a\r\nBcc: evil@x")
        with self.assertRaises(ValueError):
            validate_header_value("x\x00y")

    def test_modify_adds_missing_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            src = temp_path / "a.eml"
            dst = temp_path / "b.eml"
            m = EmailMessage()
            m["Subject"] = "t"
            m["From"] = "a@example.com"
            m.set_content("body")
            src.write_bytes(m.as_bytes())
            read_and_modify_eml(str(src), str(dst), header_name="X-Custom", header_value="yes")
            info = parse_eml(str(dst))
            self.assertEqual(info["subject"], "t")


if __name__ == "__main__":
    unittest.main()
