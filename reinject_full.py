import json, os, sys, time, subprocess, requests, websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import start_vr as sv

targets = requests.get(f'http://127.0.0.1:{sv.CDP_PORT}/json', timeout=10).json()
pages = [t for t in targets if t.get('type') == 'page']
if not pages:
    raise SystemExit('no page target')
page = pages[0]
print('page:', page.get('url'))
ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30)
nid = 1
sv.cdp_call(ws, 'Runtime.enable', {}, nid); nid += 1

expr = ("(async () => { const u = performance.getEntriesByType('resource').map(e => e.name); "
        "return { three: u.find(n => /three\\.module-.*\\.js/.test(n)) || null, "
        "engine: u.find(n => /engine-.*\\.js/.test(n)) || null }; })()")
r = sv.cdp_call(ws, 'Runtime.evaluate', {
    'expression': expr, 'awaitPromise': True, 'returnByValue': True
}, nid); nid += 1
urls = r.get('result', {}).get('result', {}).get('value', {})
if not urls.get('three'):
    urls['three'] = 'https://freakrating.lol/assets/three.module-C554G4wK.js'
if not urls.get('engine'):
    expr2 = ("(async () => { const u = performance.getEntriesByType('resource').map(e => e.name); "
             "const wp = u.find(n => /WorldPage-.*\\.js/.test(n)); if(!wp) return null; "
             "const t = await (await fetch(wp)).text(); const m = t.match(/engine-[A-Za-z0-9_-]+\\.js/); "
             "return m ? m[0] : null; })()")
    rr = sv.cdp_call(ws, 'Runtime.evaluate', {
        'expression': expr2, 'awaitPromise': True, 'returnByValue': True
    }, nid); nid += 1
    eng = rr.get('result', {}).get('result', {}).get('value')
    urls['engine'] = ('https://freakrating.lol/assets/' + eng) if eng else 'https://freakrating.lol/assets/engine-CDqqU9p9.js'
if os.environ.get('ENGINE_URL'):
    urls['engine'] = os.environ['ENGINE_URL']
print('asset urls:', urls)

ok2 = False
for attempt in range(15):
    ok2, nid = sv.find_instance(ws, json.dumps(urls['engine']), 'm.WorldEngine.prototype', '__vr_engine', nid)
    # The renderer lives on the engine instance itself (e.renderer) in newer
    # game builds; fall back to the three.js prototype lookup for old builds.
    rr = sv.cdp_call(ws, 'Runtime.evaluate', {
        'expression': "(() => { const e=window.__vr_engine; const r=e&&e.renderer; if(r&&r.domElement){ window.__vr_renderer=r; return true;} return false; })()",
        'returnByValue': True
    }, nid); nid += 1
    ok1 = bool(rr.get('result', {}).get('result', {}).get('value'))
    if not ok1:
        ok1, nid = sv.find_instance(ws, json.dumps(urls['three']), 'm.i.prototype', '__vr_renderer', nid)
    print('  attempt %d renderer=%s engine=%s' % (attempt, ok1, ok2), flush=True)
    if ok1 and ok2:
        break
    time.sleep(2)
if not (ok1 and ok2):
    print('FAILED: enter a world in the game, then rerun this script')
    ws.close()
    raise SystemExit(1)

time.sleep(2)
with open(sv.INJECT_JS, 'r', encoding='utf-8') as f:
    js = f.read()
r = sv.cdp_call(ws, 'Runtime.evaluate', {'expression': js, 'returnByValue': True}, nid); nid += 1
val = r.get('result', {}).get('result', {}).get('value')
exc = r.get('result', {}).get('exceptionDetails')
if exc:
    print('inject exception:', json.dumps(exc, ensure_ascii=False)[:1000])
print('inject result:', val)

sv.cdp_call(ws, 'Emulation.setFocusEmulationEnabled', {'enabled': True}, nid); nid += 1
ws.close()

# restart bridge cleanly
subprocess.call(['taskkill', '/F', '/IM', 'python.exe', '/FI', 'COMMANDLINE eq *vr_bridge.py*'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
out = subprocess.check_output(
    ['wmic', 'process', 'where', "name='python.exe'", 'get', 'CommandLine,ProcessId', '/format:csv'],
    text=True, encoding='utf-8', errors='ignore')
for line in out.splitlines():
    if 'vr_bridge.py' in line:
        parts = line.rsplit(',', 1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            subprocess.call(['taskkill', '/F', '/PID', parts[1].strip()],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
log = open(os.path.join(HERE, 'vr_bridge_launcher.log'), 'w', encoding='utf-8')
subprocess.Popen([sys.executable, sv.BRIDGE_PY, '3600'], stdout=log, stderr=subprocess.STDOUT,
                 cwd=HERE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
print('bridge restarted')
