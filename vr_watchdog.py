"""
VR mode watchdog.

Periodically checks (over CDP) whether the FreakRating page still has a live
SBS/stereo injection. If the injection was lost (game update, page reload,
room change) it:
  1. clears stale wrappers,
  2. re-discovers the current engine bundle hash,
  3. re-runs reinject_full.py (which re-injects vr_inject.js and restarts the bridge).

Run this in the background once, and leave it running:
    python -u vr_watchdog.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import start_vr as sv
import vr_config as cfg

CHECK_INTERVAL = float(os.environ.get('VR_WATCH_INTERVAL', '10'))  # seconds between checks
REINJECT_COOLDOWN = float(os.environ.get('VR_WATCH_COOLDOWN', '30'))  # min seconds between reinjects


def get_page():
    """Return the FreakRating page target, or None."""
    try:
        targets = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json' % sv.CDP_PORT, timeout=5))
    except Exception:
        return None
    for t in targets:
        if t.get('type') == 'page' and 'freakrating.lol' in t.get('url', ''):
            return t
    return None


def eval_expr(ws, expr, next_id=1):
    r = sv.cdp_call(ws, 'Runtime.evaluate', {
        'expression': expr, 'returnByValue': True, 'awaitPromise': True
    }, next_id)
    return r.get('result', {}).get('result', {}).get('value')


def vr_alive():
    """Return True if stereo injection is live and a world engine exists."""
    page = get_page()
    if not page:
        return False, 'no page'
    url = page.get('url', '')
    last_segment = url.rstrip('/').split('/')[-1].split('?')[0].lower()
    if last_segment == 'world':
        return False, 'no room (on /world)'
    try:
        ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10, suppress_origin=True)
    except Exception as e:
        return False, 'ws connect: %s' % e
    try:
        state = eval_expr(ws, "(() => { const e=window.__vr_engine; return {"
                               "stereo: !!window.__vr_stereo, "
                               "wrapped: !!(e && e.__vr_pose_wrapped), "
                               "engine: !!e, renderer: !!(e && e.renderer), "
                               "ws: !!window.__vr_ws_ready "
                               "}; })()")
    except Exception as e:
        return False, 'eval: %s' % e
    finally:
        ws.close()
    if not state or not isinstance(state, dict):
        return False, 'no state'
    ok = bool(state.get('stereo')) and bool(state.get('wrapped')) and \
         bool(state.get('engine')) and bool(state.get('renderer')) and bool(state.get('ws'))
    return ok, state


def clear_wrappers():
    """Undo any stale wrappers on the current page before re-injecting."""
    page = get_page()
    if not page:
        return
    try:
        ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10, suppress_origin=True)
    except Exception:
        return
    expr = ("(() => { const e=window.__vr_engine; if(!e) return 'no engine'; "
            "try { e.renderScene = e.__vr_origRenderScene || e.renderScene; } catch(err){} "
            "try { if(e.__vr_origPose) e.pose = e.__vr_origPose; } catch(err){} "
            "try { if(e.__vr_origMove && e.callbacks) e.callbacks.move = e.__vr_origMove; } catch(err){} "
            "['__vr_origRenderScene','__vr_pose_wrapped','__vr_net_pose_wrapped','__vr_origPose','__vr_origMove']"
            ".forEach(k=>{try{delete e[k];}catch(err){}}); "
            "e.__vr_stereo = false; return 'cleared'; })()")
    try:
        eval_expr(ws, expr)
    except Exception:
        pass
    finally:
        ws.close()


def discover_engine_url():
    """Find the current engine bundle hash from loaded page resources."""
    page = get_page()
    if not page:
        return None
    try:
        ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10, suppress_origin=True)
    except Exception:
        return None
    expr = ("(async () => { const u = performance.getEntriesByType('resource').map(e => e.name); "
            "let eng = u.find(n => /engine-.*\\.js/.test(n)); "
            "if(!eng){ const wp = u.find(n => /WorldPage-.*\\.js/.test(n)); "
            " if(wp){ const t = await (await fetch(wp)).text(); const m = t.match(/engine-[A-Za-z0-9_-]+\\.js/); "
            "  if(m) eng = 'https://freakrating.lol/assets/' + m[0]; } } "
            "return eng || null; })()")
    try:
        return eval_expr(ws, expr)
    except Exception:
        return None
    finally:
        ws.close()


def main():
    print('VR watchdog started. Interval=%ss Cooldown=%ss' % (CHECK_INTERVAL, REINJECT_COOLDOWN), flush=True)
    last_reinject = 0.0
    while True:
        ok, reason = vr_alive()
        if ok:
            print('[%s] VR OK' % time.strftime('%H:%M:%S'), flush=True)
            time.sleep(CHECK_INTERVAL)
            continue

        print('[%s] VR NOT alive: %s' % (time.strftime('%H:%M:%S'), reason), flush=True)
        # In the lobby there is no world engine to hook. Stop any bridge left
        # over from a previous room: it would keep submitting stale textures to
        # the OpenVR compositor and make the whole headset stutter.
        if reason == 'no room (on /world)':
            killed = cfg.kill_python_scripts('vr_bridge.py')
            if killed:
                print('  stopped stale bridge:', killed, flush=True)
            time.sleep(CHECK_INTERVAL)
            continue
        now = time.time()
        if now - last_reinject < REINJECT_COOLDOWN:
            print('  cooldown active, skip', flush=True)
            time.sleep(CHECK_INTERVAL)
            continue

        clear_wrappers()
        eng = discover_engine_url()
        env = os.environ.copy()
        if eng:
            env['ENGINE_URL'] = eng
            print('  engine url:', eng, flush=True)
        print('  re-injecting...', flush=True)
        try:
            subprocess.call([sys.executable, os.path.join(HERE, 'reinject_full.py')],
                            cwd=HERE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        except Exception as e:
            print('  reinject failed:', e, flush=True)
        last_reinject = now
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
