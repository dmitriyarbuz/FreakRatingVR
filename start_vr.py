"""
Full launcher for the FreakRating OpenVR bridge.

Starts the game with remote debugging, injects the SBS stereo renderer,
dual UI, head-pose client and camera shims, then starts vr_bridge.py.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

import vr_config as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_EXE = cfg.GAME_EXE
CDP_PORT = cfg.CDP_PORT
INJECT_JS = os.path.join(HERE, "vr_inject.js")
BRIDGE_PY = os.path.join(HERE, "vr_bridge.py")


EVENTS = []


def cdp_call(ws, method, params=None, next_id=None):
    msg = {"id": next_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))
    while True:
        data = json.loads(ws.recv())
        if data.get("id") == next_id:
            return data
        if data.get("method"):
            EVENTS.append(data)


def wait_for_page(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=5))
            for t in targets:
                if t.get("type") == "page" and "freakrating.lol" in t.get("url", ""):
                    return t
        except Exception:
            pass
        time.sleep(0.5)
    return None


def find_instance(ws, url_expr, export_expr, global_name, next_id):
    """Import a module by URL and store its first live instance on window."""
    expr = "import(%s).then(m => %s).then(p => p)" % (url_expr, export_expr)
    r = cdp_call(ws, "Runtime.evaluate", {
        "expression": expr, "awaitPromise": True, "returnByValue": False
    }, next_id)
    next_id += 1
    proto_id = r.get("result", {}).get("result", {}).get("objectId")
    if not proto_id:
        print("  no prototype for", global_name, json.dumps(r, ensure_ascii=False)[:500])
        return False, next_id
    q = cdp_call(ws, "Runtime.queryObjects", {"prototypeObjectId": proto_id}, next_id)
    next_id += 1
    objects_id = q.get("result", {}).get("objects", {}).get("objectId")
    if not objects_id:
        print("  no objects for", global_name)
        return False, next_id
    decl = "function(){ window.%s = this[0] || null; return { ok: !!window.%s, count: this.length }; }" % (global_name, global_name)
    res = cdp_call(ws, "Runtime.callFunctionOn", {
        "objectId": objects_id, "functionDeclaration": decl, "returnByValue": True
    }, next_id)
    next_id += 1
    val = res.get("result", {}).get("result", {}).get("value", {})
    print("  %s: %s" % (global_name, val))
    return bool(val.get("ok")), next_id


def main():
    # Optionally stop old instances.
    if os.environ.get("KEEP_RUNNING") != "1":
        subprocess.call(["taskkill", "/F", "/IM", "FreakRating.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        killed = cfg.kill_python_scripts("vr_bridge.py")
        if killed:
            print("stopped old bridges:", killed, flush=True)

    print("starting game...")
    subprocess.Popen(
        [GAME_EXE] + cfg.game_cdp_args(CDP_PORT),
        cwd=os.path.dirname(GAME_EXE),
    )

    page = wait_for_page(CDP_PORT)
    if not page:
        print("game page did not appear")
        return 1
    print("page:", page["url"])

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
    next_id = 1
    cdp_call(ws, "Runtime.enable", {}, next_id); next_id += 1

    # Collect ALL loaded engine modules. The game can load several engine
    # bundles after an update, and the live instance may be in any of them.
    cdp_call(ws, "Debugger.enable", {}, next_id); next_id += 1
    time.sleep(1.0)
    engine_urls = []
    for d in EVENTS:
        if d.get("method") == "Debugger.scriptParsed":
            u = d.get("params", {}).get("url", "")
            if "/engine-" in u and u.endswith(".js"):
                engine_urls.append(u)
    engine_urls = list(dict.fromkeys(engine_urls))
    print("engine urls (loaded):", engine_urls)

    # Discover module URLs from loaded resources.
    expr = ("(async () => { const u = performance.getEntriesByType('resource').map(e => e.name); "
            "return { three: u.find(n => /three\\.module-.*\\.js/.test(n)) || null, "
            "engine: u.find(n => /engine-.*\\.js/.test(n)) || null }; })()")
    r = cdp_call(ws, "Runtime.evaluate", {
        "expression": expr, "awaitPromise": True, "returnByValue": True
    }, next_id)
    next_id += 1
    urls = r.get("result", {}).get("result", {}).get("value", {})
    if not urls.get("three"):
        urls["three"] = "https://freakrating.lol/assets/three.module-C554G4wK.js"
    if not urls.get("engine"):
        # Discover the current engine hash from the WorldPage bundle (survives game updates).
        expr2 = ("(async () => { const u = performance.getEntriesByType('resource').map(e => e.name); "
                 "const wp = u.find(n => /WorldPage-.*\\.js/.test(n)); if(!wp) return null; "
                 "const t = await (await fetch(wp)).text(); const m = t.match(/engine-[A-Za-z0-9_-]+\\.js/); "
                 "return m ? m[0] : null; })()")
        rr = cdp_call(ws, "Runtime.evaluate", {
            "expression": expr2, "awaitPromise": True, "returnByValue": True
        }, next_id)
        next_id += 1
        eng = rr.get("result", {}).get("result", {}).get("value")
        urls["engine"] = ("https://freakrating.lol/assets/" + eng) if eng else "https://freakrating.lol/assets/engine-CDqqU9p9.js"
    print("asset urls:", urls)

    # Try every discovered engine module, not just the first one from the bundle.
    if urls.get("engine"):
        engine_urls.insert(0, urls["engine"])
    engine_urls = list(dict.fromkeys(engine_urls))
    print("engine urls (candidates):", engine_urls)

    ok1 = ok2 = False
    for attempt in range(30):
        ok1, next_id = find_instance(ws, json.dumps(urls["three"]), "m.i.prototype", "__vr_renderer", next_id)
        for eng_url in engine_urls:
            ok2, next_id = find_instance(ws, json.dumps(eng_url), "m.WorldEngine.prototype", "__vr_engine", next_id)
            if ok2:
                break
        if ok1 and ok2:
            break
        print("  waiting for world engine (enter a room in the game)... %d" % attempt)
        time.sleep(2)
    cdp_call(ws, "Debugger.disable", {}, next_id); next_id += 1
    if not ok1 or not ok2:
        print("instance discovery failed; enter a world in the game and rerun this launcher")
        return 1

    # Wait until the world scene is ready.
    time.sleep(2)
    with open(INJECT_JS, "r", encoding="utf-8") as f:
        js = f.read()
    r = cdp_call(ws, "Runtime.evaluate", {"expression": js, "returnByValue": True}, next_id)
    next_id += 1
    val = r.get("result", {}).get("result", {}).get("value")
    exc = r.get("result", {}).get("exceptionDetails")
    if exc:
        print("inject exception:", json.dumps(exc, ensure_ascii=False)[:1000])
    print("inject result:", val)

    cdp_call(ws, "Emulation.setFocusEmulationEnabled", {"enabled": True}, next_id)
    next_id += 1
    ws.close()

    print("starting bridge...")
    log = open(os.path.join(HERE, "vr_bridge_launcher.log"), "w", encoding="utf-8")
    subprocess.Popen([sys.executable, BRIDGE_PY, "3600"], stdout=log, stderr=subprocess.STDOUT,
                     cwd=HERE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    print("READY. Controls:")
    print("  Left stick: move   Head: look")
    print("  A: jump   LEFT STICK CLICK: sprint (Shift)   GRIP: emote (G)")
    print("  Trigger: click   Menu: Esc")
    print("  RIGHT STICK CLICK: cycle UI (0=dual, 1=original, 2=none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
