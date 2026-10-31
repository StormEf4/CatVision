"""Faster screen capture (DXGI Desktop Duplication) for auto levels (beta):
the pure-Python parts (mip level choice, shrinking to the thumbnail, the
DXGI-or-GDI choice per display) and the option.  The COM calls themselves
only run on Windows (see selftest.dxgi_capture_check)."""

from __future__ import annotations

import random
import threading
import time
import unittest
from unittest import mock

from catvision import autolevels, dxgicapture
from catvision.config import Config, Options
from catvision.dxgicapture import FastCapture, choose_mip, mip_size, resample

W, H = dxgicapture.THUMB_W, dxgicapture.THUMB_H
D1, D2 = "\\\\.\\DISPLAY1", "\\\\.\\DISPLAY2"
R1, R2 = (0, 0, 1920, 1080), (1920, 0, 4480, 1440)


def reference(data: bytes, pitch: int, width: int, height: int, w: int = W, h: int = H) -> bytes:
    """The plainest box average: every thumbnail pixel is the rounded mean
    of the source pixels in its box."""
    out = bytearray(w * h * 4)
    for j in range(h):
        y0 = j * height // h
        y1 = max((j + 1) * height // h, y0 + 1)
        for i in range(w):
            x0 = i * width // w
            x1 = max((i + 1) * width // w, x0 + 1)
            for c in range(3):
                vals = [data[y * pitch + x * 4 + c] for y in range(y0, y1) for x in range(x0, x1)]
                out[(j * w + i) * 4 + c] = int(sum(vals) / len(vals) + 0.5)
    return bytes(out)


def picture(width: int, height: int, pitch: int, seed: int = 1) -> bytes:
    rnd = random.Random(seed)
    return bytes(rnd.randrange(256) for _ in range(pitch * height))


class MipChoiceTest(unittest.TestCase):
    def test_smallest_level_still_big_enough(self):
        cases = {(1920, 1080): (4, 120, 67), (2560, 1440): (4, 160, 90),
                 (3840, 2160): (5, 120, 67), (1024, 768): (3, 128, 96),
                 (1366, 768): (3, 170, 96), (5120, 1440): (4, 320, 90),
                 (96, 54): (0, 96, 54), (191, 107): (0, 191, 107), (192, 108): (1, 96, 54)}
        for size, want in cases.items():
            self.assertEqual(choose_mip(*size), want, size)

    def test_level_is_big_enough_and_the_next_is_not(self):
        for width in range(60, 4000, 37):
            for height in (40, 54, 600, 1080, 2160):
                level, mw, mh = choose_mip(width, height)
                self.assertEqual((mw, mh), mip_size(width, height, level))
                if level:
                    self.assertGreaterEqual(mw, W)
                    self.assertGreaterEqual(mh, H)
                nw, nh = mip_size(width, height, level + 1)
                self.assertTrue(nw < W or nh < H or (nw, nh) == (mw, mh), (width, height))

    def test_mip_size_matches_direct3d(self):
        self.assertEqual(mip_size(1920, 1080, 4), (120, 67))   # 1080 / 16 = 67.5, rounded down
        self.assertEqual(mip_size(3, 5, 10), (1, 1))


class ResampleTest(unittest.TestCase):
    def test_matches_reference_box_average_with_row_padding(self):
        for width, height, pad in ((120, 67, 32), (160, 90, 0), (128, 96, 64), (170, 96, 8),
                                   (96, 54, 0), (200, 120, 4)):
            pitch = width * 4 + pad
            data = picture(width, height, pitch, seed=width + height)
            got = resample(data, pitch, width, height)
            self.assertEqual(len(got), W * H * 4)
            self.assertEqual(got, reference(data, pitch, width, height), (width, height, pad))

    def test_padding_bytes_are_ignored(self):
        width, height, pad = 120, 67, 16
        pitch = width * 4 + pad
        data = bytearray(picture(width, height, pitch))
        a = resample(bytes(data), pitch, width, height)
        for y in range(height):
            data[y * pitch + width * 4: (y + 1) * pitch] = b"\xff" * pad
        self.assertEqual(resample(bytes(data), pitch, width, height), a)

    def test_last_row_needs_no_padding(self):
        width, height, pitch = 120, 67, 120 * 4 + 64
        data = picture(width, height, pitch)[:pitch * (height - 1) + width * 4]
        self.assertEqual(resample(data, pitch, width, height),
                         reference(data, pitch, width, height))

    def test_smaller_than_thumbnail_repeats_pixels(self):
        width, height = 48, 27
        data = picture(width, height, width * 4)
        self.assertEqual(resample(data, width * 4, width, height),
                         reference(data, width * 4, width, height))

    def test_flat_picture_stays_flat_and_alpha_is_zero(self):
        width, height = 160, 90
        data = bytes([10, 20, 30, 255]) * (width * height)
        out = resample(data, width * 4, width, height)
        self.assertEqual(out, bytes([10, 20, 30, 0]) * (W * H))

    def test_too_small_buffer_raises(self):
        with self.assertRaises(ValueError):
            resample(b"\x00" * 100, 480, 120, 67)
        with self.assertRaises(ValueError):
            resample(b"\x00" * (480 * 67), 400, 120, 67)   # pitch shorter than a row

    def test_measures_like_the_full_picture(self):
        # A night scene: dark with a few small bright lights.  The thumbnail
        # keeps the lights (averaged), so the brightness matches the box-
        # averaged full picture to within rounding.
        width, height = 160, 90
        data = bytearray(bytes([6, 7, 8, 0]) * (width * height))
        for x, y in ((20, 10), (80, 45), (150, 80)):
            data[(y * width + x) * 4: (y * width + x) * 4 + 3] = b"\xff\xff\xff"
        thumb = resample(bytes(data), width * 4, width, height)
        m = autolevels.measure(thumb, W)
        self.assertGreater(max(thumb), 100)          # lights still there
        self.assertLess(m.key, 0.06)


class FakeDup:
    def __init__(self, log, results):
        self.log, self.results, self.closed = log, list(results), False

    def grab(self):
        r = self.results.pop(0) if self.results else b"F" * (W * H * 4)
        if isinstance(r, BaseException):
            raise r
        return r

    def close(self):
        self.closed = True
        self.log.append("close")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FastCaptureTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.slow_calls = []
        self.opened = []
        self.log = []
        self.plan = {}      # display id -> list of FakeDup results or an exception to raise on open

    def slow(self, rect):
        self.slow_calls.append(rect)
        return b"G" * (W * H * 4)

    def open_fast(self, did, rect):
        self.opened.append(did)
        plan = self.plan.get(did, [])
        if isinstance(plan, BaseException):
            raise plan
        self.plan[did] = []                         # the next one opened just works
        dup = FakeDup(self.log, plan)
        self.dups = getattr(self, "dups", []) + [dup]
        return dup

    def make(self):
        return FastCapture(self.slow, self.open_fast, clock=self.clock)

    def test_uses_dxgi_when_it_works(self):
        fc = self.make()
        self.assertEqual(fc.capture(D1, R1)[:1], b"F")
        self.assertEqual(fc.routes, {D1: "dxgi"})
        self.assertEqual(self.slow_calls, [])
        fc.capture(D1, R1)
        self.assertEqual(self.opened, [D1])          # opened once, then reused

    def test_open_failure_uses_gdi_and_retries_after_a_while(self):
        self.plan[D1] = dxgicapture.Unsupported("rotated display")
        fc = self.make()
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(fc.routes[D1], "gdi")
        self.assertIn("rotated", fc.reasons[D1])
        for _ in range(5):
            self.clock.t += 0.9
            fc.capture(D1, R1)
        self.assertEqual(self.opened, [D1])          # not every capture
        self.clock.t += dxgicapture.RETRY_S
        self.plan[D1] = []
        self.assertEqual(fc.capture(D1, R1)[:1], b"F")
        self.assertEqual(self.opened, [D1, D1])
        self.assertNotIn(D1, fc.reasons)

    def test_retries_further_apart_after_each_failure(self):
        self.plan[D1] = dxgicapture.DxgiError("DuplicateOutput", dxgicapture.DXGI_ERROR_UNSUPPORTED)
        fc = self.make()
        fc.capture(D1, R1)
        for gap in (5, 10, 20, 40, 60, 60):
            self.clock.t += gap - 0.5
            fc.capture(D1, R1)
            n = len(self.opened)
            self.clock.t += 0.5
            fc.capture(D1, R1)
            self.assertEqual(len(self.opened), n + 1, gap)
        self.plan[D1] = []
        self.clock.t += 60
        self.assertEqual(fc.capture(D1, R1)[:1], b"F")
        self.assertNotIn(D1, fc._fails)                # works again: back to the short wait

    def test_grab_error_drops_it_and_falls_back(self):
        self.plan[D1] = [b"F" * (W * H * 4), dxgicapture.DxgiError("Map", -1)]
        fc = self.make()
        fc.capture(D1, R1)
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(self.log, ["close"])
        self.assertEqual(fc.routes[D1], "gdi")
        fc.capture(D1, R1)
        self.assertEqual(self.opened, [D1])          # waits before trying again

    def test_access_lost_is_made_again_at_once(self):
        lost = dxgicapture.AccessLost("AcquireNextFrame", dxgicapture.DXGI_ERROR_ACCESS_LOST)
        self.plan[D1] = [lost]
        fc = self.make()
        self.assertEqual(fc.capture(D1, R1)[:1], b"F")
        self.assertEqual(self.opened, [D1, D1])
        self.assertEqual(self.log, ["close"])
        self.assertNotIn(D1, fc._failed)

    def test_access_lost_twice_waits(self):
        lost = dxgicapture.AccessLost("AcquireNextFrame", dxgicapture.DXGI_ERROR_ACCESS_LOST)
        fc = self.make()
        self.plan[D1] = [lost]
        real_open = self.open_fast

        def open_lost(did, rect):
            dup = real_open(did, rect)
            dup.results = [lost]
            return dup
        fc._open = open_lost
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(len(self.opened), 2)
        fc.capture(D1, R1)
        self.assertEqual(len(self.opened), 2)

    def test_no_picture_yet_uses_gdi_without_failing(self):
        self.plan[D1] = [None]
        fc = self.make()
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertNotIn(D1, fc._failed)
        self.assertEqual(fc.capture(D1, R1)[:1], b"F")
        self.assertEqual(self.opened, [D1])

    def test_never_a_picture_gives_up_for_a_while(self):
        self.plan[D1] = [None] * 10
        fc = self.make()
        for _ in range(dxgicapture.EMPTY_TRIES):
            self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(self.log, ["close"])
        self.assertIn(D1, fc._failed)
        fc.capture(D1, R1)
        self.assertEqual(self.opened, [D1])

    def test_per_display(self):
        self.plan[D2] = dxgicapture.DxgiError("DuplicateOutput", dxgicapture.DXGI_ERROR_UNSUPPORTED)
        fc = self.make()
        fc.capture(D1, R1)
        fc.capture(D2, R2)
        self.assertEqual(fc.routes, {D1: "dxgi", D2: "gdi"})
        self.assertIn("DXGI_ERROR_UNSUPPORTED", fc.reasons[D2])

    def test_turned_off_releases_and_uses_gdi(self):
        fc = self.make()
        fc.capture(D1, R1)
        fc.enabled = False
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(self.log, ["close"])
        fc.enabled = True
        self.assertEqual(fc.capture(D1, R1)[:1], b"F")
        self.assertEqual(self.opened, [D1, D1])

    def test_display_moved_opens_again(self):
        fc = self.make()
        fc.capture(D1, R1)
        fc.capture(D1, (0, 0, 2560, 1440))
        self.assertEqual(self.opened, [D1, D1])
        self.assertEqual(self.log, ["close"])

    def test_keep_only_and_close(self):
        fc = self.make()
        fc.capture(D1, R1)
        fc.capture(D2, R2)
        fc.keep_only({D2: R2})
        self.assertEqual(self.log, ["close"])
        fc.close()
        self.assertEqual(self.log, ["close", "close"])

    def test_without_dxgi_it_is_gdi_exactly(self):
        fc = FastCapture(self.slow, None, clock=self.clock)
        with mock.patch.object(dxgicapture, "IS_WINDOWS", False):
            fc = FastCapture(self.slow, clock=self.clock)
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(self.slow_calls, [R1])
        self.assertEqual(fc.routes, {D1: "gdi"})
        self.assertEqual(fc.reasons, {})


class NotWindowsTest(unittest.TestCase):
    @unittest.skipIf(dxgicapture.IS_WINDOWS, "checks the non-Windows behaviour")
    def test_watcher_unchanged_off_windows(self):
        w = autolevels.Watcher()
        self.assertFalse(w.available)
        self.assertIs(w._capture, autolevels.gdi_capture)
        fc = FastCapture(autolevels.gdi_capture)
        self.assertIsNone(fc._open)                  # never tries DXGI

    def test_stand_in_capture_never_uses_dxgi(self):
        thumb = bytes([40, 40, 40, 0]) * (W * H)
        calls = []
        w = autolevels.Watcher(capture=lambda rect: calls.append(rect) or thumb)
        with mock.patch.object(dxgicapture, "Duplication",
                               side_effect=AssertionError("DXGI used")):
            w.watch({D1: R1})
            out = w.measure_once()
        w.stop()
        self.assertIsNotNone(out[D1])
        self.assertEqual(calls[-1], R1)
        self.assertEqual(w.routes, {})

    def test_watcher_uses_fast_capture_and_releases_on_its_thread(self):
        thumb = bytes([40, 40, 40, 0]) * (W * H)
        made, closed = [], []

        class Dup:
            def __init__(self, did, rect):
                made.append(threading.current_thread().name)

            def grab(self):
                return thumb

            def close(self):
                closed.append(threading.current_thread().name)
        w = autolevels.Watcher()
        w._use_fast = True
        w.available = True
        with mock.patch.object(dxgicapture, "Duplication", Dup), \
                mock.patch.object(dxgicapture, "IS_WINDOWS", True):
            w.watch({D1: R1})
            deadline = time.monotonic() + 3
            while not w.routes and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(w.routes, {D1: "dxgi"})
            w.fast_capture = False
            deadline = time.monotonic() + 3
            while w.routes.get(D1) != "gdi" and time.monotonic() < deadline:
                time.sleep(0.02)
            w.fast_capture = True
            deadline = time.monotonic() + 3
            while (w.routes.get(D1) != "dxgi" or len(made) < 2) and time.monotonic() < deadline:
                time.sleep(0.02)
            w.stop()
        self.assertEqual(set(made), {"catvision-auto-levels"})
        self.assertEqual(len(closed), 2)              # turned off, then stopped
        self.assertEqual(set(closed), {"catvision-auto-levels"})


class LayoutTest(unittest.TestCase):
    """The structure layouts handed to Windows, checked here with ctypes
    (fixed-size fields, so the sizes are the same as on Windows)."""

    def test_struct_sizes_and_offsets(self):
        import ctypes
        import types

        class FakeDLL:
            def __init__(self, name):
                pass

            def __getattr__(self, name):
                f = types.SimpleNamespace()
                setattr(self, name, f)
                return f
        with mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True), \
                mock.patch.object(ctypes, "WinDLL", FakeDLL, create=True), \
                mock.patch.object(dxgicapture, "_api", None):
            api = dxgicapture._setup()
        ptr = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(api.OUTPUT_DESC), 88 + ptr)
        self.assertEqual(api.OUTPUT_DESC.DesktopCoordinates.offset, 64)
        self.assertEqual(api.OUTPUT_DESC.Rotation.offset, 84)
        self.assertEqual(api.OUTPUT_DESC.Monitor.offset, 88)
        self.assertEqual(ctypes.sizeof(api.FRAME_INFO), 48)
        self.assertEqual(api.FRAME_INFO.AccumulatedFrames.offset, 16)
        self.assertEqual(ctypes.sizeof(api.TEXTURE2D_DESC), 44)
        self.assertEqual(api.TEXTURE2D_DESC.Format.offset, 16)
        self.assertEqual(api.TEXTURE2D_DESC.MiscFlags.offset, 40)
        self.assertEqual(ctypes.sizeof(api.MAPPED), ptr + 8)
        g = api.IID_OUTPUT1
        self.assertEqual((g.Data1, g.Data2, g.Data3), (0x00CDDEA8, 0x939B, 0x4B83))
        self.assertEqual(bytes(g.Data4), bytes.fromhex("a340a685226666cc"))
        desc = api.OUTPUT_DESC()
        name = D1.encode("utf-16-le")
        ctypes.memmove(ctypes.addressof(desc), name, len(name))
        self.assertEqual(desc.name(), D1)

    def test_vtable_slots(self):
        # The slots, from dxgi.h / dxgi1_2.h / d3d11.h (see dxgicapture).
        d = dxgicapture
        self.assertEqual((d.QUERY_INTERFACE, d.RELEASE), (0, 2))
        self.assertEqual(d.FACTORY1_ENUM_ADAPTERS1, 12)
        self.assertEqual(d.ADAPTER_ENUM_OUTPUTS, 7)
        self.assertEqual((d.OUTPUT_GET_DESC, d.OUTPUT1_DUPLICATE_OUTPUT), (7, 22))
        self.assertEqual((d.DUPL_ACQUIRE_NEXT_FRAME, d.DUPL_RELEASE_FRAME), (8, 14))
        self.assertEqual((d.DEVICE_CREATE_TEXTURE_2D, d.DEVICE_CREATE_SHADER_RESOURCE_VIEW), (5, 7))
        self.assertEqual(d.TEXTURE2D_GET_DESC, 10)
        self.assertEqual((d.CONTEXT_MAP, d.CONTEXT_UNMAP, d.CONTEXT_COPY_SUBRESOURCE_REGION,
                          d.CONTEXT_GENERATE_MIPS), (14, 15, 46, 54))


# --------------------------------------------------------------------------
# Fake COM: objects with real vtables (ctypes callbacks), so the ctypes calls
# in dxgicapture run for real.  The slots, signatures and structure offsets
# here are written out again from the SDK headers, not taken from the code.
# --------------------------------------------------------------------------

def _hr(code: int) -> int:
    return code - (1 << 32) if code & 0x80000000 else code


E_FAIL, E_NOINTERFACE = _hr(0x80004005), _hr(0x80004002)
IID_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"
IID_OUTPUT1 = "00cddea8-939b-4b83-a340-a685226666cc"
IID_TEXTURE2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"


class FakeWorld:
    """The fake DXGI / Direct3D: adapters with displays, one desktop size
    and format, a script of what AcquireNextFrame returns, and failures."""

    def __init__(self, adapters, desktop=(1920, 1080, 87)):
        import ctypes
        self.c = ctypes
        self.HR, self.VP, self.U = ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32
        self.adapters = adapters            # [[(device name, rotation), ...], ...]
        self.desktop = desktop
        self.frames: list = []              # "frame", "mouse", "timeout" or an HRESULT
        self.fail: dict = {}                # what -> HRESULT to return
        self.objects: list = []
        self.calls: list = []
        self.errors: list = []
        self.held = False                   # a frame acquired and not released
        self.keep: list = []
        self.textures: dict = {}

    # -- objects -------------------------------------------------------------
    def make(self, kind, slots, qi=None):
        c = self.c
        obj = type("Obj", (), {})()
        obj.kind, obj.refs = kind, 1
        obj.vtbl = (c.c_void_p * 64)()
        any_slot = c.CFUNCTYPE(self.HR, self.VP)
        for n in range(64):
            def unexpected(this, n=n):
                self.errors.append(f"{kind}: slot {n} called")
                return E_FAIL
            cb = any_slot(unexpected)
            self.keep.append(cb)
            obj.vtbl[n] = c.cast(cb, c.c_void_p).value
        qi = qi or {}

        def query(this, riid, out):
            key = bytes((c.c_ubyte * 16).from_address(riid))
            maker = qi.get(key)
            made = maker() if maker else None
            c.c_void_p.from_address(out).value = made.addr if made else None
            return 0 if made else E_NOINTERFACE

        def add_ref(this):
            obj.refs += 1
            return obj.refs

        def release(this):
            obj.refs -= 1
            self.calls.append(("Release", kind))
            if obj.refs < 0:
                self.errors.append(f"{kind}: released too often")
            return max(obj.refs, 0)
        table = {0: (c.CFUNCTYPE(self.HR, self.VP, self.VP, self.VP), query),
                 1: (c.CFUNCTYPE(self.U, self.VP), add_ref),
                 2: (c.CFUNCTYPE(self.U, self.VP), release)}
        table.update(slots)
        for n, (proto, fn) in table.items():
            def checked(this, *args, fn=fn, n=n):
                if this != obj.addr:
                    self.errors.append(f"{kind}: slot {n} called on another object")
                return fn(this, *args)
            cb = proto(checked)
            self.keep.append(cb)
            obj.vtbl[n] = c.cast(cb, c.c_void_p).value
        obj.mem = c.c_void_p(c.addressof(obj.vtbl))
        obj.addr = c.addressof(obj.mem)
        self.objects.append(obj)
        return obj

    def kind_of(self, addr):
        for o in self.objects:
            if o.addr == addr:
                return o.kind
        return None

    def alive(self):
        return sorted(o.kind for o in self.objects if o.refs)

    def out(self, pp, obj):
        self.c.c_void_p.from_address(pp).value = obj.addr if obj else None

    @staticmethod
    def iid(text):
        import uuid
        return uuid.UUID(text).bytes_le

    # -- DXGI ----------------------------------------------------------------
    def create_factory(self, riid, pp):
        c, HR, VP, U = self.c, self.HR, self.VP, self.U
        if bytes((c.c_ubyte * 16).from_address(riid)) != self.iid(IID_FACTORY1):
            self.errors.append("CreateDXGIFactory1: not IDXGIFactory1")

        def enum_adapters1(this, i, out):
            if i >= len(self.adapters):
                self.out(out, None)
                return _hr(dxgicapture.DXGI_ERROR_NOT_FOUND)
            self.out(out, self.make_adapter(i))
            return 0
        self.out(pp, self.make("factory", {12: (c.CFUNCTYPE(HR, VP, U, VP), enum_adapters1)}))
        return 0

    def make_adapter(self, i):
        c, HR, VP, U = self.c, self.HR, self.VP, self.U

        def enum_outputs(this, j, out):
            if j >= len(self.adapters[i]):
                self.out(out, None)
                return _hr(dxgicapture.DXGI_ERROR_NOT_FOUND)
            self.out(out, self.make_output(*self.adapters[i][j]))
            return 0
        return self.make(f"adapter{i}", {7: (c.CFUNCTYPE(HR, VP, U, VP), enum_outputs)})

    def make_output(self, name, rotation):
        import struct
        c, HR, VP = self.c, self.HR, self.VP

        def get_desc(this, p):
            raw = (c.c_char * 92).from_address(p)
            text = name.encode("utf-16-le")
            raw[0:len(text)] = text
            struct.pack_into("<4i2i", raw, 64, 0, 0, self.desktop[0], self.desktop[1],
                             1, rotation)          # RECT, AttachedToDesktop, Rotation
            return 0

        def duplicate_output(this, device, out):
            self.calls.append(("DuplicateOutput", self.kind_of(device)))
            if "DuplicateOutput" in self.fail:
                self.out(out, None)
                return self.fail["DuplicateOutput"]
            self.out(out, self.make_duplication())
            return 0

        def output1():
            if "QueryOutput1" in self.fail:
                return None
            return self.make(f"output1 {name}", {
                22: (c.CFUNCTYPE(HR, VP, VP, VP), duplicate_output)})
        return self.make(f"output {name}", {7: (c.CFUNCTYPE(HR, VP, VP), get_desc)},
                         {self.iid(IID_OUTPUT1): output1})

    def make_duplication(self):
        import struct
        c, HR, VP, U = self.c, self.HR, self.VP, self.U

        def acquire(this, timeout, info, out):
            self.calls.append(("AcquireNextFrame", timeout))
            self.out(out, None)
            if self.held:
                self.errors.append("AcquireNextFrame before ReleaseFrame")
                return _hr(dxgicapture.DXGI_ERROR_INVALID_CALL)
            step = self.frames.pop(0) if self.frames else "timeout"
            if step == "timeout":
                return _hr(dxgicapture.DXGI_ERROR_WAIT_TIMEOUT)
            if isinstance(step, int):
                return step
            raw = (c.c_char * 48).from_address(info)
            struct.pack_into("<qqI", raw, 0, 0 if step == "mouse" else 12345, 777,
                             0 if step == "mouse" else 1)
            self.held = True
            self.out(out, self.make_frame())
            return 0

        def release_frame(this):
            self.calls.append(("ReleaseFrame",))
            if not self.held:
                self.errors.append("ReleaseFrame without a frame")
                return _hr(dxgicapture.DXGI_ERROR_INVALID_CALL)
            self.held = False
            return 0
        return self.make("duplication", {8: (c.CFUNCTYPE(HR, VP, U, VP, VP), acquire),
                                         14: (c.CFUNCTYPE(HR, VP), release_frame)})

    def make_frame(self):
        c, VP = self.c, self.VP

        def get_desc(this, p):
            w, h, fmt = self.desktop
            self.pack_desc(p, w, h, 1, fmt, usage=0, bind=0x20, cpu=0, misc=0)

        def desktop():
            return self.make("desktop texture", {10: (c.CFUNCTYPE(None, VP, VP), get_desc)})
        return self.make("frame resource", {}, {self.iid(IID_TEXTURE2D): desktop})

    def pack_desc(self, p, w, h, mips, fmt, usage, bind, cpu, misc):
        import struct
        struct.pack_into("<IIIIiIIiIII", (self.c.c_char * 44).from_address(p), 0,
                         w, h, mips, 1, fmt, 1, 0, usage, bind, cpu, misc)

    # -- Direct3D ------------------------------------------------------------
    def create_device(self, adapter, driver, software, flags, levels, n, sdk,
                      pdevice, plevel, pcontext):
        import struct
        c, HR, VP, U = self.c, self.HR, self.VP, self.U
        self.calls.append(("D3D11CreateDevice", self.kind_of(adapter), driver, software,
                           flags, levels, n, sdk))
        if "D3D11CreateDevice" in self.fail:
            self.out(pdevice, None)
            self.out(pcontext, None)
            return self.fail["D3D11CreateDevice"]

        def create_texture(this, pdesc, init, out):
            f = struct.unpack_from("<IIIIiIIiIII", (c.c_char * 44).from_address(pdesc))
            name = "staging" if f[7] == 3 else "mips"
            self.calls.append(("CreateTexture2D", name, f, init))
            if f"CreateTexture2D {name}" in self.fail:
                self.out(out, None)
                return self.fail[f"CreateTexture2D {name}"]
            obj = self.make(name, {})
            self.textures[obj.addr] = f
            self.out(out, obj)
            return 0

        def create_view(this, res, desc, out):
            self.calls.append(("CreateShaderResourceView", self.kind_of(res), desc))
            if "CreateShaderResourceView" in self.fail:
                self.out(out, None)
                return self.fail["CreateShaderResourceView"]
            self.out(out, self.make("view", {}))
            return 0

        def copy(this, dst, dsub, x, y, z, src, ssub, box):
            self.calls.append(("CopySubresourceRegion", self.kind_of(dst), dsub, x, y, z,
                               self.kind_of(src), ssub, box))

        def generate_mips(this, view):
            self.calls.append(("GenerateMips", self.kind_of(view)))

        def map_(this, res, sub, kind, flags, pmapped):
            self.calls.append(("Map", self.kind_of(res), sub, kind, flags))
            if "Map" in self.fail:
                return self.fail["Map"]
            w, h = self.textures[res][:2]
            pitch = w * 4 + 64
            buf = c.create_string_buffer(bytes([10, 20, 30, 255]) * (pitch * h // 4))
            self.keep.append(buf)
            struct.pack_into("<QII", (c.c_char * 16).from_address(pmapped), 0,
                             c.addressof(buf), pitch, pitch * h)
            return 0

        def unmap(this, res, sub):
            self.calls.append(("Unmap", self.kind_of(res), sub))
        device = self.make("device", {5: (c.CFUNCTYPE(HR, VP, VP, VP, VP), create_texture),
                                      7: (c.CFUNCTYPE(HR, VP, VP, VP, VP), create_view)})
        context = self.make("context", {
            14: (c.CFUNCTYPE(HR, VP, VP, U, c.c_int32, U, VP), map_),
            15: (c.CFUNCTYPE(None, VP, VP, U), unmap),
            46: (c.CFUNCTYPE(None, VP, VP, U, U, U, U, VP, U, VP), copy),
            54: (c.CFUNCTYPE(None, VP, VP), generate_mips)})
        self.out(pdevice, device)
        self.out(pcontext, context)
        c.c_int32.from_address(plevel).value = 0xb000
        return 0

    def dll(self, name):
        c, VP, U = self.c, self.VP, self.U
        lib = type("DLL", (), {})()
        if name == "dxgi":
            lib.CreateDXGIFactory1 = c.CFUNCTYPE(self.HR, VP, VP)(self.create_factory)
            self.keep.append(lib.CreateDXGIFactory1)
        else:
            lib.D3D11CreateDevice = c.CFUNCTYPE(self.HR, VP, c.c_int32, VP, U, VP, U, U,
                                                VP, VP, VP)(self.create_device)
            self.keep.append(lib.D3D11CreateDevice)
        return lib


class FakeComTest(unittest.TestCase):
    """Duplication's COM calls against the fake: the right slots with the
    right arguments, every reference released, also when something fails."""

    def world(self, adapters=None, desktop=(1920, 1080, 87)):
        import ctypes
        world = FakeWorld(adapters or [[(D1, 1)], [(D2, 0)]], desktop)
        for patch in (mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True),
                      mock.patch.object(ctypes, "WinDLL", world.dll, create=True),
                      mock.patch.object(dxgicapture, "_api", None)):
            patch.start()
            self.addCleanup(patch.stop)
        return world

    def calls(self, world, name):
        return [c for c in world.calls if c[0] == name]

    def test_display_on_the_second_adapter(self):
        world = self.world()
        world.frames = ["frame", "timeout", "mouse", "frame"]
        dup = dxgicapture.Duplication(D2.lower(), R2)       # names match in any case
        # The first adapter and its display were let go while searching, the
        # factory, adapter and output once the duplication was made.
        self.assertEqual(world.alive(), ["context", "device", "duplication", "output1 " + D2])
        self.assertEqual(self.calls(world, "D3D11CreateDevice"),
                         [("D3D11CreateDevice", "adapter1", 0, None, 0x20, None, 0, 7)])
        self.assertEqual(self.calls(world, "DuplicateOutput"), [("DuplicateOutput", "device")])
        world.calls.clear()
        thumb = dup.grab()
        self.assertEqual(thumb, bytes([10, 20, 30, 0]) * (W * H))
        self.assertFalse(world.held)
        self.assertEqual(self.calls(world, "AcquireNextFrame"), [("AcquireNextFrame", 50)])
        made = self.calls(world, "CreateTexture2D")
        self.assertEqual(made[0], ("CreateTexture2D", "mips",
                                   (1920, 1080, 5, 1, 87, 1, 0, 0, 0x28, 0, 1), None))
        self.assertEqual(made[1], ("CreateTexture2D", "staging",
                                   (120, 67, 1, 1, 87, 1, 0, 3, 0, 0x20000, 0), None))
        self.assertEqual(self.calls(world, "CreateShaderResourceView"),
                         [("CreateShaderResourceView", "mips", None)])
        self.assertEqual(self.calls(world, "CopySubresourceRegion"), [
            ("CopySubresourceRegion", "mips", 0, 0, 0, 0, "desktop texture", 0, None),
            ("CopySubresourceRegion", "staging", 0, 0, 0, 0, "mips", 4, None)])
        self.assertEqual(self.calls(world, "GenerateMips"), [("GenerateMips", "view")])
        self.assertEqual(self.calls(world, "Map"), [("Map", "staging", 0, 1, 0)])
        self.assertEqual(self.calls(world, "Unmap"), [("Unmap", "staging", 0)])
        self.assertEqual(self.calls(world, "ReleaseFrame"), [("ReleaseFrame",)])
        self.assertNotIn("frame resource", world.alive())
        self.assertNotIn("desktop texture", world.alive())
        # No new picture: the same thumbnail, nothing to hand back.
        world.calls.clear()
        self.assertIs(dup.grab(), thumb)
        self.assertEqual(world.calls, [("AcquireNextFrame", 0)])
        # Only the pointer moved: the same thumbnail, the frame handed back.
        world.calls.clear()
        self.assertIs(dup.grab(), thumb)
        self.assertEqual(self.calls(world, "ReleaseFrame"), [("ReleaseFrame",)])
        self.assertEqual(self.calls(world, "CopySubresourceRegion"), [])
        self.assertFalse(world.held)
        # A new picture: the textures are used again.
        world.calls.clear()
        self.assertEqual(dup.grab(), thumb)
        self.assertEqual(self.calls(world, "CreateTexture2D"), [])
        self.assertEqual((dup.frames, dup.unchanged), (2, 2))
        dup.close()
        self.assertEqual(world.alive(), [])
        self.assertEqual(world.errors, [])
        with self.assertRaises(dxgicapture.DxgiError):
            dup.grab()                                  # never through released objects
        dup.close()                                     # twice is harmless
        self.assertEqual(world.errors, [])

    def test_small_display_is_copied_straight_to_the_cpu_texture(self):
        world = self.world(desktop=(160, 90, 87))
        world.frames = ["frame"]
        dup = dxgicapture.Duplication(D1)
        self.assertEqual(dup.grab(), bytes([10, 20, 30, 0]) * (W * H))
        self.assertEqual([c[1] for c in self.calls(world, "CreateTexture2D")], ["staging"])
        self.assertEqual(self.calls(world, "GenerateMips"), [])
        self.assertEqual(self.calls(world, "CopySubresourceRegion"), [
            ("CopySubresourceRegion", "staging", 0, 0, 0, 0, "desktop texture", 0, None)])
        dup.close()
        self.assertEqual((world.alive(), world.errors), ([], []))

    def test_setup_failures_release_everything(self):
        access, invalid = _hr(dxgicapture.E_ACCESSDENIED), _hr(dxgicapture.E_INVALIDARG)
        cases = {
            "not found": ([[(D1, 1)]], {}, D2, dxgicapture.Unsupported),
            "rotated": ([[(D1, 2)]], {}, D1, dxgicapture.Unsupported),
            "no device": ([[(D1, 1)]], {"D3D11CreateDevice": E_FAIL}, D1, dxgicapture.DxgiError),
            "no IDXGIOutput1": ([[(D1, 1)]], {"QueryOutput1": 1}, D1, dxgicapture.DxgiError),
            "secure desktop": ([[(D1, 1)]], {"DuplicateOutput": access}, D1,
                               dxgicapture.DxgiError),
            "already duplicated": ([[(D1, 1)]], {"DuplicateOutput": invalid}, D1,
                                   dxgicapture.DxgiError),
        }
        for what, (adapters, fail, did, error) in cases.items():
            with self.subTest(what):
                world = self.world(adapters)
                world.fail = fail
                with self.assertRaises(error):
                    dxgicapture.Duplication(did)
                self.assertEqual(world.alive(), [])
                self.assertEqual(world.errors, [])

    def test_grab_failures_hand_the_frame_back_and_release_everything(self):
        cases = {
            "HDR desktop": ((1920, 1080, 10), {}, dxgicapture.Unsupported),
            "mip texture": ((1920, 1080, 87), {"CreateTexture2D mips": E_FAIL},
                            dxgicapture.DxgiError),
            "view": ((1920, 1080, 87), {"CreateShaderResourceView": E_FAIL},
                     dxgicapture.DxgiError),
            "cpu texture": ((1920, 1080, 87), {"CreateTexture2D staging": E_FAIL},
                            dxgicapture.DxgiError),
            "map": ((1920, 1080, 87), {"Map": _hr(dxgicapture.DXGI_ERROR_DEVICE_REMOVED)},
                    dxgicapture.DxgiError),
        }
        for what, (desktop, fail, error) in cases.items():
            with self.subTest(what):
                world = self.world(desktop=desktop)
                world.frames = ["frame"]
                world.fail = fail
                dup = dxgicapture.Duplication(D1)
                with self.assertRaises(error):
                    dup.grab()
                self.assertFalse(world.held)            # ReleaseFrame always
                self.assertNotIn("frame resource", world.alive())
                self.assertNotIn("desktop texture", world.alive())
                self.assertEqual(self.calls(world, "Unmap"), [])   # only after a good Map
                dup.close()
                self.assertEqual(world.alive(), [])
                self.assertEqual(world.errors, [])

    def test_access_lost_and_other_acquire_errors(self):
        world = self.world()
        world.frames = [_hr(dxgicapture.DXGI_ERROR_ACCESS_LOST),
                        _hr(dxgicapture.DXGI_ERROR_DEVICE_REMOVED)]
        dup = dxgicapture.Duplication(D1)
        with self.assertRaises(dxgicapture.AccessLost):
            dup.grab()
        with self.assertRaises(dxgicapture.DxgiError) as caught:
            dup.grab()
        self.assertNotIsInstance(caught.exception, dxgicapture.AccessLost)
        self.assertIn("DXGI_ERROR_DEVICE_REMOVED", str(caught.exception))
        self.assertEqual(self.calls(world, "ReleaseFrame"), [])   # nothing was acquired
        dup.close()
        self.assertEqual((world.alive(), world.errors), ([], []))

    def test_pointer_only_first_frame_is_not_measured(self):
        # Before the first desktop picture the surface may still be black.
        world = self.world()
        world.frames = ["mouse", "frame"]
        dup = dxgicapture.Duplication(D1)
        self.assertIsNone(dup.grab())
        self.assertEqual(self.calls(world, "CopySubresourceRegion"), [])
        self.assertFalse(world.held)
        self.assertEqual(dup.grab(), bytes([10, 20, 30, 0]) * (W * H))
        self.assertEqual(self.calls(world, "AcquireNextFrame"),
                         [("AcquireNextFrame", 50), ("AcquireNextFrame", 50)])
        dup.close()
        self.assertEqual((world.alive(), world.errors), ([], []))

    def test_no_picture_yet_waits_a_little_each_time(self):
        world = self.world()
        dup = dxgicapture.Duplication(D1)
        self.assertIsNone(dup.grab())
        self.assertIsNone(dup.grab())
        self.assertEqual(self.calls(world, "AcquireNextFrame"),
                         [("AcquireNextFrame", 50), ("AcquireNextFrame", 50)])
        dup.close()
        self.assertEqual((world.alive(), world.errors), ([], []))

    def test_method_signatures(self):
        # Argument counts after the object pointer and the return types, from
        # the SDK headers.
        import ctypes
        self.world()
        api = dxgicapture._setup()
        hr, u32 = ctypes.c_int32, ctypes.c_uint32
        want = {"QueryInterface": (hr, 2), "Release": (u32, 0), "EnumAdapters1": (hr, 2),
                "EnumOutputs": (hr, 2), "OutputGetDesc": (hr, 1), "DuplicateOutput": (hr, 2),
                "AcquireNextFrame": (hr, 3), "ReleaseFrame": (hr, 0),
                "CreateTexture2D": (hr, 3), "CreateShaderResourceView": (hr, 3),
                "TextureGetDesc": (None, 1), "Map": (hr, 5), "Unmap": (None, 2),
                "CopySubresourceRegion": (None, 8), "GenerateMips": (None, 1)}
        self.assertEqual(set(api.methods), set(want))
        for name, (restype, n) in want.items():
            proto = api.methods[name]
            self.assertIs(proto._restype_, restype, name)
            self.assertEqual(len(proto._argtypes_), n + 1, name)
            self.assertIs(proto._argtypes_[0], ctypes.c_void_p, name)
        for name in ("EnumAdapters1", "EnumOutputs", "AcquireNextFrame"):
            self.assertIs(api.methods[name]._argtypes_[1], u32, name)
        self.assertIs(api.methods["Unmap"]._argtypes_[2], u32)            # Subresource
        self.assertIs(api.methods["Map"]._argtypes_[3], ctypes.c_int32)     # D3D11_MAP
        self.assertEqual(len(api.d3d11.D3D11CreateDevice.argtypes), 10)


class RetryTest(unittest.TestCase):
    def test_many_failures_in_a_row_keep_the_longest_wait(self):
        # A display where DXGI never works, all day: the wait stays at the cap
        # (2 ** n grew too big for a float and stopped the display's measuring).
        fc = FastCapture(lambda rect: b"G" * (W * H * 4),
                         mock.Mock(side_effect=dxgicapture.Unsupported("rotated display")),
                         clock=Clock())
        fc._fails[D1] = 5000
        fc._failed[D1] = fc._clock() - dxgicapture.RETRY_MAX_S
        self.assertEqual(fc.capture(D1, R1)[:1], b"G")
        self.assertEqual(fc._fails[D1], 5001)


class OptionTest(unittest.TestCase):
    def test_default_on(self):
        self.assertIs(Options().auto_fast_capture, True)

    def test_bad_values_fall_back(self):
        for bad in (0, 1, "no", None, [], {}, 2.0):
            o = Options.from_dict({"auto_fast_capture": bad})
            self.assertIs(o.auto_fast_capture, True, bad)

    def test_off_is_kept(self):
        c = Config()
        c.options.auto_fast_capture = False
        back = Config.from_json(c.to_json())
        self.assertIs(back.options.auto_fast_capture, False)

    def test_clean_auto_repairs_a_set_value(self):
        o = Options()
        o.auto_fast_capture = "yes"
        o._clean_auto()
        self.assertIs(o.auto_fast_capture, True)



def _gui_ok() -> bool:
    from tests.test_autolevels_config import _gui_ok as ok
    return ok()


@unittest.skipUnless(_gui_ok(), "set CATVISION_GUI_TESTS=1 with tkinter and a display")
class FastCaptureGuiTest(unittest.TestCase):
    """The "Faster screen capture" box on the Tune tab."""

    def _app(self, options):
        from tests.test_autolevels_config import AutoGuiBadConfigTest
        return AutoGuiBadConfigTest._app(self, options)

    def test_box_follows_the_option_and_drives_the_watcher(self):
        app, root = self._app({"auto_fast_capture": False})
        var = app.auto_flag_vars["auto_fast_capture"]
        self.assertIs(var.get(), False)
        self.assertIs(app.auto_watch.fast_capture, False)
        var.set(True)
        app._on_auto_option()
        self.assertIs(app.cfg.options.auto_fast_capture, True)
        self.assertIs(app.auto_watch.fast_capture, True)

    def test_bad_saved_value_is_on(self):
        app, root = self._app({"auto_fast_capture": "off"})
        self.assertIs(app.cfg.options.auto_fast_capture, True)
        self.assertIs(app.auto_flag_vars["auto_fast_capture"].get(), True)
        self.assertIs(app.auto_watch.fast_capture, True)


if __name__ == "__main__":
    unittest.main()
