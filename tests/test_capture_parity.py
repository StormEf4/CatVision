"""Both screen-capture routes for auto levels must measure the same
brightness: DXGI (graphics card mip chain -> resample) and GDI (HALFTONE
to the same mip size -> the same resample).

Big synthetic frames (up to 3840 x 2160) are simulated in pure Python
quickly by building them from a few shared rows: each shrink step works
once per distinct row (or pair of rows) and reuses the result.  Frames are
grey (blue = green = red), which is all the brightness measure needs.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import unittest
import zlib
from unittest import mock

from catvision import autolevels, selftest
from catvision.autolevels import SHADOW_SHARE, THUMB_H, THUMB_W, measure
from catvision.dxgicapture import choose_mip, mip_size, resample

RESOLUTIONS = [(1024, 768), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160)]


class Frame:
    """A grey picture as a list of rows; equal rows are the same tuple."""

    def __init__(self, width: int, height: int, rows: list[tuple]):
        self.width, self.height, self.rows = width, height, rows
        assert len(rows) == height and all(len(r) == width for r in rows)

    def bgra(self) -> bytes:
        cache: dict = {}
        out = []
        for r in self.rows:
            b = cache.get(id(r))
            if b is None:
                b = cache[id(r)] = b"".join(bytes((v, v, v, 0)) for v in r)
            out.append(b)
        return b"".join(out)


def _intern(rows: list[tuple]) -> list[tuple]:
    seen: dict = {}
    return [seen.setdefault(r, r) for r in rows]


# --- the graphics card: GenerateMips -------------------------------------

def _bilinear_taps(src: int, dst: int) -> list[tuple[int, int, float]]:
    """GenerateMips samples the bigger level bilinearly at each smaller
    texel's centre: (left texel, right texel, weight of the right one).
    Halving an even size gives exactly the 2 x 2 box."""
    taps = []
    for x in range(dst):
        s = (x + 0.5) * src / dst - 0.5
        i = int(s // 1)
        f = s - i
        a, b = min(max(i, 0), src - 1), min(max(i + 1, 0), src - 1)
        taps.append((a, b, f))
    return taps


def mip_chain(frame: Frame, level: int, rounding: str = "even") -> Frame:
    """The mip level as an 8-bit (UNORM) picture, rounded at every level:
    "even" (round half to even, Direct3D's preferred float -> UNORM
    conversion) or "up" (round half up: a card that does this makes each
    level read up to ~1/8 of a level brighter on average)."""
    rnd = round if rounding == "even" else (lambda v: int(v + 0.5))
    width, height, rows = frame.width, frame.height, frame.rows
    for n in range(1, level + 1):
        w, h = mip_size(frame.width, frame.height, n)
        xt, yt = _bilinear_taps(width, w), _bilinear_taps(height, h)
        hcache: dict = {}

        def horiz(r):
            got = hcache.get(id(r))
            if got is None:
                got = hcache[id(r)] = [r[a] * (1 - f) + r[b] * f for a, b, f in xt]
            return got
        vcache: dict = {}
        out = []
        for a, b, f in yt:
            key = (id(rows[a]), id(rows[b]), f)
            got = vcache.get(key)
            if got is None:
                ra, rb = horiz(rows[a]), horiz(rows[b])
                got = vcache[key] = tuple(rnd(p * (1 - f) + q * f) for p, q in zip(ra, rb))
            out.append(got)
        width, height, rows = w, h, _intern(out)
    return Frame(width, height, rows)


# --- GDI: HALFTONE as an exact area average ------------------------------

def _area_weights(src: int, dst: int) -> list[list[tuple[int, float]]]:
    """Each target pixel's share of every source pixel it overlaps."""
    out = []
    for i in range(dst):
        lo, hi = i * src / dst, (i + 1) * src / dst
        taps = []
        for k in range(int(lo), min(src, int(hi) + 1)):
            cover = min(hi, k + 1) - max(lo, k)
            if cover > 1e-12:
                taps.append((k, cover / (hi - lo)))
        out.append(taps)
    return out


def area_average(frame: Frame, w: int, h: int) -> Frame:
    xw, yw = _area_weights(frame.width, w), _area_weights(frame.height, h)
    hcache: dict = {}

    def horiz(r):
        got = hcache.get(id(r))
        if got is None:
            got = hcache[id(r)] = [sum(r[k] * c for k, c in taps) for taps in xw]
        return got
    out = []
    for taps in yw:
        cols = [0.0] * w
        for k, c in taps:
            hr = horiz(frame.rows[k])
            cols = [p + q * c for p, q in zip(cols, hr)]
        out.append(tuple(min(255, int(v + 0.5)) for v in cols))
    return Frame(w, h, _intern(out))


# --- scenes ----------------------------------------------------------------

def _rand(seed: int):
    state = seed

    def nxt(n: int) -> int:
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state % n
    return nxt


def night(width: int, height: int) -> Frame:
    """A dark night raid: near-black ground with a faint gradient, small
    lights (2-4 pixels) here and there and a few brighter windows."""
    rnd = _rand(7)
    bands = [tuple(6 + 10 * b // 11 + (x * 7 + b) % 3 - 1 for x in range(width)) for b in range(12)]
    lights = []
    for _ in range(24):
        r = list(bands[rnd(12)])
        for _ in range(3 + rnd(6)):
            x, size, v = rnd(width - 6), 2 + rnd(3), 150 + rnd(106)
            r[x:x + size] = [v] * size
        lights.append(tuple(r))
    rows = []
    y = 0
    while y < height:
        if rnd(9) == 0:
            r = lights[rnd(len(lights))]
            rows += [r] * min(2 + rnd(3), height - y)
        else:
            r = bands[min(11, 12 * y // height)]
            rows += [r] * min(1 + rnd(5), height - y)
        y = len(rows)
    return Frame(width, height, _intern(rows))


def day(width: int, height: int) -> Frame:
    """A bright day: sky, a textured ground, some dark shade."""
    rnd = _rand(11)
    texture = [tuple(min(255, max(0, base + (17 * x % 23) - 11 + 30 * ((x // 41) % 3 == 0)))
                     for x in range(width)) for base in (95, 120, 140, 70)]
    rows = []
    for y in range(height):
        if y < height * 2 // 5:
            v = 230 - 40 * y // height
            rows.append((v,) * width)
        else:
            rows.append(texture[(y // (3 + rnd(4))) % 4] if rnd(5) else texture[3])
    return Frame(width, height, _intern(rows))


def hud(width: int, height: int) -> Frame:
    """Night with a game HUD: thin bright text strokes and a mid-grey map."""
    base = night(width, height)
    rows = list(base.rows)
    stroke_cache: dict = {}
    for top in range(height - 140, height - 20, 14):
        for dy in range(10):
            y = top + dy
            src = rows[y]
            key = (id(src), dy)
            if key not in stroke_cache:
                r = list(src)
                for x in range(40, min(width, 420)):
                    if (x * 7 + dy * 3) % 5 < 2 and dy % 3 != 2:
                        r[x] = 245
                for x in range(width - 260, width - 40):   # the map
                    r[x] = 90 + (x % 9) * 3
                stroke_cache[key] = tuple(r)
            rows[y] = stroke_cache[key]
    return Frame(width, height, _intern(rows))


SCENES = {"night": night, "day": day, "hud": hud}


def both_routes(frame: Frame, rounding: str = "even") -> dict:
    """The thumbnails both routes make of one frame: DXGI (mip chain ->
    resample), GDI now (HALFTONE to the mip size -> resample) and GDI
    before (HALFTONE straight to the thumbnail)."""
    level, mw, mh = choose_mip(frame.width, frame.height)
    gpu = mip_chain(frame, level, rounding)
    assert (gpu.width, gpu.height) == (mw, mh)
    halftone = area_average(frame, mw, mh)
    return {"dxgi": resample(gpu.bgra(), mw * 4, mw, mh),
            "gdi": resample(halftone.bgra(), mw * 4, mw, mh),
            "gdi_before": area_average(frame, THUMB_W, THUMB_H).bgra()}


def _mean_abs(a: bytes, b: bytes) -> float:
    return sum(abs(p - q) for p, q in zip(a[0::4], b[0::4])) / (len(a) // 4)


class CaptureParityTest(unittest.TestCase):
    def test_area_average_is_the_mip_chain_at_powers_of_two(self):
        frame = night(1024, 768)
        a = mip_chain(frame, 3)
        b = area_average(frame, 128, 96)
        diff = max(abs(p - q) for ra, rb in zip(a.rows, b.rows) for p, q in zip(ra, rb))
        self.assertLessEqual(diff, 1)    # rounding at each mip level only

    def test_both_routes_measure_the_same(self):
        for width, height in RESOLUTIONS:
            for name, scene in SCENES.items():
                with self.subTest(size=(width, height), scene=name):
                    t = both_routes(scene(width, height))
                    a, b = measure(t["dxgi"], THUMB_W), measure(t["gdi"], THUMB_W)
                    info = (a.key, b.key, a.shadows * 255, b.shadows * 255)
                    # Same grid, same last step: within a level or two per pixel
                    # (more only where a mip level has an odd size, 135 -> 67:
                    # the card samples two texels where HALFTONE takes the area).
                    self.assertLess(_mean_abs(t["dxgi"], t["gdi"]), 3.0, info)
                    self.assertLess(abs(a.average - b.average) / a.average, 0.01, info)
                    # The key's shadow part is a whole level (the 25th
                    # percentile): in a dark scene sitting right on a step,
                    # a rounding can move it one level (~4% of the key at
                    # level 8, for any capture), never more.
                    sa, sb = round(a.shadows * 255), round(b.shadows * 255)
                    self.assertLessEqual(abs(sa - sb), 1, info)
                    step = SHADOW_SHARE * abs(math.log(sa / sb))
                    self.assertLess(abs(math.log(a.key / b.key)), 0.015 + step, info)


class GdiCaptureTest(unittest.TestCase):
    def test_shrinks_to_the_mip_size_then_resamples(self):
        asked = []
        frame = night(1920, 1080)

        def shrink(rect, w, h):
            asked.append((w, h))
            return area_average(frame, w, h).bgra()
        with mock.patch.object(autolevels, "gdi_shrink", shrink):
            got = autolevels.gdi_capture((0, 0, 1920, 1080))
        self.assertEqual(asked, [choose_mip(1920, 1080)[1:]])
        mw, mh = asked[0]
        self.assertEqual(got, resample(area_average(frame, mw, mh).bgra(), mw * 4, mw, mh))
        self.assertEqual(len(got), THUMB_W * THUMB_H * 4)

    def test_failed_copy_and_tiny_display(self):
        with mock.patch.object(autolevels, "gdi_shrink", lambda rect, w, h: None):
            self.assertIsNone(autolevels.gdi_capture((0, 0, 1024, 768)))
        tiny = bytes(THUMB_W * THUMB_H * 4)
        with mock.patch.object(autolevels, "gdi_shrink", lambda rect, w, h: tiny):
            self.assertIs(autolevels.gdi_capture((0, 0, THUMB_W, THUMB_H)), tiny)


def _read_png(path: str) -> tuple[int, int, bytes]:
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks[kind] = body
        pos += 12 + n
    w, h, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color) == (8, 2)
    return w, h, zlib.decompress(chunks[b"IDAT"])


class SelftestPicturesTest(unittest.TestCase):
    def test_png_round_trip(self):
        bgra = bytes((10, 20, 30, 0, 40, 50, 60, 0, 70, 80, 90, 0,
                      1, 2, 3, 0, 4, 5, 6, 0, 7, 8, 9, 0))
        with tempfile.TemporaryDirectory() as tmp:
            path = selftest.write_png(os.path.join(tmp, "t.png"), bgra, 3, 2)
            w, h, raw = _read_png(path)
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(raw, b"\x00" + bytes((30, 20, 10, 60, 50, 40, 90, 80, 70))
                         + b"\x00" + bytes((3, 2, 1, 6, 5, 4, 9, 8, 7)))

    def test_capture_parity_report(self):
        frame = night(1024, 768)
        level, mw, mh = choose_mip(1024, 768)
        mid = mip_chain(frame, level).bgra()

        class Dup:
            mip_w, mip_h = mw, mh

            def read_mip(self):
                pitch = mw * 4 + 64          # graphics cards pad rows
                return b"".join(mid[y * mw * 4:(y + 1) * mw * 4] + bytes(64)
                                for y in range(mh)), pitch
        thumb = resample(mid, mw * 4, mw, mh)

        def shrink(rect, w, h):
            return area_average(frame, w, h).bgra()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(autolevels, "gdi_shrink", shrink):
            out = selftest.capture_parity(Dup(), thumb, (0, 0, 1024, 768),
                                          os.path.join(tmp, "selftest-x.json"))
            pictures = sorted(os.listdir(tmp))
        self.assertLess(out["key_diff"] / out["key_dxgi"], 0.015, out)
        self.assertIn("key_diff_before", out)
        self.assertLessEqual(out["mip_pixel_diff"], 1.0)
        self.assertEqual(len(pictures), 5, out)
        self.assertTrue(all(p.startswith("selftest-x.capture-") for p in pictures))

    def test_capture_parity_never_raises(self):
        class Broken:
            mip_w, mip_h = 128, 96

            def read_mip(self):
                raise OSError("lost")

        def shrink(rect, w, h):
            raise OSError("no DC")
        with mock.patch.object(autolevels, "gdi_shrink", shrink):
            out = selftest.capture_parity(Broken(), bytes(THUMB_W * THUMB_H * 4),
                                          (0, 0, 1024, 768), None)
        self.assertIn("dxgi_mip_error", out)
        self.assertIn("gdi_mip_error", out)
        self.assertIn("key_dxgi", out)
        self.assertNotIn("key_diff", out)

    def test_gpu_probe(self):
        # A card that averages exactly: no bias, the same key as the box.
        class Dup:
            level, mip_w, mip_h = 0, 0, 0

            def shrink_picture(self, bgra, width, height):
                self.level, self.mip_w, self.mip_h = choose_mip(width, height)
                mid = resample(bgra, width * 4, width, height, self.mip_w, self.mip_h)
                return mid, self.mip_w * 4
        with tempfile.TemporaryDirectory() as tmp:
            out = selftest.gpu_probe(Dup(), os.path.join(tmp, "selftest-x.json"))
            pictures = sorted(os.listdir(tmp))
        self.assertEqual((out["mip_level"], out["mip_size"]), (3, [128, 96]))
        self.assertLess(abs(out["key_gpu"] / out["key_box"] - 1), 0.015, out)
        self.assertLess(abs(out["mip_bias_levels"]), 0.1)
        self.assertEqual(pictures, ["selftest-x.probe-gpu-mip.png", "selftest-x.probe-gpu.png"])

    def test_probe_picture(self):
        pic = selftest.probe_picture(1024, 768)
        self.assertEqual(len(pic), 1024 * 768 * 4)
        m = measure(resample(pic, 1024 * 4, 1024, 768), THUMB_W)
        self.assertLess(m.key, 0.2)     # a night-like picture
        self.assertEqual(pic, selftest.probe_picture(1024, 768))   # the same every time

    def test_mip_rounding(self):
        # Two 2 x 2 boxes; blue, green, red sums 2, 6, 8 and 7, 5, 9: halves
        # (2, 6) show the rounding, quarters (7, 5, 9) round to nearest.
        top = bytes((0, 0, 0, 9, 1, 1, 1, 9, 3, 2, 0, 9, 3, 1, 9, 9))
        bottom = bytes((1, 2, 3, 9, 0, 3, 4, 9, 1, 2, 0, 9, 0, 0, 0, 9))
        pic = top + bottom
        self.assertEqual(selftest._mip_down(pic, 4, 2, "half_up"),
                         bytes((1, 2, 2, 0, 2, 1, 2, 0)))
        self.assertEqual(selftest._mip_down(pic, 4, 2, "half_even"),
                         bytes((0, 2, 2, 0, 2, 1, 2, 0)))
        self.assertEqual(selftest._mip_down(pic, 4, 2, "down"),
                         bytes((0, 1, 2, 0, 1, 1, 2, 0)))
        # Odd sizes drop the last column / row, as Direct3D's mip sizes do.
        self.assertEqual(len(selftest._mip_down(bytes(7 * 5 * 4), 7, 5, "half_up")), 3 * 2 * 4)
        self.assertEqual(len(selftest.mip_chain(bytes(64 * 32 * 4), 64, 32, 3, "down")),
                         8 * 4 * 4)

    def test_gpu_probe_names_the_cards_rounding(self):
        # A card that rounds half up at every level: the probe says so, and
        # its dark grain measures brighter than an exact average.
        for rounding in ("half_up", "half_even", "down"):
            class Dup:
                level, mip_w, mip_h = 0, 0, 0

                def shrink_picture(self, bgra, width, height, r=rounding):
                    self.level, self.mip_w, self.mip_h = choose_mip(width, height)
                    mid = selftest.mip_chain(bgra, width, height, self.level, r)
                    pitch = self.mip_w * 4 + 64          # rows padded, as a card's are
                    return b"".join(mid[y * self.mip_w * 4:(y + 1) * self.mip_w * 4] + bytes(64)
                                    for y in range(self.mip_h)), pitch
            out = selftest.gpu_probe(Dup(), None)
            self.assertEqual(out["rounding"], rounding, out)
            self.assertEqual(out["rounding_differs"][rounding], 0.0)
            self.assertGreater(min(v for k, v in out["rounding_differs"].items()
                                   if k != rounding), 0.03)
            if rounding == "half_up":
                self.assertGreater(out["mip_bias_levels"], 0.1)
            self.assertEqual(out["pictures"], [])

    def test_capture_probes_never_raise(self):
        def broken(*args, **kwargs):
            raise OSError("no")

        class Gamma:
            def monitors(self):
                return [mock.Mock(id="D1", rect=(0, 0, 1024, 768))]
        closed = []

        class Dup:
            dup = device = "x"

            def __init__(self, *args):
                pass

            def shrink_picture(self, *args):
                raise OSError("card")

            def close(self):
                closed.append(True)
                self.dup = self.device = None
        from catvision import dxgicapture
        with mock.patch.object(selftest, "halftone_probe", broken), \
                mock.patch.object(dxgicapture, "Duplication", broken):
            out = selftest.capture_probes(Gamma(), None)
        self.assertIn("error", out["halftone_probe"])
        self.assertIn("skipped", out["gpu_probe"])
        with mock.patch.object(selftest, "halftone_probe", broken), \
                mock.patch.object(dxgicapture, "Duplication", Dup):
            out = selftest.capture_probes(Gamma(), None)
        self.assertIn("error", out["gpu_probe"])
        self.assertEqual((closed, out["released"]), ([True], True))


if __name__ == "__main__":
    unittest.main()
