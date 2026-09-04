"""The name and the icon the desktop sees us as.

A plain ``python -m dirsynk`` inherits the interpreter's identity: macOS reads the
application menu and the Dock icon from the *bundle* that is running, which is the
Python framework, so the app introduces itself as "Python" with the Python icon. The
name is fixable — the bundle's info dictionary is mutable at runtime — and the icons
we simply hand to Tk and to the Dock ourselves.

Everything here is best-effort and platform-guarded: on a machine where a step is not
available the app still opens, just wearing the interpreter's clothes.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import tkinter as tk
from pathlib import Path

APP_NAME = "DirsyNK"

RESOURCES = Path(__file__).resolve().parent / "resources"
ICON_SIZES = (512, 256, 128, 64, 32)


def icon_paths() -> list[Path]:
    """The icon PNGs, largest first. Empty if the resources are missing."""
    candidates = (RESOURCES / f"icon-{size}.png" for size in ICON_SIZES)
    return [path for path in candidates if path.is_file()]


# --- the macOS half: just enough Objective-C to be introduced properly ----------------


def _runtime() -> ctypes.CDLL | None:
    """The Objective-C runtime with Foundation and AppKit loaded, or None."""
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        for framework in ("Foundation", "AppKit"):
            ctypes.cdll.LoadLibrary(ctypes.util.find_library(framework))
    except (OSError, TypeError):  # pragma: no cover - a Mac without the frameworks
        return None
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _send(objc, restype, receiver, selector: bytes, *args):
    """``[receiver selector:args]``. Arguments are ctypes values; None receivers are safe."""
    if not receiver:
        return None
    signature = [ctypes.c_void_p, ctypes.c_void_p, *(type(arg) for arg in args)]
    send = ctypes.CFUNCTYPE(restype, *signature)(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    )
    return send(ctypes.c_void_p(receiver), ctypes.c_void_p(objc.sel_registerName(selector)), *args)


def _nsstring(objc, text: str):
    cls = objc.objc_getClass(b"NSString")
    return ctypes.c_void_p(
        _send(objc, ctypes.c_void_p, cls, b"stringWithUTF8String:", ctypes.c_char_p(text.encode()))
    )


def claim_app_name() -> None:
    """Introduce us to macOS as DirsyNK rather than as Python.

    The application menu is built from the main bundle's ``CFBundleName`` when
    ``NSApplication`` finishes launching, so this has to run *before* Tk exists —
    afterwards the menu bar is already drawn and the change comes too late.

    Sending ``setObject:forKey:`` to an immutable dictionary would raise an
    Objective-C exception, which no ``except`` here could catch — it would take the
    whole process down. Hence the explicit mutability check rather than a try block.
    """
    objc = _runtime()
    if objc is None:
        return
    bundle = _send(objc, ctypes.c_void_p, objc.objc_getClass(b"NSBundle"), b"mainBundle")
    info = _send(objc, ctypes.c_void_p, bundle, b"infoDictionary")
    mutable = objc.objc_getClass(b"NSMutableDictionary")
    if not info or not mutable:
        return
    if not _send(objc, ctypes.c_bool, info, b"isKindOfClass:", ctypes.c_void_p(mutable)):
        return  # pragma: no cover - a real .app bundle, which already has its own name
    _send(
        objc,
        None,
        info,
        b"setObject:forKey:",
        _nsstring(objc, APP_NAME),
        _nsstring(objc, "CFBundleName"),
    )


def _set_dock_icon(path: Path) -> None:
    """The Dock and the ⌘-tab switcher read this, not ``wm iconphoto``."""
    objc = _runtime()
    if objc is None:
        return
    image = _send(objc, ctypes.c_void_p, objc.objc_getClass(b"NSImage"), b"alloc")
    image = _send(
        objc, ctypes.c_void_p, image, b"initWithContentsOfFile:", _nsstring(objc, str(path))
    )
    app = _send(objc, ctypes.c_void_p, objc.objc_getClass(b"NSApplication"), b"sharedApplication")
    if image and app:
        _send(objc, None, app, b"setApplicationIconImage:", ctypes.c_void_p(image))


# --- the cross-platform half ----------------------------------------------------------


def apply_icon(window: tk.Tk) -> None:
    """Dress ``window`` and every window it opens in the DirsyNK icon."""
    paths = icon_paths()
    if not paths:  # pragma: no cover - only if the package was installed without its data
        return
    # Tk holds no reference to a PhotoImage, so an unheld one is garbage collected and
    # the icon goes blank with it. The window owns them, and outlives them by nothing.
    window.icon_images = [tk.PhotoImage(master=window, file=str(path)) for path in paths]
    try:
        window.iconphoto(True, *window.icon_images)
    except tk.TclError:  # pragma: no cover - some window managers refuse
        pass
    _set_dock_icon(paths[0])
