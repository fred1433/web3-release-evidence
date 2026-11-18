"""Chain access, fault-injecting RPC proxy and process management for the bench.

The bench reads the chain DIRECTLY from the node (Chain). The application under test
(interface, indexer, API) only ever talks to the node through RpcProxy, which can cut
chosen JSON-RPC methods. That separation is what lets the bench say what the chain
really holds while the application is being lied to.
"""
import json, os, socket, subprocess, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post_json(url, payload, timeout=10):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_get(url, headers=None, timeout=5):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def pad(v):
    if isinstance(v, int):
        return format(v, '064x')
    return v.lower().replace('0x', '').rjust(64, '0')


class RpcError(Exception):
    def __init__(self, err):
        super().__init__(err.get('message'))
        self.data = err.get('data')


class Chain:
    """Direct JSON-RPC access to the local node, independent of the application."""

    def __init__(self, url):
        self.url = url
        self._id = 0
        self.mines = []  # wall-clock ms of every evm_mine the bench issued

    def rpc(self, method, params=None):
        self._id += 1
        j = post_json(self.url, {'jsonrpc': '2.0', 'id': self._id, 'method': method, 'params': params or []})
        if 'error' in j:
            raise RpcError(j['error'])
        return j['result']

    def keccak(self, text):
        return self.rpc('web3_sha3', ['0x' + text.encode().hex()])

    def selector(self, signature):
        return self.keccak(signature)[:10]

    def mine(self):
        t = int(time.time() * 1000)
        self.mines.append(t)
        self.rpc('evm_mine')
        return t

    def head(self):
        return int(self.rpc('eth_blockNumber'), 16)

    def block(self, n):
        return self.rpc('eth_getBlockByNumber', [hex(n), True])

    def pending_count(self):
        return int(self.rpc('txpool_status')['pending'], 16)

    def receipt(self, h):
        return self.rpc('eth_getTransactionReceipt', [h])

    def send(self, frm, to, data, gas=None):
        tx = {'from': frm, 'data': data}
        if to:
            tx['to'] = to
        if gas:
            tx['gas'] = hex(gas)
        return self.rpc('eth_sendTransaction', [tx])

    def send_and_mine(self, frm, to, data, gas=None):
        h = self.send(frm, to, data, gas)
        self.mine()
        r = self.receipt(h)
        if r is None or r['status'] != '0x1':
            raise RuntimeError(f'setup transaction failed: {h}')
        return r

    def call(self, to, data, frm=None):
        tx = {'to': to, 'data': data}
        if frm:
            tx['from'] = frm
        return self.rpc('eth_call', [tx, 'latest'])

    def simulate_error(self, frm, to, data):
        """Return the 4-byte custom error selector the call reverts with, or None."""
        try:
            self.call(to, data, frm)
            return None
        except RpcError as e:
            d = e.data if isinstance(e.data, str) else (e.data or {}).get('data') if isinstance(e.data, dict) else None
            return d[:10] if d else 'revert'

    def uint(self, to, data):
        return int(self.call(to, data), 16)

    def native(self, addr):
        return int(self.rpc('eth_getBalance', [addr, 'latest']), 16)

    def nonce(self, addr, tag='pending'):
        return int(self.rpc('eth_getTransactionCount', [addr, tag]), 16)


class RpcProxy:
    """JSON-RPC proxy between the application and the node. Can cut chosen methods."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.cut_methods = set()
        self.cut_status = 503
        self.counts = {}
        self.events = []  # writes, signatures and cut calls, with time
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header('access-control-allow-origin', '*')
                self.send_header('access-control-allow-headers', 'content-type')
                self.send_header('access-control-allow-methods', 'POST, OPTIONS')

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('content-length', 0)))
                try:
                    method = json.loads(body).get('method', '?')
                except Exception:
                    method = '?'
                with proxy.lock:
                    proxy.counts[method] = proxy.counts.get(method, 0) + 1
                    cut = method in proxy.cut_methods
                    if cut or method in ('eth_sendTransaction', 'personal_sign'):
                        proxy.events.append({'t': int(time.time() * 1000), 'method': method, 'cut': cut})
                if cut:
                    self.send_response(proxy.cut_status)
                    self._cors()
                    self.end_headers()
                    self.wfile.write(b'injected outage')
                    return
                req = urllib.request.Request(proxy.upstream, body, {'content-type': 'application/json'})
                with urllib.request.urlopen(req, timeout=10) as r:
                    out = r.read()
                self.send_response(200)
                self._cors()
                self.send_header('content-type', 'application/json')
                self.end_headers()
                self.wfile.write(out)

        self.port = free_port()
        self.url = f'http://127.0.0.1:{self.port}'
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.cut_methods = set()
            self.counts = {}
            self.events = []

    def cut(self, *methods):
        with self.lock:
            self.cut_methods = set(methods)

    def restore(self):
        with self.lock:
            self.cut_methods = set()

    def count(self, method):
        with self.lock:
            return self.counts.get(method, 0)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def start_anvil(port, chain_id, log_path):
    log = open(log_path, 'w')
    proc = subprocess.Popen(
        ['anvil', '--host', '127.0.0.1', '--port', str(port), '--chain-id', str(chain_id), '--no-mining', '--accounts', '6'],
        stdout=log, stderr=subprocess.STDOUT)
    url = f'http://127.0.0.1:{port}'
    for _ in range(100):
        try:
            post_json(url, {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}, timeout=1)
            return proc, url
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError('anvil did not start')


def start_app(variant, env_extra, log_path):
    port = free_port()
    env = dict(os.environ, PORT=str(port), VARIANT=variant, **env_extra)
    log = open(log_path, 'a')
    proc = subprocess.Popen(['node', os.path.join(ROOT, 'app', 'server.mjs')], env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f'http://127.0.0.1:{port}'
    for _ in range(100):
        try:
            code, _ = http_get(url + '/api/state', timeout=1)
            if code == 200:
                return proc, url
        except Exception:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError('app server did not start')


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
