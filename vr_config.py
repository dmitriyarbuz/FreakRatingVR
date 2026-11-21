"""Shared paths, CDP flags and process helpers for the VR mod launchers.

All machine-specific values can be overridden through environment variables so
the scripts are not tied to one particular install directory.
"""
import os

CDP_PORT = int(os.environ.get("CDP_PORT", "9223"))
GAME_EXE = os.environ.get("VR_GAME_EXE", r"C:\freakrating\FreakRating.exe")
STEAMVR_DIR = os.environ.get(
    "VR_STEAMVR", r"D:\Steam\steamapps\common\SteamVR\bin\win64"
)


def game_cdp_args(port=CDP_PORT):
    """Flags for launching the game with a loopback-only debugging endpoint."""
    args = [
        "--remote-debugging-port=%d" % port,
        "--remote-debugging-address=127.0.0.1",
    ]
    # Cross-origin WebSocket access is off by default: an arbitrary web page
    # must not be able to drive the game's CDP endpoint. Opt in only when a
    # browser-based debugging tool really needs it (VR_CDP_ALLOW_ORIGINS=1).
    if os.environ.get("VR_CDP_ALLOW_ORIGINS") == "1":
        args.append("--remote-allow-origins=*")
    return args


def kill_python_scripts(*script_names):
    """Kill python processes whose command line contains one of *script_names*.

    Replaces the old wmic/taskkill shellout: wmic is being removed from recent
    Windows builds and taskkill's /FI filter does not support wildcards.
    """
    import psutil

    killed = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("python.exe", "pythonw.exe", "python", "python3"):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if any(script in cmdline for script in script_names):
                proc.kill()
                killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return killed
