import os, sys, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, os.environ.get("VR_LOG", "vr_bridge_launcher.log"))
env = os.environ.copy()
# Pass through window/texture sizing so resolution can be raised via env.
env["VR_WIN_W"] = os.environ.get("VR_WIN_W", "2293")
env["VR_WIN_H"] = os.environ.get("VR_WIN_H", "900")
env["VR_EYE_W"] = os.environ.get("VR_EYE_W", "860")
env["VR_EYE_H"] = os.environ.get("VR_EYE_H", "675")
env["VR_TARGET_FPS"] = os.environ.get("VR_TARGET_FPS", "45")
if os.environ.get("VR_SYNTH"):
    env["VR_SYNTH"] = os.environ["VR_SYNTH"]

cmd = [sys.executable, "-u", os.path.join(HERE, "vr_bridge.py"), "3600"]
with open(LOG, "w", encoding="utf-8", buffering=1) as f:
    p = subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT)
print("launched bridge pid", p.pid)
