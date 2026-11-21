import json, os, sys, requests, websocket

def main():
    expr = sys.stdin.read() if len(sys.argv) < 2 else sys.argv[1]
    port = os.environ.get('CDP_PORT', '9223')
    targets = requests.get(f'http://127.0.0.1:{port}/json', timeout=10).json()
    pages = [t for t in targets if t.get('type') == 'page']
    if not pages:
        print('no page target')
        return
    ws_url = pages[0]['webSocketDebuggerUrl']
    ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
    msg = {
        'id': 1,
        'method': 'Runtime.evaluate',
        'params': {
            'expression': expr,
            'returnByValue': True,
            'awaitPromise': True,
            'userGesture': True,
        },
    }
    ws.send(json.dumps(msg))
    while True:
        data = json.loads(ws.recv())
        if data.get('id') == 1:
            print(json.dumps(data, indent=2, ensure_ascii=False))
            break
    ws.close()

if __name__ == '__main__':
    main()
