// Serves site/ under https://theaipipe.com/demos/web3-release-evidence/ (static files only).
const BASE = '/demos/web3-release-evidence';
const ROBOTS = 'noindex, nofollow';
const MEDIA = { '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg' };

// Safari asks for audio and video in byte ranges and will not play them otherwise.
async function ranged(res, request, type) {
  const buf = await res.arrayBuffer();
  const size = buf.byteLength;
  const headers = new Headers(res.headers);
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Content-Type', type);
  headers.set('X-Robots-Tag', ROBOTS);
  headers.delete('Content-Length');
  const head = request.method === 'HEAD';
  const m = /^bytes=(\d*)-(\d*)$/.exec(request.headers.get('Range') || '');
  if (!m || (m[1] === '' && m[2] === '')) {
    headers.set('Content-Length', String(size));
    return new Response(head ? null : buf, { status: 200, headers });
  }
  let start, end;
  if (m[1] === '') { start = Math.max(0, size - Number(m[2])); end = size - 1; }
  else { start = Number(m[1]); end = m[2] === '' ? size - 1 : Math.min(Number(m[2]), size - 1); }
  if (start >= size || start > end) {
    headers.set('Content-Range', `bytes */${size}`);
    return new Response(null, { status: 416, headers });
  }
  headers.set('Content-Range', `bytes ${start}-${end}/${size}`);
  headers.set('Content-Length', String(end - start + 1));
  return new Response(head ? null : buf.slice(start, end + 1), { status: 206, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === BASE) return Response.redirect(`${url.origin}${BASE}/`, 308);
    if (!url.pathname.startsWith(BASE + '/')) return new Response('Not found', { status: 404 });
    const path = url.pathname.slice(BASE.length) || '/';
    const ext = (path.match(/\.[a-z0-9]+$/i) || [''])[0].toLowerCase();
    const type = MEDIA[ext];
    const assetReq = type
      ? new Request(new URL(path, url.origin), { method: 'GET' })
      : new Request(new URL(path, url.origin), request);
    const res = await env.ASSETS.fetch(assetReq);
    if (type && res.status === 200) return ranged(res, request, type);
    const out = new Response(res.body, res);
    const loc = out.headers.get('Location');
    if (loc && loc.startsWith('/')) out.headers.set('Location', BASE + loc);
    out.headers.set('X-Robots-Tag', ROBOTS);
    return out;
  },
};
