"""Faster screen capture for auto levels (beta): DXGI Desktop Duplication.

This is the screen-copy route recording apps such as OBS use: Windows hands
over a copy of what it composed for one display.  It never touches the game
(no injection, no game memory, no game process handle).  The graphics card
shrinks the copy (mipmaps), and only a 96 x 54 thumbnail reaches Python; it
is measured for brightness and never saved or sent.

Everything DXGI / Direct3D is created, used and released on the Watcher's own
thread.  When it doesn't work for a display (an HDR or rotated display, a
remote session, an old driver, the secure desktop...), that display uses the
GDI copy (autolevels.gdi_capture) and DXGI is tried again every RETRY_S.

Pure ctypes COM: each method is called through its slot in the object's
vtable.  A wrong slot number would crash the process, so every one used is
listed below with its interface and checked against the Windows SDK headers
(dxgi.h, dxgi1_2.h, d3d11.h).
"""

from __future__ import annotations

import time
from itertools import accumulate
from operator import add
from typing import Callable

from .winutil import IS_WINDOWS

THUMB_W, THUMB_H = 96, 54       # the size autolevels measures (same as autolevels.THUMB_W/H)
RETRY_S = 5.0                   # a display where DXGI failed tries it again after this long...
RETRY_MAX_S = 60.0              # ...twice as long after each failure in a row, up to this
EMPTY_TRIES = 3                 # a new duplication that hands over no picture this many times fails
FIRST_FRAME_MS = 50             # the first picture after opening may need a moment to arrive
FRAME_MS = 0                    # after that: never wait (no new picture = the screen is unchanged)

# HRESULTs (winerror.h), compared as unsigned 32-bit numbers.
S_OK = 0
DXGI_ERROR_INVALID_CALL = 0x887A0001
DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_UNSUPPORTED = 0x887A0004
DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_ERROR_NOT_CURRENTLY_AVAILABLE = 0x887A0022
DXGI_ERROR_ACCESS_LOST = 0x887A0026      # mode change, fullscreen switch, secure desktop
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027     # no new picture since the last one
DXGI_ERROR_SESSION_DISCONNECTED = 0x887A0028   # remote session disconnected
E_ACCESSDENIED = 0x80070005              # DuplicateOutput on the secure desktop (UAC)
E_INVALIDARG = 0x80070057                # DuplicateOutput: this program already duplicates it
_ERROR_NAMES = {
    DXGI_ERROR_INVALID_CALL: "DXGI_ERROR_INVALID_CALL",
    DXGI_ERROR_NOT_FOUND: "DXGI_ERROR_NOT_FOUND",
    DXGI_ERROR_UNSUPPORTED: "DXGI_ERROR_UNSUPPORTED",
    DXGI_ERROR_DEVICE_REMOVED: "DXGI_ERROR_DEVICE_REMOVED",
    DXGI_ERROR_NOT_CURRENTLY_AVAILABLE: "DXGI_ERROR_NOT_CURRENTLY_AVAILABLE",
    DXGI_ERROR_ACCESS_LOST: "DXGI_ERROR_ACCESS_LOST",
    DXGI_ERROR_WAIT_TIMEOUT: "DXGI_ERROR_WAIT_TIMEOUT",
    DXGI_ERROR_SESSION_DISCONNECTED: "DXGI_ERROR_SESSION_DISCONNECTED",
    E_ACCESSDENIED: "E_ACCESSDENIED",
    E_INVALIDARG: "E_INVALIDARG",
}

# Enum values (dxgiformat.h, dxgi.h, d3d11.h, d3dcommon.h).
DXGI_FORMAT_B8G8R8A8_UNORM = 87          # the desktop's normal 8-bit format (HDR desktops differ)
DXGI_MODE_ROTATION_UNSPECIFIED = 0
DXGI_MODE_ROTATION_IDENTITY = 1          # 2-4 = rotated 90 / 180 / 270: those use GDI
D3D_DRIVER_TYPE_UNKNOWN = 0              # required when an adapter is passed explicitly
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
D3D11_USAGE_DEFAULT = 0
D3D11_USAGE_STAGING = 3
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_RESOURCE_MISC_GENERATE_MIPS = 0x1
D3D11_MAP_READ = 1

# Vtable slots.  IUnknown: 0 QueryInterface, 1 AddRef, 2 Release.
QUERY_INTERFACE, RELEASE = 0, 2
# IDXGIObject adds 3 SetPrivateData, 4 SetPrivateDataInterface, 5 GetPrivateData,
# 6 GetParent.  IDXGIFactory: 7 EnumAdapters, 8 MakeWindowAssociation,
# 9 GetWindowAssociation, 10 CreateSwapChain, 11 CreateSoftwareAdapter;
# IDXGIFactory1: 12 EnumAdapters1, 13 IsCurrent.
FACTORY1_ENUM_ADAPTERS1 = 12
# IDXGIAdapter: 7 EnumOutputs, 8 GetDesc, 9 CheckInterfaceSupport; IDXGIAdapter1: 10 GetDesc1.
ADAPTER_ENUM_OUTPUTS = 7
# IDXGIOutput: 7 GetDesc, 8 GetDisplayModeList, 9 FindClosestMatchingMode,
# 10 WaitForVBlank, 11 TakeOwnership, 12 ReleaseOwnership,
# 13 GetGammaControlCapabilities, 14 SetGammaControl, 15 GetGammaControl,
# 16 SetDisplaySurface, 17 GetDisplaySurfaceData, 18 GetFrameStatistics;
# IDXGIOutput1: 19 GetDisplayModeList1, 20 FindClosestMatchingMode1,
# 21 GetDisplaySurfaceData1, 22 DuplicateOutput.
OUTPUT_GET_DESC = 7
OUTPUT1_DUPLICATE_OUTPUT = 22
# IDXGIOutputDuplication (an IDXGIObject): 7 GetDesc, 8 AcquireNextFrame,
# 9 GetFrameDirtyRects, 10 GetFrameMoveRects, 11 GetFramePointerShape,
# 12 MapDesktopSurface, 13 UnMapDesktopSurface, 14 ReleaseFrame.
DUPL_ACQUIRE_NEXT_FRAME = 8
DUPL_RELEASE_FRAME = 14
# ID3D11Device (an IUnknown): 3 CreateBuffer, 4 CreateTexture1D,
# 5 CreateTexture2D, 6 CreateTexture3D, 7 CreateShaderResourceView ...
DEVICE_CREATE_TEXTURE_2D = 5
DEVICE_CREATE_SHADER_RESOURCE_VIEW = 7
# ID3D11DeviceChild: 3 GetDevice, 4 GetPrivateData, 5 SetPrivateData,
# 6 SetPrivateDataInterface.  ID3D11Resource adds 7 GetType,
# 8 SetEvictionPriority, 9 GetEvictionPriority; ID3D11Texture2D: 10 GetDesc.
TEXTURE2D_GET_DESC = 10
# ID3D11DeviceContext (a DeviceChild): 7 VSSetConstantBuffers,
# 8 PSSetShaderResources, 9 PSSetShader, 10 PSSetSamplers, 11 VSSetShader,
# 12 DrawIndexed, 13 Draw, 14 Map, 15 Unmap, 16 PSSetConstantBuffers ...
# 44 RSSetViewports, 45 RSSetScissorRects, 46 CopySubresourceRegion,
# 47 CopyResource, 48 UpdateSubresource, 49 CopyStructureCount,
# 50 ClearRenderTargetView, 51 ClearUnorderedAccessViewUint,
# 52 ClearUnorderedAccessViewFloat, 53 ClearDepthStencilView, 54 GenerateMips.
CONTEXT_MAP = 14
CONTEXT_UNMAP = 15
CONTEXT_COPY_SUBRESOURCE_REGION = 46
CONTEXT_UPDATE_SUBRESOURCE = 48      # self-test only (Duplication.shrink_picture)
CONTEXT_GENERATE_MIPS = 54

IID_IDXGI_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"
IID_IDXGI_OUTPUT1 = "00cddea8-939b-4b83-a340-a685226666cc"
IID_ID3D11_TEXTURE2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"


class DxgiError(OSError):
    """A DXGI / Direct3D call failed (``hr`` is the unsigned HRESULT)."""

    def __init__(self, what: str, hr: int):
        self.hr = hr & 0xFFFFFFFF
        super().__init__(f"{what}: {_ERROR_NAMES.get(self.hr, hex(self.hr))}")


class AccessLost(DxgiError):
    """The duplication must be made again (mode change, fullscreen switch...)."""


class Unsupported(Exception):
    """DXGI works but can't be used for this display (HDR, rotated, not found)."""


# --------------------------------------------------------------------------
# Pure Python parts (tested on every platform)
# --------------------------------------------------------------------------

def mip_size(width: int, height: int, level: int) -> tuple[int, int]:
    """Size of a mip level, as Direct3D makes them (halved, rounded down, at least 1)."""
    return max(1, width >> level), max(1, height >> level)


def choose_mip(width: int, height: int, w: int = THUMB_W, h: int = THUMB_H) -> tuple[int, int, int]:
    """The smallest mip level still at least w x h (so the last shrink is a
    small box average): (level, width, height).  A display smaller than the
    thumbnail uses level 0.  The GDI route (autolevels.gdi_capture) shrinks
    to this same size first and then uses the same resample(), so both
    routes make the same thumbnail."""
    level = 0
    while True:
        nw, nh = mip_size(width, height, level + 1)
        if nw < w or nh < h or (nw, nh) == mip_size(width, height, level):
            break
        level += 1
    return (level, *mip_size(width, height, level))


def _spans(source: int, target: int) -> list[tuple[int, int]]:
    """The source pixels [start, end) that make up each target pixel."""
    out = []
    for i in range(target):
        a = i * source // target
        b = max((i + 1) * source // target, a + 1)
        out.append((a, min(b, source)))
    return out


def resample(data: bytes, pitch: int, width: int, height: int,
             w: int = THUMB_W, h: int = THUMB_H) -> bytes:
    """Box-average a BGRA picture (rows ``pitch`` bytes apart, as the
    graphics card hands them over) to exactly w x h BGRA, top-down rows.
    Each thumbnail pixel is the rounded average of the source pixels it
    covers.  Alpha is left 0 (GDI's is too).  Both capture routes end with
    this, from the same in-between size (choose_mip)."""
    if pitch < width * 4 or len(data) < pitch * (height - 1) + width * 4:
        raise ValueError("picture too small for its size")
    xs = _spans(width, w)
    ys = _spans(height, h)
    out = bytearray(w * h * 4)
    row_bytes = width * 4
    for j, (y0, y1) in enumerate(ys):
        rows = [data[y * pitch: y * pitch + row_bytes] for y in range(y0, y1)]
        tall = y1 - y0
        boxes = [(a, b, (b - a) * tall) for a, b in xs]
        base = j * w * 4
        for c in range(3):                      # blue, green, red
            if tall == 1:
                column = rows[0][c::4]
            elif tall == 2:
                column = list(map(add, rows[0][c::4], rows[1][c::4]))
            else:
                column = list(map(sum, zip(*(r[c::4] for r in rows))))
            p = [0, *accumulate(column)]        # running sums: each box is one subtraction
            out[base + c: base + w * 4: 4] = bytes(
                ((p[b] - p[a]) * 2 + n) // (2 * n) for a, b, n in boxes)   # rounded average
    return bytes(out)


# --------------------------------------------------------------------------
# Choosing the route per display: DXGI when it works, GDI otherwise
# --------------------------------------------------------------------------

class FastCapture:
    """Captures displays with DXGI Desktop Duplication when it works and
    with ``slow`` (GDI) when it doesn't, per display.  Used only on the
    Watcher's thread (the DXGI objects must stay on one thread).

    ``open_fast(display_id, rect)`` returns an object with ``grab()``
    (thumbnail bytes, or None while no picture has arrived yet) and
    ``close()``; it raises when DXGI can't be used."""

    def __init__(self, slow: Callable, open_fast: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._slow = slow
        if open_fast is None and IS_WINDOWS:
            open_fast = Duplication
        self._open = open_fast
        self._clock = clock
        self._fast: dict = {}                  # display id -> open duplication
        self._rects: dict = {}                 # display id -> the rect it was opened for
        self._failed: dict[str, float] = {}    # display id -> when DXGI last failed
        self._fails: dict[str, int] = {}       # display id -> failures in a row
        self._empty: dict[str, int] = {}       # display id -> "no picture yet" in a row
        self.enabled = True                    # Options.auto_fast_capture
        self.routes: dict[str, str] = {}       # display id -> "dxgi" / "gdi" (last capture)
        self.reasons: dict[str, str] = {}      # display id -> why DXGI isn't used

    def capture(self, did: str, rect) -> bytes | None:
        if not self.enabled or self._open is None:
            if self._fast:
                self.close()                   # turned off: let go of the graphics card
            self.routes[did] = "gdi"
            if self._open is not None:
                self.reasons[did] = "turned off"
            return self._slow(rect)
        pixels = self._grab(did, rect)
        if pixels is not None:
            self.routes[did] = "dxgi"
            self.reasons.pop(did, None)
            return pixels
        self.routes[did] = "gdi"
        return self._slow(rect)

    def _grab(self, did: str, rect) -> bytes | None:
        dup = self._fast.get(did)
        if dup is not None and self._rects.get(did) != rect:
            self._drop(did)                    # the display moved or changed size: open again
            dup = None
        for attempt in range(2):               # a lost duplication is made again once right away
            if dup is None:
                failed = self._failed.get(did)
                # (The doubling stops at the cap: 2 ** n of a long day's failures
                # would be too big for a float and stop the display's measuring.)
                doublings = min(self._fails.get(did, 1) - 1, 16)
                wait = min(RETRY_S * 2 ** doublings, RETRY_MAX_S)
                if failed is not None and self._clock() - failed < wait:
                    return None
                try:
                    dup = self._open(did, rect)
                except Exception as exc:
                    self._fail(did, exc)
                    return None
                self._fast[did], self._rects[did] = dup, rect
            try:
                pixels = dup.grab()
            except AccessLost as exc:
                self._drop(did)
                dup = None
                if attempt:
                    self._fail(did, exc)
                continue
            except Exception as exc:
                self._drop(did)
                self._fail(did, exc)
                return None
            if pixels is None:
                # Each try waits FIRST_FRAME_MS: don't keep paying for one that never comes.
                self._empty[did] = self._empty.get(did, 0) + 1
                if self._empty[did] >= EMPTY_TRIES:
                    self._drop(did)
                    self._fail(did, Unsupported("no picture from DXGI"))
                else:
                    self.reasons[did] = "no picture yet"
            else:
                self._fails.pop(did, None)
                self._failed.pop(did, None)
                self._empty.pop(did, None)
            return pixels
        return None

    def _fail(self, did: str, exc: BaseException) -> None:
        self._failed[did] = self._clock()
        self._fails[did] = self._fails.get(did, 0) + 1
        self.reasons[did] = str(exc) or type(exc).__name__

    def _drop(self, did: str) -> None:
        dup = self._fast.pop(did, None)
        self._rects.pop(did, None)
        self._empty.pop(did, None)
        if dup is not None:
            try:
                dup.close()
            except Exception:
                pass

    def keep_only(self, dids) -> None:
        """Let go of displays that are no longer measured."""
        for did in [d for d in self._fast if d not in dids]:
            self._drop(did)

    def close(self) -> None:
        for did in list(self._fast):
            self._drop(did)


# --------------------------------------------------------------------------
# DXGI Desktop Duplication through ctypes COM (Windows only)
# --------------------------------------------------------------------------

_api = None


def _setup():
    """Declare the structures, functions and method types once."""
    global _api
    if _api is not None:
        return _api
    import ctypes
    import uuid

    # Fixed-size types (UINT, BOOL, LONG and enums are all 32-bit on Windows),
    # so the layouts below can be checked anywhere.
    vp = ctypes.c_void_p
    u32, i32 = ctypes.c_uint32, ctypes.c_int32
    hresult = i32                    # not ctypes.HRESULT: failures are handled here, not raised

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", u32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        u = uuid.UUID(text)
        g = GUID()
        g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
        g.Data4[:] = list(u.bytes[8:])
        return g

    class DXGI_OUTPUT_DESC(ctypes.Structure):
        # dxgi.h: WCHAR DeviceName[32]; RECT DesktopCoordinates; BOOL
        # AttachedToDesktop; DXGI_MODE_ROTATION Rotation; HMONITOR Monitor.
        # 96 bytes on 64-bit (88 + the handle, 8-aligned), 92 on 32-bit.
        _fields_ = [("DeviceName", ctypes.c_uint16 * 32), ("DesktopCoordinates", i32 * 4),
                    ("AttachedToDesktop", i32), ("Rotation", i32), ("Monitor", vp)]

        def name(self) -> str:
            return bytes(self.DeviceName).decode("utf-16-le", "replace").split("\0", 1)[0]

    class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
        # dxgi1_2.h: LARGE_INTEGER LastPresentTime, LastMouseUpdateTime; UINT
        # AccumulatedFrames; BOOL RectsCoalesced, ProtectedContentMaskedOut;
        # DXGI_OUTDUPL_POINTER_POSITION {POINT Position; BOOL Visible};
        # UINT TotalMetadataBufferSize, PointerShapeBufferSize.  48 bytes.
        _fields_ = [("LastPresentTime", ctypes.c_int64), ("LastMouseUpdateTime", ctypes.c_int64),
                    ("AccumulatedFrames", u32), ("RectsCoalesced", i32),
                    ("ProtectedContentMaskedOut", i32), ("PointerX", i32), ("PointerY", i32),
                    ("PointerVisible", i32), ("TotalMetadataBufferSize", u32),
                    ("PointerShapeBufferSize", u32)]

    class D3D11_TEXTURE2D_DESC(ctypes.Structure):
        # d3d11.h: UINT Width, Height, MipLevels, ArraySize; DXGI_FORMAT Format;
        # DXGI_SAMPLE_DESC {UINT Count, Quality}; D3D11_USAGE Usage; UINT
        # BindFlags, CPUAccessFlags, MiscFlags.  44 bytes.
        _fields_ = [("Width", u32), ("Height", u32), ("MipLevels", u32), ("ArraySize", u32),
                    ("Format", i32), ("SampleCount", u32), ("SampleQuality", u32),
                    ("Usage", i32), ("BindFlags", u32), ("CPUAccessFlags", u32),
                    ("MiscFlags", u32)]

    class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
        # d3d11.h: void *pData; UINT RowPitch, DepthPitch.  16 bytes on 64-bit.
        _fields_ = [("pData", vp), ("RowPitch", u32), ("DepthPitch", u32)]

    ptr = ctypes.sizeof(vp)
    sizes = {DXGI_OUTPUT_DESC: 88 + ptr, DXGI_OUTDUPL_FRAME_INFO: 48,
             D3D11_TEXTURE2D_DESC: 44, D3D11_MAPPED_SUBRESOURCE: ptr + 8, GUID: 16}
    for struct, size in sizes.items():
        if ctypes.sizeof(struct) != size:     # never call Windows with a wrong layout
            raise Unsupported(f"{struct.__name__} is {ctypes.sizeof(struct)} bytes, not {size}")

    # (CreateShaderResourceView gets a NULL description: a view of the whole
    # texture in its own format, all mip levels, which is what GenerateMips needs.)

    P = ctypes.POINTER
    proto = ctypes.WINFUNCTYPE
    methods = {   # slot -> function type; the object pointer always comes first
        "QueryInterface": proto(hresult, vp, P(GUID), P(vp)),
        "Release": proto(u32, vp),
        "EnumAdapters1": proto(hresult, vp, u32, P(vp)),
        "EnumOutputs": proto(hresult, vp, u32, P(vp)),
        "OutputGetDesc": proto(hresult, vp, P(DXGI_OUTPUT_DESC)),
        "DuplicateOutput": proto(hresult, vp, vp, P(vp)),
        "AcquireNextFrame": proto(hresult, vp, u32, P(DXGI_OUTDUPL_FRAME_INFO), P(vp)),
        "ReleaseFrame": proto(hresult, vp),
        "CreateTexture2D": proto(hresult, vp, P(D3D11_TEXTURE2D_DESC), vp, P(vp)),
        "CreateShaderResourceView": proto(hresult, vp, vp, vp, P(vp)),
        "TextureGetDesc": proto(None, vp, P(D3D11_TEXTURE2D_DESC)),
        "Map": proto(hresult, vp, vp, u32, i32, u32, P(D3D11_MAPPED_SUBRESOURCE)),
        "Unmap": proto(None, vp, vp, u32),
        "CopySubresourceRegion": proto(None, vp, vp, u32, u32, u32, u32, vp, u32, vp),
        "GenerateMips": proto(None, vp, vp),
        "UpdateSubresource": proto(None, vp, vp, u32, vp, vp, u32, u32),
    }

    dxgi = ctypes.WinDLL("dxgi")
    dxgi.CreateDXGIFactory1.restype = hresult
    dxgi.CreateDXGIFactory1.argtypes = [P(GUID), P(vp)]
    d3d11 = ctypes.WinDLL("d3d11")
    d3d11.D3D11CreateDevice.restype = hresult
    d3d11.D3D11CreateDevice.argtypes = [vp, i32, vp, u32, vp, u32, u32, P(vp), P(i32), P(vp)]

    class Api:
        pass
    api = Api()
    api.ctypes, api.vp, api.i32, api.methods = ctypes, vp, i32, methods
    api.dxgi, api.d3d11 = dxgi, d3d11
    api.OUTPUT_DESC, api.FRAME_INFO = DXGI_OUTPUT_DESC, DXGI_OUTDUPL_FRAME_INFO
    api.TEXTURE2D_DESC, api.MAPPED = D3D11_TEXTURE2D_DESC, D3D11_MAPPED_SUBRESOURCE
    api.IID_FACTORY1 = guid(IID_IDXGI_FACTORY1)
    api.IID_OUTPUT1 = guid(IID_IDXGI_OUTPUT1)
    api.IID_TEXTURE2D = guid(IID_ID3D11_TEXTURE2D)
    _api = api
    return api


def _call(api, obj, slot: int, name: str, *args):
    """Call method ``name`` (vtable slot ``slot``) of COM object ``obj``."""
    ctypes = api.ctypes
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return api.methods[name](table[slot])(obj, *args)


def _check(what: str, hr: int) -> None:
    if hr < 0:
        if hr & 0xFFFFFFFF == DXGI_ERROR_ACCESS_LOST:
            raise AccessLost(what, hr)
        raise DxgiError(what, hr)


def _release(api, obj) -> None:
    if obj:
        _call(api, obj, RELEASE, "Release")


def _query(api, obj, iid):
    out = api.vp()
    _check("QueryInterface", _call(api, obj, QUERY_INTERFACE, "QueryInterface",
                                   api.ctypes.byref(iid), api.ctypes.byref(out)))
    return out.value


def find_output(api, display_id: str):
    """(adapter, output, DXGI_OUTPUT_DESC) of the display with this GDI
    device name (\\\\.\\DISPLAY1...), each with its own reference; raises
    Unsupported when no adapter shows it."""
    byref = api.ctypes.byref
    factory = api.vp()
    _check("CreateDXGIFactory1", api.dxgi.CreateDXGIFactory1(byref(api.IID_FACTORY1),
                                                             byref(factory)))
    factory = factory.value
    try:
        for i in range(64):
            adapter = api.vp()
            hr = _call(api, factory, FACTORY1_ENUM_ADAPTERS1, "EnumAdapters1", i, byref(adapter))
            if hr & 0xFFFFFFFF == DXGI_ERROR_NOT_FOUND:
                break
            _check("EnumAdapters1", hr)
            adapter = adapter.value
            keep = False
            try:
                for j in range(64):
                    output = api.vp()
                    hr = _call(api, adapter, ADAPTER_ENUM_OUTPUTS, "EnumOutputs", j, byref(output))
                    if hr & 0xFFFFFFFF == DXGI_ERROR_NOT_FOUND:
                        break
                    _check("EnumOutputs", hr)
                    output = output.value
                    desc = api.OUTPUT_DESC()
                    hr = _call(api, output, OUTPUT_GET_DESC, "OutputGetDesc", byref(desc))
                    if hr >= 0 and desc.name().lower() == display_id.lower():
                        keep = True
                        return adapter, output, desc
                    _release(api, output)
            finally:
                if not keep:
                    _release(api, adapter)
    finally:
        _release(api, factory)
    raise Unsupported("display not found by DXGI")


class Duplication:
    """One display's Desktop Duplication, with the textures that shrink its
    picture on the graphics card.  Raises (DxgiError / Unsupported) when it
    can't be set up; close() releases everything."""

    def __init__(self, display_id: str, rect=None):
        self.api = api = _setup()
        byref = api.ctypes.byref
        self.device = self.context = self.output1 = self.dup = None
        self.mips = self.view = self.staging = None
        self.size = (0, 0)
        self.level, self.mip_w, self.mip_h = 0, 0, 0
        self.last: bytes | None = None
        self.frames = 0          # pictures copied
        self.unchanged = 0       # captures that found no new picture (the screen was unchanged)
        adapter, output, desc = find_output(api, display_id)
        try:
            if desc.Rotation not in (DXGI_MODE_ROTATION_UNSPECIFIED, DXGI_MODE_ROTATION_IDENTITY):
                raise Unsupported("rotated display")
            device, context, level = api.vp(), api.vp(), api.i32()
            _check("D3D11CreateDevice", api.d3d11.D3D11CreateDevice(
                adapter, D3D_DRIVER_TYPE_UNKNOWN, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                None, 0, D3D11_SDK_VERSION, byref(device), byref(level), byref(context)))
            self.device, self.context = device.value, context.value
            self.output1 = _query(api, output, api.IID_OUTPUT1)
            dup = api.vp()
            _check("DuplicateOutput", _call(api, self.output1, OUTPUT1_DUPLICATE_OUTPUT,
                                            "DuplicateOutput", self.device, byref(dup)))
            self.dup = dup.value
        except BaseException:
            self.close()
            raise
        finally:
            _release(api, output)
            _release(api, adapter)

    def _texture(self, width: int, height: int, mips: int, usage: int, bind: int,
                 cpu: int, misc: int):
        api = self.api
        desc = api.TEXTURE2D_DESC()
        desc.Width, desc.Height, desc.MipLevels, desc.ArraySize = width, height, mips, 1
        desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM
        desc.SampleCount, desc.SampleQuality = 1, 0
        desc.Usage, desc.BindFlags, desc.CPUAccessFlags, desc.MiscFlags = usage, bind, cpu, misc
        out = api.vp()
        _check("CreateTexture2D", _call(api, self.device, DEVICE_CREATE_TEXTURE_2D,
                                        "CreateTexture2D", api.ctypes.byref(desc), None,
                                        api.ctypes.byref(out)))
        return out.value

    def _make_textures(self, width: int, height: int) -> None:
        """The mip chain texture (full size, shrunk on the graphics card) and
        the small one the CPU reads, for this picture size."""
        api = self.api
        self._free_textures()
        self.level, self.mip_w, self.mip_h = choose_mip(width, height)
        if self.level:
            self.mips = self._texture(width, height, self.level + 1, D3D11_USAGE_DEFAULT,
                                      D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET,
                                      0, D3D11_RESOURCE_MISC_GENERATE_MIPS)
            view = api.vp()
            _check("CreateShaderResourceView", _call(
                api, self.device, DEVICE_CREATE_SHADER_RESOURCE_VIEW,
                "CreateShaderResourceView", self.mips, None, api.ctypes.byref(view)))
            self.view = view.value
        self.staging = self._texture(self.mip_w, self.mip_h, 1, D3D11_USAGE_STAGING, 0,
                                     D3D11_CPU_ACCESS_READ, 0)
        self.size = (width, height)

    def _free_textures(self) -> None:
        for name in ("staging", "view", "mips"):
            _release(self.api, getattr(self, name))
            setattr(self, name, None)
        self.size = (0, 0)

    def grab(self) -> bytes | None:
        """The display's newest picture as a 96 x 54 BGRA thumbnail.  No new
        picture since the last one (the screen didn't change): the last
        thumbnail again (None before the first).  Raises AccessLost when the
        duplication must be made again, DxgiError / Unsupported otherwise."""
        api = self.api
        byref = api.ctypes.byref
        if not self.dup:                 # closed: never call through a released object
            raise DxgiError("grab after close", DXGI_ERROR_INVALID_CALL)
        info, resource = api.FRAME_INFO(), api.vp()
        wait = FRAME_MS if self.last is not None else FIRST_FRAME_MS
        hr = _call(api, self.dup, DUPL_ACQUIRE_NEXT_FRAME, "AcquireNextFrame", wait,
                   byref(info), byref(resource))
        if hr & 0xFFFFFFFF == DXGI_ERROR_WAIT_TIMEOUT:
            self.unchanged += 1
            return self.last
        _check("AcquireNextFrame", hr)
        try:
            if info.LastPresentTime == 0:
                # Only the mouse pointer changed.  Before the first picture
                # too: then the surface may not hold the desktop yet (it can
                # be black), so wait for a real one rather than measure it.
                self.unchanged += 1
                return self.last
            if not resource.value:
                raise DxgiError("AcquireNextFrame", DXGI_ERROR_INVALID_CALL)
            texture = _query(api, resource.value, api.IID_TEXTURE2D)
            try:
                desc = api.TEXTURE2D_DESC()
                _call(api, texture, TEXTURE2D_GET_DESC, "TextureGetDesc", byref(desc))
                if desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM:
                    raise Unsupported(f"desktop format {desc.Format} (HDR?)")
                if (desc.Width, desc.Height) != self.size:
                    self._make_textures(desc.Width, desc.Height)
                # Copy the picture out, so the frame can be handed back at once.
                target = self.mips if self.level else self.staging
                _call(api, self.context, CONTEXT_COPY_SUBRESOURCE_REGION, "CopySubresourceRegion",
                      target, 0, 0, 0, 0, texture, 0, None)
            finally:
                _release(api, texture)
        finally:
            _release(api, resource.value)
            _call(api, self.dup, DUPL_RELEASE_FRAME, "ReleaseFrame")
        self.last = self.shrink()
        self.frames += 1
        return self.last

    def shrink(self) -> bytes:
        """Shrink the copied picture on the graphics card (mipmaps), read the
        small level back and box-average it to the thumbnail size."""
        data, pitch = self.read_mip()
        return resample(data, pitch, self.mip_w, self.mip_h)

    def read_mip(self) -> tuple[bytes, int]:
        """Shrink the copied picture on the graphics card and read the small
        mip level back: (BGRA rows, bytes from one row to the next).
        (The card rounds to whole levels at each mip level.  Rounding half
        up would measure grainy night scenes about 4% brighter in key than
        GDI does (8% at worst, simulated on night frames): under
        autolevels.NOISE_KEY,
        and reading a bigger level would only halve it at 2-4x the Python
        work, so it isn't guarded against.  The self-test's capture_probes
        reports how the card rounds.)"""
        api = self.api
        byref = api.ctypes.byref
        if not self.context or not self.staging or (self.level and not self.view):
            raise DxgiError("shrink before the first picture", DXGI_ERROR_INVALID_CALL)
        if self.level:
            _call(api, self.context, CONTEXT_GENERATE_MIPS, "GenerateMips", self.view)
            _call(api, self.context, CONTEXT_COPY_SUBRESOURCE_REGION, "CopySubresourceRegion",
                  self.staging, 0, 0, 0, 0, self.mips, self.level, None)
        mapped = api.MAPPED()
        _check("Map", _call(api, self.context, CONTEXT_MAP, "Map", self.staging, 0,
                            D3D11_MAP_READ, 0, byref(mapped)))
        try:
            pitch = mapped.RowPitch
            if not mapped.pData or pitch < self.mip_w * 4:
                raise DxgiError("Map", DXGI_ERROR_INVALID_CALL)
            data = api.ctypes.string_at(mapped.pData, pitch * (self.mip_h - 1) + self.mip_w * 4)
        finally:
            _call(api, self.context, CONTEXT_UNMAP, "Unmap", self.staging, 0)
        return data, pitch

    def shrink_picture(self, bgra: bytes, width: int, height: int) -> tuple[bytes, int]:
        """Self-test only: shrink a known BGRA picture (top-down rows) on
        this graphics card exactly as a screen picture is (mip chain, same
        level choice): (the mip level's BGRA rows, pitch).  Shows how the
        card rounds while averaging.  The textures are made for this size,
        so the next grab() makes its own again."""
        api = self.api
        if (not self.context or width <= 0 or height <= 0
                or len(bgra) < width * height * 4):
            raise DxgiError("shrink_picture", DXGI_ERROR_INVALID_CALL)
        try:
            # (inside the try: a texture made before a failure is released too)
            self._make_textures(width, height)
            if not self.level or not self.mips or not self.view:
                raise Unsupported("picture too small for a mip chain")
            # The copy is the size UpdateSubresource reads (pitch x height),
            # kept alive until it returns.
            buf = api.ctypes.create_string_buffer(bytes(bgra[:width * height * 4]),
                                                  width * height * 4)
            _call(api, self.context, CONTEXT_UPDATE_SUBRESOURCE, "UpdateSubresource",
                  self.mips, 0, None, buf, width * 4, 0)
            return self.read_mip()
        finally:
            self._free_textures()

    def close(self) -> None:
        api = self.api
        self._free_textures()
        for name in ("dup", "output1", "context", "device"):
            _release(api, getattr(self, name))
            setattr(self, name, None)
