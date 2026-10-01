"""Native OS folder picker bridge.

Uses only the Python standard library (``tkinter.filedialog``), which shows
the operating system's own folder chooser on Linux, macOS, and Windows.

All platform/toolkit specifics live here; ``server.py`` only calls
``choose_output_directory()`` and ``validate_chosen_directory()``.

If no display or toolkit is available (e.g. headless server), the picker
raises ``PickerUnavailable`` and the caller must fall back to a typed path
instead of faking a native dialog.
"""
import os
import threading

_picker_lock = threading.Lock()


class PickerUnavailable(Exception):
    """The native picker cannot run in this environment."""


class PickerBusy(Exception):
    """A native picker dialog is already open."""


def choose_output_directory(initial_dir=None):
    """Open the OS native folder chooser.

    Args:
        initial_dir: Optional directory the dialog should start in.

    Returns:
        The selected absolute path, or "" when the user cancels.

    Raises:
        PickerUnavailable: No display/toolkit available.
        PickerBusy: Another picker dialog is already open.
    """
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError as exc:
        raise PickerUnavailable("Native picker unavailable: tkinter is not installed") from exc

    if not _picker_lock.acquire(blocking=False):
        raise PickerBusy("A folder picker is already open")

    try:
        try:
            root = tkinter.Tk()
        except tkinter.TclError as exc:
            raise PickerUnavailable("Native picker unavailable (no display)") from exc
        try:
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except tkinter.TclError:
                pass  # window-manager nicety only; never fatal
            kwargs = {"parent": root, "title": "Choose output folder", "mustexist": True}
            if isinstance(initial_dir, str) and initial_dir.strip():
                candidate = os.path.abspath(os.path.expanduser(initial_dir.strip()))
                if os.path.isdir(candidate):
                    kwargs["initialdir"] = candidate
            selected = filedialog.askdirectory(**kwargs)
            return selected or ""
        finally:
            try:
                root.destroy()
            except tkinter.TclError:
                pass
    finally:
        _picker_lock.release()


def validate_chosen_directory(raw):
    """Validate a natively picked output directory. Never trusts the caller.

    Returns the canonical absolute path.

    Raises:
        ValueError: Empty, non-string, or NUL-containing input.
        FileNotFoundError: Path does not exist (may have vanished).
        NotADirectoryError: Path exists but is not a directory.
        PermissionError: Directory cannot be listed (inaccessible).
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError("No folder was selected")
    path = os.path.realpath(raw.strip())
    if not os.path.exists(path):
        raise FileNotFoundError(f"Selected folder no longer exists: {path}")
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Selected path is not a folder: {path}")
    try:
        os.listdir(path)
    except PermissionError as exc:
        raise PermissionError(f"Selected folder is not accessible: {path}") from exc
    except OSError:
        pass  # other listing errors surface at conversion time with context
    return path
