import json, os, requests, websocket, time

port = os.environ.get('CDP_PORT', '9223')
targets = requests.get(f'http://127.0.0.1:{port}/json', timeout=10).json()
pages = [t for t in targets if t.get('type') == 'page']
ws = websocket.create_connection(pages[0]['webSocketDebuggerUrl'], timeout=10, suppress_origin=True)

nid = [0]
queue = []
def call(method, params=None, collect=True):
    nid[0] += 1
    mid = nid[0]
    ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
    while True:
        d = json.loads(ws.recv())
        if d.get('id') == mid:
            return d
        if collect and d.get('method'):
            queue.append(d)

def drain(seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            ws.settimeout(max(0.05, end - time.time()))
            queue.append(json.loads(ws.recv()))
        except Exception:
            break

call('Debugger.enable')
drain(2)
scripts = {}
for d in queue:
    if d.get('method') == 'Debugger.scriptParsed':
        p = d['params']
        scripts[p['scriptId']] = p.get('url', '')
cand = [(sid, u) for sid, u in scripts.items() if 'engine-' in u and u.endswith('.js')]
print('engine scripts:', cand[:5], 'total scripts:', len(scripts))
if not cand:
    ws.close()
    raise SystemExit(1)

sid, url = cand[0]
text = call('Debugger.getScriptSource', {'scriptId': sid}).get('result', {}).get('scriptSource', '')
lines = text.split('\n')
hits = [i for i, ln in enumerate(lines) if 'renderScene' in ln]
print('renderScene lines:', hits[:10], 'total lines:', len(lines))
if not hits:
    ws.close()
    raise SystemExit(1)

line = hits[0]
bp = call('Debugger.setBreakpoint', {
    'location': {'scriptId': sid, 'lineNumber': line},
})
bid = bp.get('result', {}).get('breakpoint', {}).get('id')
print('breakpoint id:', bid, 'line:', line)
if not bid:
    ws.close()
    raise SystemExit(1)

# wait for pause
paused = None
end = time.time() + 12
while time.time() < end:
    try:
        ws.settimeout(max(0.1, end - time.time()))
        d = json.loads(ws.recv())
    except Exception:
        break
    if d.get('method') == 'Debugger.paused':
        paused = d
        break
    if d.get('method'):
        queue.append(d)

if not paused:
    print('no pause')
else:
    frame = paused['params']['callFrames'][0]
    expr = "(() => { window.__vr_engine = this; return { ok: !!this, keys: this ? Object.keys(this).slice(0,8) : null, hasCam: !!(this && this.camera) }; })()"
    ev = call('Debugger.evaluateOnCallFrame', {
        'callFrameId': frame['callFrameId'], 'expression': expr, 'returnByValue': True
    }, collect=False)
    print('eval:', json.dumps(ev.get('result', {}).get('result', {}).get('value'), ensure_ascii=False))
    call('Debugger.resume')

call('Debugger.removeBreakpoint', {'breakpointId': bid})
call('Debugger.disable')
ws.close()
