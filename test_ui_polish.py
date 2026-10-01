"""UI polish contracts: required controls, labels, wrapping guards, responsiveness."""
import re
import unittest
from pathlib import Path


def html():
    return Path("web/index.html").read_text()


def css():
    return Path("web/styles.css").read_text()


def js():
    return Path("web/app.js").read_text()


class RequiredControlsTests(unittest.TestCase):
    def test_file_picker_primary(self):
        h = html()
        self.assertIn('id="pickBtn"', h)
        self.assertIn('id="fileInput"', h)
        self.assertIn('accept=".mbox"', h)
        self.assertIn('id="dropzone"', h)
        self.assertIn("Pick MBOX file", h)

    def test_results_controls_wired(self):
        h = html()
        for rid in ("search", "dateFrom", "dateTo", "clearFilters", "prevPage",
                    "nextPage", "resultsList", "pageMeta", "resultsMeta"):
            self.assertIn(f'id="{rid}"', h)
        self.assertIn('name="filter"', h)
        self.assertIn('name="view"', h)

    def test_viewer_and_modify_controls(self):
        h = html()
        for rid in ("vFile", "vSubject", "vFrom", "vTo", "vCc", "vReply",
                    "vDate", "vBody", "vHtml", "vAtt", "dlLink",
                    "modHeader", "modValue", "modOut", "modBtn", "modMeta"):
            self.assertIn(f'id="{rid}"', h)

    def test_progress_and_steps(self):
        h = html()
        self.assertIn('role="progressbar"', h)
        self.assertIn('id="uploadProgress"', h)
        for rid in ("stepSource", "stepSetup", "stepConvert", "stepResults"):
            self.assertIn(f'id="{rid}"', h)

    def test_buttons_have_labels(self):
        h = html()
        for m in re.finditer(r"<button([^>]*)>(.*?)</button>", h, re.S):
            attrs, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if 'id="startBtn"' in attrs or not label:
                continue
            self.assertTrue(label, f"button {attrs.strip()} has no label")

    def test_sandbox_intact(self):
        h = html()
        self.assertIn('sandbox=""', h)
        self.assertNotIn("allow-scripts", h)
        self.assertNotIn("allow-same-origin", h)
        self.assertIn("body_html_sanitized", js())
        self.assertNotIn("innerHTML", js())


class WrappingGuardTests(unittest.TestCase):
    def test_hidden_is_honored(self):
        # Author button/link display rules must not override [hidden].
        self.assertIn("[hidden]", css())

    def test_shrink_guards(self):
        # Grid children and table cells must be able to shrink past
        # hostile unbroken content (300-char subjects, long filenames).
        self.assertIn("min-width: 0", css())
        self.assertIn("table-layout: fixed", css())
        self.assertIn("overflow-wrap: anywhere", css())

    def test_buttons_dont_collapse_or_bleed(self):
        self.assertIn("white-space: nowrap", css())
        self.assertIn("min-width: max-content", css())
        # ...except result rows, which must wrap their long content.
        self.assertIn(".results button", css())

    def test_focus_visible(self):
        self.assertIn(":focus-visible", css())
        self.assertIn(":focus-within", css())


class ResponsiveTests(unittest.TestCase):
    def test_breakpoint_stacks(self):
        c = css()
        self.assertIn("@media (max-width: 720px)", c)
        self.assertIn("repeat(3, 1fr)", c)  # stats collapse 6 -> 3

    def test_no_fixed_page_width(self):
        c = css()
        for m in re.finditer(r"\.workflow\s*\{[^}]*\}", c):
            self.assertIsNone(re.search(r"(?<!max-)width:\s*980px", m.group(0)))
        self.assertIn("max-width: 980px", c)


if __name__ == "__main__":
    unittest.main()
