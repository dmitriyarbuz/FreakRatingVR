"""
One-shot VR launcher for the FreakRating VR mod.

Starts (or restarts) the game with remote debugging, then starts the watchdog
in the background and exits. The watchdog watches the page and automatically
injects the SBS stereo renderer as soon as a world engine is available
(i.e. as soon as you enter a room in the game).

Unlike start_vr.py this script NEVER blocks waiting for a room — so the
shortcut returns immediately and VR mode turns on by itself when you enter
a room.
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_EXE = r"C:\freakrating\FreakRating.exe"
CDP_PORT = 9223

PY = sys.executable
WATCHDOG = os.path.join(HERE, "vr_watchdog.py")
LOG = os.path.join(HERE, "launch_vr.log")


def game_with_cdp():
    """Return True if a FreakRating page with CDP is already running."""
    try:
        targets = __import__("json").load(
            urllib.request.urlopen("http://127.0.0.1:%d/json" % CDP_PORT, timeout=3)
        )
        return any(t.get("type") == "page" and "freakrating.lol" in t.get("url", "")
                   for t in targets)
    except Exception:
        return False


def kill_watchdogs():
    out = subprocess.check_output(
        ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine,ProcessId", "/format:csv"],
        text=True, encoding="utf-8", errors="ignore",
    )
    for line in out.splitlines():
        if "vr_watchdog.py" in line:
            parts = line.rsplit(",", 1)
            if len(parts) == 2 and parts[1].strip().isdigit():
                subprocess.call(["taskkill", "/F", "/PID", parts[1].strip()],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    log = open(LOG, "a", encoding="utf-8")
    def say(msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    if game_with_cdp():
        say("game already running with CDP")
    else:
        say("starting game...")
        subprocess.call(["taskkill", "/F", "/IM", "FreakRating.exe"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        subprocess.Popen([GAME_EXE, "--remote-debugging-port=%d" % CDP_PORT,
                          "--remote-allow-origins=*"],
                         cwd=os.path.dirname(GAME_EXE),
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)

    kill_watchdogs()
    time.sleep(1)
    say("starting watchdog...")
    subprocess.Popen([PY, "-u", WATCHDOG],
                     cwd=HERE,
                     stdout=log, stderr=subprocess.STDOUT,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    say("READY — enter a room in the game; VR mode turns on automatically.")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
