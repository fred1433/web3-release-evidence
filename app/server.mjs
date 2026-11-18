// Sample Grants: a synthetic dApp used as the test ground for the release bench.
// One process serves three things: the web interface, a minimal off-chain indexer
// that follows the contract's events, and the API the interface reads.
// Every chain call goes through RPC_URL (the fault-injecting proxy during a bench run).
// No dependencies beyond Node itself.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const env = (k, d) => process.env[k] ?? d;
const PORT = Number(env('PORT', '5173'));
const RPC_URL = env('RPC_URL', 'http://127.0.0.1:8545');
const VARIANT = env('VARIANT', 'corrected');
const GRANTS = env('GRANTS_ADDRESS', '').toLowerCase();
const TOKEN = env('TOKEN_ADDRESS', '').toLowerCase();
const ADMIN = env('ADMIN_ADDRESS', '').toLowerCase();
const READER = env('READER_ADDRESS', '').toLowerCase();
const CHAIN_ID = env('CHAIN_ID', '0x7a69');
const FEE = env('FEE_WEI', '25000000000000000000');
const DATASET = 'sensor-log-S07';
const TERMS = 'Sample Grants demo terms v0 (fictitious). I understand the reader will see the synthetic sensor log S-07 until I revoke access, and that this payment cannot be reversed.';

let rpcId = 0;
async function rpc(method, params = []) {
  const res = await fetch(RPC_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: ++rpcId, method, params }),
  });
  if (!res.ok) throw new Error(`rpc ${method} http ${res.status}`);
  const j = await res.json();
  if (j.error) throw new Error(`rpc ${method}: ${j.error.message}`);
  return j.result;
}
const hexOf = (s) => '0x' + Buffer.from(s, 'utf8').toString('hex');
const keccak = (s) => rpc('web3_sha3', [hexOf(s)]);
const word = (hex, i) => '0x' + hex.slice(2 + 64 * i, 2 + 64 * (i + 1));
const addrFromTopic = (t) => '0x' + t.slice(26).toLowerCase();

// ---------- indexer ----------
const index = { block: -1, grants: new Map(), lastError: null, topics: {}, selectors: {} };

async function prepare() {
  const sig = {
    grantAndPay: 'grantAndPay(address,bytes32,uint256,bytes32)',
    revoke: 'revoke(uint256)',
    setAllowlisted: 'setAllowlisted(address,bool)',
    balanceOf: 'balanceOf(address)',
    allowlisted: 'allowlisted(address)',
  };
  for (const [k, s] of Object.entries(sig)) index.selectors[k] = (await keccak(s)).slice(0, 10);
  index.topics.granted = await keccak('Granted(uint256,address,address,bytes32,uint256)');
  index.topics.revoked = await keccak('Revoked(uint256,address)');
  index.datasetId = await keccak(DATASET);
}

async function tick() {
  try {
    const head = parseInt(await rpc('eth_blockNumber'), 16);
    if (head > index.block) {
      const logs = await rpc('eth_getLogs', [{ address: GRANTS, fromBlock: '0x' + (index.block + 1).toString(16), toBlock: '0x' + head.toString(16) }]);
      for (const log of logs) apply(log);
      index.block = head;
    }
    index.lastError = null;
  } catch (e) {
    index.lastError = String(e.message || e);
  }
}

function apply(log) {
  const id = BigInt(log.topics[1]).toString();
  if (log.topics[0] === index.topics.granted) {
    index.grants.set(id, {
      id, owner: addrFromTopic(log.topics[2]), reader: addrFromTopic(log.topics[3]),
      dataset: word(log.data, 0), paid: BigInt(word(log.data, 1)).toString(),
      active: true, grantedBlock: parseInt(log.blockNumber, 16), grantedTx: log.transactionHash,
    });
  } else if (log.topics[0] === index.topics.revoked) {
    const g = index.grants.get(id);
    if (g) Object.assign(g, { active: false, revokedBlock: parseInt(log.blockNumber, 16), revokedTx: log.transactionHash });
  }
}

// ---------- consent records (off-chain) ----------
const acceptances = new Map();
async function verifySignature(account, message, signature) {
  const bytes = Buffer.from(message, 'utf8');
  const prefixed = Buffer.concat([Buffer.from(`\x19Ethereum Signed Message:\n${bytes.length}`, 'utf8'), bytes]);
  const hash = await rpc('web3_sha3', ['0x' + prefixed.toString('hex')]);
  const sig = signature.replace(/^0x/, '');
  const r = sig.slice(0, 64), s = sig.slice(64, 128);
  let v = parseInt(sig.slice(128, 130), 16); if (v < 27) v += 27;
  const input = hash.slice(2) + v.toString(16).padStart(64, '0') + r + s;
  const out = await rpc('eth_call', [{ to: '0x0000000000000000000000000000000000000001', data: '0x' + input }, 'latest']);
  return out && out.length >= 66 && ('0x' + out.slice(26)).toLowerCase() === account.toLowerCase();
}

// ---------- fixture data served under a grant ----------
const SENSOR_LOG = {
  dataset: 'Synthetic sensor log S-07',
  note: 'Generated fixture. Not real data.',
  rows: [
    { hour: '00:00', celsius: 11.2 }, { hour: '06:00', celsius: 9.8 },
    { hour: '12:00', celsius: 17.4 }, { hour: '18:00', celsius: 14.1 },
  ],
};

// ---------- http ----------
const STATIC = {
  '/': ['ui/index.html', 'text/html; charset=utf-8'],
  '/app.js': ['ui/app.js', 'text/javascript; charset=utf-8'],
  '/style.css': ['ui/style.css', 'text/css; charset=utf-8'],
};
const send = (res, code, body, type = 'application/json') => {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
};
const readBody = (req) => new Promise((ok) => { let b = ''; req.on('data', (c) => (b += c)); req.on('end', () => ok(b)); });

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const p = url.pathname;
  try {
    if (STATIC[p]) return send(res, 200, fs.readFileSync(path.join(HERE, STATIC[p][0]), 'utf8'), STATIC[p][1]);
    if (p === '/flow.js') return send(res, 200, fs.readFileSync(path.join(HERE, '..', 'variants', VARIANT, 'flow.js'), 'utf8'), 'text/javascript; charset=utf-8');
    if (p === '/config.json') return send(res, 200, {
      chainId: CHAIN_ID, grants: GRANTS, token: TOKEN, admin: ADMIN, reader: READER, fee: FEE,
      datasetId: index.datasetId, datasetName: SENSOR_LOG.dataset, terms: TERMS, selectors: index.selectors, variant: VARIANT,
    });
    if (p === '/api/state') return send(res, 200, { indexedBlock: index.block, lastError: index.lastError, grants: [...index.grants.values()] });
    if (p === '/api/grants') {
      const owner = (url.searchParams.get('owner') || '').toLowerCase();
      return send(res, 200, { indexedBlock: index.block, grants: [...index.grants.values()].filter((g) => g.owner === owner) });
    }
    let m = p.match(/^\/api\/data\/(\d+)$/);
    if (m) {
      const g = index.grants.get(m[1]);
      const reader = String(req.headers['x-reader-address'] || '').toLowerCase();
      if (g && g.active && g.reader === reader) return send(res, 200, { grantId: g.id, ...SENSOR_LOG });
      return send(res, 403, { error: 'No active grant for this reader.', grantId: m[1], indexedBlock: index.block });
    }
    if (p === '/api/acceptance' && req.method === 'POST') {
      const { account, message, signature } = JSON.parse(await readBody(req));
      if (message !== TERMS) return send(res, 400, { error: 'Unknown terms text.' });
      const verified = await verifySignature(account, message, signature);
      if (!verified) return send(res, 400, { error: 'Signature does not match the account.' });
      const rec = { account: account.toLowerCase(), terms: 'v0-fictitious', signature, verified, recordedAt: new Date().toISOString() };
      acceptances.set(rec.account, rec);
      return send(res, 201, rec);
    }
    m = p.match(/^\/api\/acceptance\/(0x[0-9a-fA-F]{40})$/);
    if (m) {
      const rec = acceptances.get(m[1].toLowerCase());
      return rec ? send(res, 200, rec) : send(res, 404, { error: 'No acceptance recorded.' });
    }
    send(res, 404, { error: 'Not found' });
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
});

await prepare();
await tick();
setInterval(tick, 150);
server.listen(PORT, '127.0.0.1', () => console.log(`sample-grants (${VARIANT}) on http://127.0.0.1:${PORT}`));
