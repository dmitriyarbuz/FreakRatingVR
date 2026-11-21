"""
Post-reboot restore for the FreakRating VR stack with Virtual Desktop.

Run this AFTER:
  1. PC has rebooted
  2. In the Pico's Virtual Desktop app you see the PC and Connect -> Switch to VR

What it does:
  - Starts SteamVR (vrstartup.exe) if it is not already running
  - Launches the game with --remote-debugging-port=9223
  - Injects the SBS stereo renderer + head-pose client + dual UI
  - Starts vr_bridge.py

The game must be IN A WORLD/ROOM (not the empty lobby) for the world
engine to be found. If it is stuck waiting, enter any room in the game.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

import vr_config as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
STEAMVR = cfg.STEAMVR_DIR
CDP_PORT = cfg.CDP_PORT


def tasklist():
    try:
        return subprocess.check_output(["tasklist"], text=True, errors="ignore").lower()
    except Exception:
        return ""


def running(name):
    return name.lower() in tasklist()


def start_steamvr():
    if running("vrserver.exe") and running("vrcompositor.exe"):
        print("[vr] SteamVR already running")
        return
    exe = os.path.join(STEAMVR, "vrstartup.exe")
    if not os.path.exists(exe):
        exe = os.path.join(STEAMVR, "vrcompositor.exe")
    print("[vr] starting SteamVR:", exe)
    subprocess.Popen([exe], cwd=STEAMVR)
    for _ in range(60):
        if running("vrserver.exe") and running("vrcompositor.exe"):
            print("[vr] SteamVR up")
            return
        time.sleep(1)
    print("[vr] WARNING: vrserver/vrcompositor not detected after 60s")


def main():
    print("=== restore_after_reboot ===")
    start_steamvr()
    time.sleep(2)

    print("[game] launching via start_vr.py (CDP + inject + bridge)...")
    rc = subprocess.call([sys.executable, os.path.join(HERE, "start_vr.py")], cwd=HERE)
    print("[game] start_vr.py exit code:", rc)

    # Quick status report.
    time.sleep(2)
    try:
        targets = json.load(urllib.request.urlopen(
            "http://127.0.0.1:%d/json" % CDP_PORT, timeout=5))
        pages = [t for t in targets if t.get("type") == "page"]
        print("[status] CDP pages:", [p.get("url") for p in pages])
    except Exception as e:
        print("[status] CDP check failed:", e)
    print("[status] bridge running:", running("python.exe"))
    print("=== done. Check headset. ===")
    return rc


if __name__ == "__main__":
    sys.exit(main())
