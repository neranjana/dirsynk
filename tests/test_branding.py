"""The name and icon the desktop sees — the parts that need no display."""

from __future__ import annotations

import struct
import sys

import pytest

from dirsynk.ui import branding


def test_every_declared_icon_size_ships_as_a_square_rgba_png() -> None:
    paths = branding.icon_paths()
    assert [path.name for path in paths] == [f"icon-{size}.png" for size in branding.ICON_SIZES]

    for path, size in zip(paths, branding.ICON_SIZES, strict=True):
        header = path.read_bytes()[:26]
        assert header[:8] == b"\x89PNG\r\n\x1a\n", path
        width, height, depth, colour = struct.unpack(">IIBB", header[16:26])
        assert (width, height, depth, colour) == (size, size, 8, 6), path


def test_the_runtime_is_only_reached_for_on_macos() -> None:
    assert (branding._runtime() is not None) == (sys.platform == "darwin")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS is the only one that needs telling")
def test_claiming_the_name_replaces_python_in_the_bundle() -> None:
    """The application menu is drawn from CFBundleName; unclaimed, it reads "Python"."""
    import ctypes

    branding.claim_app_name()

    objc = branding._runtime()
    bundle = branding._send(objc, ctypes.c_void_p, objc.objc_getClass(b"NSBundle"), b"mainBundle")
    info = branding._send(objc, ctypes.c_void_p, bundle, b"infoDictionary")
    name = branding._send(
        objc, ctypes.c_void_p, info, b"objectForKey:", branding._nsstring(objc, "CFBundleName")
    )
    assert branding._send(objc, ctypes.c_char_p, name, b"UTF8String") == b"DirsyNK"
