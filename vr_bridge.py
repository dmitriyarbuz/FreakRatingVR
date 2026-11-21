"""
FreakRating -> SteamVR bridge.

Captures the game's SBS window with dxcam, splits it into two eye textures and
submits them to the OpenVR compositor as the scene. Streams HMD pose to the
page over WebSocket and maps VR controller state to CDP keyboard/mouse events.

Usage: python vr_bridge.py [duration_seconds]
"""

import asyncio
import ctypes
import json
import math
import os
import sys
import threading
import time
import urllib.request

import cv2
import dxcam
import glfw
import numpy as np
import openvr
import websocket as ws_client
import websockets
from OpenGL.GL import *

CDP_PORT = int(os.environ.get("CDP_PORT", "9223"))
WS_PORT = 8777
# Windowed capture size. Aspect is kept ~2.545 (matches the game page) so the
# browser scales uniformly and the picture is not distorted. Smaller = faster.
# Capture the maximized window and downscale the whole SBS frame once before splitting.
EYE_W = int(os.environ.get("VR_EYE_W", "860"))
EYE_H = int(os.environ.get("VR_EYE_H", "675"))


class Struct(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(Struct)]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(Struct)]
user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(Point)]


def get_game_client_rect(hwnd):
    r = Struct()
    user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    p = Point()
    p.x, p.y = 0, 0
    user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(p))
    return (p.x, p.y, p.x + (r.right - r.left), p.y + (r.bottom - r.top))


def make_texture(w, h):
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
    glBindTexture(GL_TEXTURE_2D, 0)
    glFinish()
    return tex


def upload_texture(tex, arr):
    h, w = arr.shape[:2]
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGB, GL_UNSIGNED_BYTE,
                    arr.ctypes.data_as(ctypes.c_void_p))
    glBindTexture(GL_TEXTURE_2D, 0)


def quat_to_list(q):
    return [float(q[0]), float(q[1]), float(q[2]), float(q[3])]


def quat_pos_to_matrix(q, p):
    """Build an OpenVR HmdMatrix34_t from a quaternion (x,y,z,w) and position."""
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    m = openvr.HmdMatrix34_t()
    m.m[0][0] = 1.0 - 2.0 * (yy + zz)
    m.m[0][1] = 2.0 * (xy - wz)
    m.m[0][2] = 2.0 * (xz + wy)
    m.m[0][3] = float(p[0])
    m.m[1][0] = 2.0 * (xy + wz)
    m.m[1][1] = 1.0 - 2.0 * (xx + zz)
    m.m[1][2] = 2.0 * (yz - wx)
    m.m[1][3] = float(p[1])
    m.m[2][0] = 2.0 * (xz - wy)
    m.m[2][1] = 2.0 * (yz + wx)
    m.m[2][2] = 1.0 - 2.0 * (xx + yy)
    m.m[2][3] = float(p[2])
    return m


class CDPInput:
    """Thin CDP keyboard/mouse injector using websocket-client."""

    def __init__(self):
        targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % CDP_PORT))
        page = next((t for t in targets if t.get("type") == "page" and t.get("url", "").startswith("http")), None)
        if not page:
            raise RuntimeError("no game page target")
        self.ws = ws_client.create_connection(page["webSocketDebuggerUrl"], timeout=5)
        self.next_id = 1
        self.held = set()
        self.mouse_held = set()
        self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})

    def send(self, method, params):
        cmd = {"id": self.next_id, "method": method, "params": params}
        self.next_id += 1
        try:
            self.ws.send(json.dumps(cmd))
        except Exception:
            pass

    def key(self, name, code, vk, down):
        typ = "keyDown" if down else "keyUp"
        self.send("Input.dispatchKeyEvent", {
            "type": typ,
            "key": name,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        })

    def set_held(self, name, spec, active):
        if active and name not in self.held:
            self.held.add(name)
            self.key(spec[0], spec[1], spec[2], True)
        elif not active and name in self.held:
            self.held.discard(name)
            self.key(spec[0], spec[1], spec[2], False)

    def unlock_mouse(self):
        self.send("Runtime.evaluate", {
            "expression": "if(document.pointerLockElement) document.exitPointerLock(); 'ok'",
            "userGesture": True, "returnByValue": True,
        })

    def mouse_button(self, button, down):
        self.send("Input.dispatchMouseEvent", {
            "type": "mousePressed" if down else "mouseReleased",
            "x": 1321, "y": 519, "button": button, "clickCount": 1,
        })

    def wheel(self, delta_y):
        self.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": 1321, "y": 519, "deltaX": 0, "deltaY": delta_y,
        })

    def set_mouse(self, button, active):
        if active and button not in self.mouse_held:
            self.mouse_held.add(button)
            self.mouse_button(button, True)
        elif not active and button in self.mouse_held:
            self.mouse_held.discard(button)
            self.mouse_button(button, False)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


KEY_SPECS = {
    "w": ("w", "KeyW", 87),
    "a": ("a", "KeyA", 65),
    "s": ("s", "KeyS", 83),
    "d": ("d", "KeyD", 68),
    "shift": ("Shift", "ShiftLeft", 16),
    "space": (" ", "Space", 32),
    "ctrl": ("Control", "ControlLeft", 17),
    "e": ("e", "KeyE", 69),
    "x": ("x", "KeyX", 88),
    "g": ("g", "KeyG", 71),
    "f": ("f", "KeyF", 70),
    "1": ("1", "Digit1", 49),
    "2": ("2", "Digit2", 50),
    "3": ("3", "Digit3", 51),
    "4": ("4", "Digit4", 52),
    "escape": ("Escape", "Escape", 27),
}

BUTTON_MASKS = {
    "system": 1 << openvr.k_EButton_System,
    "menu": 1 << openvr.k_EButton_ApplicationMenu,
    "grip": 1 << openvr.k_EButton_Grip,
    "a": 1 << openvr.k_EButton_A,
    "touchpad": 1 << openvr.k_EButton_SteamVR_Touchpad,
    "trigger": 1 << openvr.k_EButton_SteamVR_Trigger,
}


# --- small quaternion helpers (quaternions are [x, y, z, w]) ---
def mat_to_pq(m):
    """Convert a 3x4 OpenVR matrix (m[row][col]) into position + quaternion."""
    px, py, pz = m[0][3], m[1][3], m[2][3]
    r00, r01, r02 = m[0][0], m[0][1], m[0][2]
    r10, r11, r12 = m[1][0], m[1][1], m[1][2]
    r20, r21, r22 = m[2][0], m[2][1], m[2][2]
    tr = r00 + r11 + r22
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        qw, qx, qy, qz = 0.25 * s, (r21 - r12) / s, (r02 - r20) / s, (r10 - r01) / s
    elif r00 > r11 and r00 > r22:
        s = math.sqrt(1.0 + r00 - r11 - r22) * 2
        qw, qx, qy, qz = (r21 - r12) / s, 0.25 * s, (r01 + r10) / s, (r02 + r20) / s
    elif r11 > r22:
        s = math.sqrt(1.0 + r11 - r00 - r22) * 2
        qw, qx, qy, qz = (r02 - r20) / s, (r01 + r10) / s, 0.25 * s, (r12 + r21) / s
    else:
        s = math.sqrt(1.0 + r22 - r00 - r11) * 2
        qw, qx, qy, qz = (r10 - r01) / s, (r02 + r20) / s, (r12 + r21) / s, 0.25 * s
    return [px, py, pz], [qx, qy, qz, qw]


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ]


def qinv(q):
    return [-q[0], -q[1], -q[2], q[3]]


def qrot(q, v):
    qx, qy, qz, qw = q
    tx = 2 * (qy * v[2] - qz * v[1])
    ty = 2 * (qz * v[0] - qx * v[2])
    tz = 2 * (qx * v[1] - qy * v[0])
    return [
        v[0] + qw * tx + qy * tz - qz * ty,
        v[1] + qw * ty + qz * tx - qx * tz,
        v[2] + qw * tz + qx * ty - qy * tx,
    ]


class PoseStream:
    def __init__(self):
        self.shared = {"pose": None}
        self.clients = set()
        # Exact camera pose the page used for its most recent render, so the
        # submitted texture can be tagged with the pose it was rendered from.
        self.render_pose = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    async def _handler(self, ws):
        self.clients.add(ws)

        async def receiver():
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    rp = data.get("renderPose")
                    if rp and rp.get("q") and rp.get("p"):
                        self.render_pose = (rp["q"], rp["p"], time.time())
                except Exception:
                    pass

        recv_task = asyncio.create_task(receiver())
        try:
            while True:
                pose = self.shared["pose"]
                if pose:
                    await ws.send(json.dumps({"pose": pose}))
                await asyncio.sleep(1 / 60.0)
        except Exception:
            return
        finally:
            recv_task.cancel()
            self.clients.discard(ws)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        async def runner():
            async with websockets.serve(self._handler, "127.0.0.1", WS_PORT) as server:
                self.server = server
                await asyncio.Future()

        loop.run_until_complete(runner())

    def update(self, pose):
        self.shared["pose"] = pose

    def get_render_pose(self):
        rp = self.render_pose
        if rp and time.time() - rp[2] < 0.5:
            return (rp[0], rp[1])
        return None

    def send_ui_mode(self, mode):
        async def broadcast():
            for client in list(self.clients):
                try:
                    await client.send(json.dumps({"uiMode": mode}))
                except Exception:
                    pass
        try:
            asyncio.run_coroutine_threadsafe(broadcast(), self.loop)
        except Exception:
            pass

    def send_hands(self, hands):
        async def broadcast():
            for client in list(self.clients):
                try:
                    await client.send(json.dumps({"hands": hands}))
                except Exception:
                    pass
        try:
            asyncio.run_coroutine_threadsafe(broadcast(), self.loop)
        except Exception:
            pass



def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0

    # --- window / capture ---
    # Find the FreakRating main window handle via PowerShell-free ctypes EnumWindows.
    hwnd = None
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]

    def enum_cb(h, lparam):
        nonlocal hwnd
        length = user32.GetWindowTextLengthW(h)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(h, buf, length + 1)
        title = buf.value
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        if cls.value != "Chrome_WidgetWin_1" or "FreakRating" not in title:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if not pid.value:
            return True
        # Only the actual game process — a Chrome tab titled "FreakRatingVR"
        # must not be mistaken for the game window.
        try:
            import psutil
            pname = psutil.Process(pid.value).name().lower()
        except Exception:
            pname = ""
        if pname != "freakrating.exe":
            return True
        hwnd = h
        print("game window:", hwnd, repr(title), "pid", pid.value, flush=True)
        return False

    user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
    if not hwnd:
        print("FreakRating window not found")
        return 1
    # Size the window so the game's internal canvas is ~2x the eye texture
    # (canvas runs at 0.75x the client size, so 2293x900 -> 1720x676 canvas).
    WIN_W = int(os.environ.get("VR_WIN_W", "2293"))
    WIN_H = int(os.environ.get("VR_WIN_H", "900"))
    if WIN_W > 0 and WIN_H > 0:
        try:
            user32.ShowWindow(ctypes.c_void_p(hwnd), 1)  # SW_SHOWNORMAL
            time.sleep(0.2)
            # Recompute borders AFTER ShowWindow: a restored window gets its
            # title bar back, changing the client offset by ~30px.
            wr = Struct()
            user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(wr))
            cr = get_game_client_rect(hwnd)
            bw = (wr.right - wr.left) - (cr[2] - cr[0])
            bh = (wr.bottom - wr.top) - (cr[3] - cr[1])
            user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 100, 100,
                                WIN_W + bw, WIN_H + bh, 0x40 | 0x4 | 0x10)
        except Exception as exc:
            print("window resize failed:", exc, flush=True)
    else:
        user32.ShowWindow(ctypes.c_void_p(hwnd), 3)  # maximize
    time.sleep(0.6)
    rect = get_game_client_rect(hwnd)
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    print("capture region:", rect, "size:", width, "x", height, flush=True)
    cam = dxcam.create(region=rect)
    capture_rect = rect

    # --- OpenGL ---
    if not glfw.init():
        print("glfw init failed")
        return 1
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    window = glfw.create_window(64, 64, "vr bridge", None, None)
    if not window:
        print("glfw window failed")
        glfw.terminate()
        return 1
    glfw.make_context_current(window)

    tex_l = make_texture(EYE_W, EYE_H)
    tex_r = make_texture(EYE_W, EYE_H)
    print("eye texture:", EYE_W, "x", EYE_H, "(source %d x %d)" % (width, height), flush=True)

    # --- OpenVR ---
    openvr.init(openvr.VRApplication_Scene)
    compositor = openvr.VRCompositor()
    compositor.setTrackingSpace(openvr.TrackingUniverseStanding)
    try:
        compositor.forceInterleavedReprojectionOn(False)
    except Exception:
        pass
    system = openvr.VRSystem()
    print("HMD:", system.getStringTrackedDeviceProperty(0, openvr.Prop_ModelNumber_String), flush=True)

    frame_timing = openvr.Compositor_FrameTiming()
    frame_timing.m_nSize = ctypes.sizeof(openvr.Compositor_FrameTiming)
    frame_timing_fn = compositor.function_table.getFrameTiming

    # Pose-tagged eye textures: the compositor uses the pose the frame was
    # actually rendered with and reprojects (timewarp) to the display pose.
    tex_left = openvr.VRTextureWithPose_t()
    tex_left.handle = ctypes.c_void_p(int(tex_l))
    tex_left.eType = openvr.TextureType_OpenGL
    tex_left.eColorSpace = openvr.ColorSpace_Gamma
    tex_right = openvr.VRTextureWithPose_t()
    tex_right.handle = ctypes.c_void_p(int(tex_r))
    tex_right.eType = openvr.TextureType_OpenGL
    tex_right.eColorSpace = openvr.ColorSpace_Gamma

    # Normal bounds (the frame is flipped in numpy).
    bounds = openvr.VRTextureBounds_t()
    bounds.uMin = 0.0
    bounds.vMin = 0.0
    bounds.uMax = 1.0
    bounds.vMax = 1.0

    poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()
    game_poses = (openvr.TrackedDevicePose_t * openvr.k_unMaxTrackedDeviceCount)()

    stream = PoseStream()
    stream.start()
    cdp = CDPInput()

    start = time.time()
    frames = 0
    submit_errors = 0
    last_key_check = 0.0
    menu_prev = False
    right_stick_prev = False
    grip_r_prev = False
    grip_l_prev = False
    HOLD_THRESHOLD = 0.35
    x_prev = False
    x_press_t = 0.0
    x_hold = False
    b_prev = False
    b_press_t = 0.0
    b_hold = False
    ui_mode = 0
    body_yaw = 0.0
    last_turn = time.time()
    TURN_SPEED = 3.5  # radians per second at full stick (right stick)
    # By default DO NOT force the game to the foreground. Forcing focus steals
    # the user's mouse cursor every 2s, so it is opt-in via VR_KEEP_FOCUS=1.
    KEEP_FOCUS = os.environ.get("VR_KEEP_FOCUS", "0") == "1"
    last_focus = 0.0
    last_report = time.time()
    perf = {"poses": 0.0, "grab": 0.0, "resize": 0.0, "upload": 0.0, "submit": 0.0, "total": 0.0, "n": 0}
    # History of head poses actually sent to the page. The captured frame was
    # rendered ~1 bridge frame ago, so tag the texture with that older pose and
    # let the compositor reproject it to the current display pose.
    pose_hist = []
    POSE_DELAY = int(os.environ.get("VR_POSE_DELAY", "1"))
    TAG_POSE = os.environ.get("VR_TAG_POSE", "1") != "0"
    # If set, pace the loop to a fixed rate. 30 is an exact divisor of the
    # PICO stream's 60 Hz, giving even 2:1 frame repeats instead of judder.
    TARGET_FPS = float(os.environ.get("VR_TARGET_FPS", "0"))
    CAPTURE_FPS = float(os.environ.get("VR_CAPTURE_FPS", "0"))
    # Debug: submit a synthetic scrolling test pattern instead of the game capture.
    # Lets us tell whether judder comes from the submit/pose/stream pipeline or
    # from the game's own irregular frame pacing.
    SYNTH = os.environ.get("VR_SYNTH", "0") != "0"
    capture_dt = 1.0 / CAPTURE_FPS if CAPTURE_FPS > 0 else 0
    last_capture = 0.0
    have_tex = False
    capture_pose = None
    next_tick = time.perf_counter()
    print("bridge running for %.1fs (pose-tagged submit: %s, delay %d, submitFPS=%s captureFPS=%s)" % (
        duration, TAG_POSE, POSE_DELAY, TARGET_FPS, CAPTURE_FPS), flush=True)

    while time.time() - start < duration:
        if TARGET_FPS > 0:
            nowp = time.perf_counter()
            if nowp < next_tick:
                time.sleep(next_tick - nowp)
            next_tick += 1.0 / TARGET_FPS
            # Do NOT clamp to "now": allow catch-up so pacing stays even after
            # a slow capture instead of inheriting the game's irregular cadence.
        t0 = time.time()
        hand_poses = {}

        t_poses = time.perf_counter()
        compositor.waitGetPoses(poses, game_poses)
        perf["poses"] += time.perf_counter() - t_poses

        # HMD pose -> page
        hmd = poses[0]
        if hmd.bPoseIsValid:
            cur_p, cur_q = mat_to_pq(hmd.mDeviceToAbsoluteTracking.m)
            stream.update({"q": cur_q, "p": cur_p, "yaw": body_yaw})
            pose_hist.append((cur_q, cur_p))
            if len(pose_hist) > 16:
                pose_hist.pop(0)

        # keep the game window in front AND at the capture size so nothing
        # occludes the frame and dxcam keeps grabbing the intended region.
        if t0 - last_focus > 2.0:
            last_focus = t0
            if KEEP_FOCUS:
                try:
                    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
                except Exception:
                    pass
            try:
                if WIN_W > 0 and WIN_H > 0:
                    wr2 = Struct()
                    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(wr2))
                    cr2 = get_game_client_rect(hwnd)
                    bw2 = (wr2.right - wr2.left) - (cr2[2] - cr2[0])
                    bh2 = (wr2.bottom - wr2.top) - (cr2[3] - cr2[1])
                    if (cr2[2] - cr2[0]) != WIN_W or (cr2[3] - cr2[1]) != WIN_H:
                        user32.ShowWindow(ctypes.c_void_p(hwnd), 1)
                        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 100, 100,
                                            WIN_W + bw2, WIN_H + bh2, 0x40 | 0x4 | 0x10)
                        cr2 = get_game_client_rect(hwnd)
                # Always re-sync the capture region with the live client rect.
                # The window position can shift (title bar, restore) after startup.
                if cr2 != capture_rect:
                    try:
                        cam.release()
                    except Exception:
                        pass
                    cam = dxcam.create(region=cr2)
                    capture_rect = cr2
                    print("capture region updated:", cr2, flush=True)
            except Exception:
                pass

        # controller input (~30 Hz)
        if t0 - last_key_check > 1.0 / 30.0:
            last_key_check = t0
            now_btn = time.time()
            a_right = x_left = b_right = y_left = False
            trig_r = trig_l = False
            grip_r = grip_l = False
            touch_r = touch_l = False
            for dev in (1, 2, 3, 4):
                if system.getTrackedDeviceClass(dev) != openvr.TrackedDeviceClass_Controller:
                    continue
                if not system.isTrackedDeviceConnected(dev):
                    continue
                res, state, pose = system.getControllerStateWithPose(
                    openvr.TrackingUniverseStanding, dev
                )
                if not res:
                    continue
                pressed = state.ulButtonPressed
                # Touchpad reports the thumbstick on rAxis[0]
                ax = state.rAxis[0].x
                ay = state.rAxis[0].y
                is_left = dev in (1, 3)

                # Controller pose relative to the head, expressed in head-local
                # space, so the page can parent hands to the camera (see vr_inject.js).
                if pose.bPoseIsValid and hmd.bPoseIsValid:
                    cp, cq = mat_to_pq(pose.mDeviceToAbsoluteTracking.m)
                    qh_inv = qinv(cur_q)
                    rel_p = qrot(qh_inv, [cp[0] - cur_p[0], cp[1] - cur_p[1], cp[2] - cur_p[2]])
                    rel_q = qmul(qh_inv, cq)
                    hand_poses["left" if is_left else "right"] = {
                        "p": rel_p, "q": rel_q, "valid": True,
                    }
                else:
                    hand_poses["left" if is_left else "right"] = {"valid": False}
                if is_left:
                    cdp.set_held("w", KEY_SPECS["w"], ay > 0.35)
                    cdp.set_held("s", KEY_SPECS["s"], ay < -0.35)
                    cdp.set_held("a", KEY_SPECS["a"], ax < -0.35)
                    cdp.set_held("d", KEY_SPECS["d"], ax > 0.35)
                else:
                    # Right stick X rotates the whole body (smooth turn)
                    dt = min(0.1, now_btn - last_turn)
                    last_turn = now_btn
                    if abs(ax) > 0.15:
                        body_yaw -= ax * TURN_SPEED * dt
                bit7 = bool(pressed & (1 << 7))   # A on right, X on left
                bit1 = bool(pressed & (1 << 1))   # B on right, Y on left
                bit32 = bool(pressed & (1 << 32)) # thumbstick click
                bit33 = bool(pressed & (1 << 33)) # trigger
                grip_now = bool(pressed & (1 << 2)) or bool(pressed & (1 << 34))
                if is_left:
                    x_left = x_left or bit7
                    y_left = y_left or bit1
                    touch_l = touch_l or bit32
                    trig_l = trig_l or bit33
                    grip_l = grip_l or grip_now
                else:
                    a_right = a_right or bit7
                    b_right = b_right or bit1
                    touch_r = touch_r or bit32
                    trig_r = trig_r or bit33
                    grip_r = grip_r or grip_now

            # A (right bit7) = jump
            cdp.set_held("space", KEY_SPECS["space"], a_right)
            # Left thumbstick click = sprint (Shift)
            cdp.set_held("shift", KEY_SPECS["shift"], touch_l)
            # Triggers: right = LMB, left = RMB
            cdp.set_mouse("left", trig_r)
            cdp.set_mouse("right", trig_l)

            # Right thumbstick click cycles UI: 0=dual, 1=original, 2=none
            if touch_r and not right_stick_prev:
                ui_mode = (ui_mode + 1) % 3
                stream.send_ui_mode(ui_mode)
                print("UI mode ->", ui_mode, flush=True)
            right_stick_prev = touch_r

            # Grip = inventory slot via mouse wheel (rising edge only)
            if grip_r and not grip_r_prev:
                cdp.wheel(120)   # right grip: slot down
            if grip_l and not grip_l_prev:
                cdp.wheel(-120)  # left grip: slot up
            grip_r_prev = grip_r
            grip_l_prev = grip_l

            # Y (left bit1) = menu (Escape + unlock)
            if y_left and not menu_prev:
                cdp.set_held("escape", KEY_SPECS["escape"], True)
                cdp.unlock_mouse()
            elif not y_left and menu_prev:
                cdp.set_held("escape", KEY_SPECS["escape"], False)
            menu_prev = y_left

            # B (right bit1): TAP = emote (G pulse), HOLD = emote menu (G held)
            if b_right and not b_prev:
                b_press_t = now_btn
                b_hold = False
            if b_right and not b_hold and (now_btn - b_press_t) > HOLD_THRESHOLD:
                cdp.set_held("g", KEY_SPECS["g"], True)
                b_hold = True
            if not b_right and b_prev:
                if b_hold:
                    cdp.set_held("g", KEY_SPECS["g"], False)
                else:
                    cdp.key("g", "KeyG", 71, True)
                    cdp.key("g", "KeyG", 71, False)
            b_prev = b_right

            # X (left bit7): TAP = interact (E), HOLD = chat (X key held)
            if x_left and not x_prev:
                x_press_t = now_btn
                x_hold = False
            if x_left and not x_hold and (now_btn - x_press_t) > HOLD_THRESHOLD:
                cdp.set_held("x", KEY_SPECS["x"], True)
                x_hold = True
            if not x_left and x_prev:
                if x_hold:
                    cdp.set_held("x", KEY_SPECS["x"], False)
                else:
                    cdp.key("e", "KeyE", 69, True)
                    cdp.key("e", "KeyE", 69, False)
            x_prev = x_left
            if hand_poses:
                stream.send_hands(hand_poses)

        # capture (optionally at its own rate) + submit
        t_cap = time.perf_counter()
        if (not capture_dt) or (t_cap - last_capture >= capture_dt):
            if SYNTH:
                # Static grid (reference for head-turn judder) + a bright bar
                # that scrolls at a constant rate (reference for frame pacing).
                sh, sw = height // 2, width // 2
                frame = np.zeros((sh, sw, 3), dtype=np.uint8)
                frame[:, ::80] = (0, 0, 90)       # vertical grid
                frame[::80, :] = (0, 0, 90)       # horizontal grid
                period = 2.0
                frac = ((time.perf_counter() - start) % period) / period
                bx = int(frac * (sw - 120))
                frame[sh//2 - 60:sh//2 + 60, bx:bx + 120] = (0, 255, 255)
            else:
                frame = cam.grab(new_frame_only=True)
            if frame is not None:
                t_rsz = time.perf_counter()
                small = cv2.resize(frame, (EYE_W * 2, EYE_H), interpolation=cv2.INTER_LINEAR)
                left_img = np.ascontiguousarray(small[::-1, :EYE_W])
                right_img = np.ascontiguousarray(small[::-1, EYE_W:])
                t_upl = time.perf_counter()
                upload_texture(tex_l, left_img)
                upload_texture(tex_r, right_img)
                last_capture = t_cap
                have_tex = True
                if pose_hist:
                    idx = max(0, len(pose_hist) - 1 - POSE_DELAY)
                    capture_pose = pose_hist[idx]
                # Prefer the exact camera pose the page rendered this frame from.
                rp = stream.get_render_pose()
                if rp:
                    capture_pose = rp
                perf["grab"] += t_rsz - t_cap
                perf["resize"] += t_upl - t_rsz
                perf["upload"] += time.perf_counter() - t_upl
        if have_tex:
            t_sub = time.perf_counter()
            if TAG_POSE and capture_pose:
                mat = quat_pos_to_matrix(capture_pose[0], capture_pose[1])
                tex_left.mDeviceToAbsoluteTracking = mat
                tex_right.mDeviceToAbsoluteTracking = mat
            submit_flags = openvr.Submit_TextureWithPose if TAG_POSE else 0
            try:
                compositor.submit(openvr.Eye_Left, tex_left, bounds, submit_flags)
                compositor.submit(openvr.Eye_Right, tex_right, bounds, submit_flags)
                compositor.postPresentHandoff()
            except Exception as exc:
                submit_errors += 1
                if submit_errors <= 5:
                    print("submit error:", type(exc).__name__, exc, flush=True)
            perf["submit"] += time.perf_counter() - t_sub
            perf["total"] += time.perf_counter() - t0
            perf["n"] += 1
            frames += 1

        if time.time() - last_report >= 5.0:
            n = perf["n"] or 1
            print("perf/frame ms: poses=%.1f grab=%.1f resize=%.1f upload=%.1f submit=%.1f total=%.1f fps=%.1f" % (
                perf["poses"] / n * 1000, perf["grab"] / n * 1000, perf["resize"] / n * 1000,
                perf["upload"] / n * 1000, perf["submit"] / n * 1000,
                perf["total"] / n * 1000, n / 5.0), flush=True)
            perf = {"poses": 0.0, "grab": 0.0, "resize": 0.0, "upload": 0.0, "submit": 0.0, "total": 0.0, "n": 0}
            last_report = time.time()
            try:
                frame_timing_fn(ctypes.byref(frame_timing), 0)
                print("frame: flags=0x%x async=%s motion=%s vsyncs=%d dropped=%d presents=%d clientInt=%.1f transfer=%.1f" % (
                    frame_timing.m_nReprojectionFlags,
                    bool(frame_timing.m_nReprojectionFlags & openvr.VRCompositor_ReprojectionAsync),
                    bool(frame_timing.m_nReprojectionFlags & openvr.VRCompositor_ReprojectionMotion),
                    frame_timing.m_nNumVSyncsReadyForUse, frame_timing.m_nNumDroppedFrames,
                    frame_timing.m_nNumFramePresents, frame_timing.m_flClientFrameIntervalMs,
                    frame_timing.m_flTransferLatencyMs), flush=True)
            except Exception as exc:
                print("timing err:", exc, flush=True)

        # target ~75 Hz
        elapsed = time.time() - t0
        if elapsed < 1.0 / 75.0:
            time.sleep(1.0 / 75.0 - elapsed)

    print("done: frames=%d submit_errors=%d" % (frames, submit_errors), flush=True)
    openvr.shutdown()
    glfw.terminate()
    cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())