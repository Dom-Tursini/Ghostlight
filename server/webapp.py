"""
Serves the built front end from the capture service, on the same port as the
WebSocket.

Running Ghostlight used to mean two terminals and a Node install: Vite to serve
static files, and Python to talk to the sensor. Node was doing nothing at
runtime except handing over a few hundred kilobytes of already-built assets,
which is a lot of prerequisite for very little work. Serving them from here
makes `python ghostlight.py` the whole application.

Sharing one port matters more than it looks. The page and the socket end up on
the same origin, so there is no CORS to configure, no second port to remember,
and no way for the two halves to disagree about where the other one is. The
front end simply connects back to wherever it was loaded from.

Vite is still the right tool while developing, because of hot reload. That path
is unchanged: `npm run dev` serves on its own port and connects back to this one.
"""

from __future__ import annotations

import mimetypes
import os
import posixpath
import urllib.parse

from websockets.datastructures import Headers
from websockets.http11 import Response

# Windows reads MIME types out of the registry, where .js is often registered as
# text/plain. A module script served as text/plain is refused by the browser and
# the app silently never starts, so these are pinned rather than guessed.
TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.ico': 'image/x-icon',
    '.webp': 'image/webp',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.wasm': 'application/wasm',
    '.map': 'application/json; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
    '.md': 'text/plain; charset=utf-8',
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

NOT_BUILT = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Ghostlight</title></head>
<body style="background:#090A0B;color:#c9ced2;font:14px/1.7 system-ui,sans-serif;
             margin:0;display:grid;place-items:center;height:100vh">
<div style="max-width:34em;padding:2em">
<h1 style="color:#00FFAE;font-weight:400;margin:0 0 .6em">Front end not built</h1>
<p>The capture service is running, but there is no <code>dist</code> folder to
serve. Build it once:</p>
<pre style="background:#111517;padding:.9em;border-radius:4px">npm install
npm run build</pre>
<p>Then reload this page. While working on the interface itself, run
<code>npm run dev</code> instead and use the address it prints, which reloads
on every edit.</p>
</div></body></html>"""


def web_root() -> str | None:
    """Where the built front end lives, or None if it has not been built.

    GHOSTLIGHT_WEB wins, so a packaged build can put the assets wherever suits
    it without this file needing to know the layout.
    """
    env = os.environ.get('GHOSTLIGHT_WEB')
    candidates = [env] if env else []
    candidates += [os.path.join(_ROOT, 'dist'), os.path.join(_HERE, 'web')]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, 'index.html')):
            return os.path.abspath(c)
    return None


def _resolve(root: str, path: str) -> str | None:
    """Map a URL path to a file inside root, or None if it escapes.

    The containment check is the point. The path arrives from the network, and
    without it a request for ../../ walks straight out of the web root and
    serves anything the process can read.
    """
    path = urllib.parse.unquote(path.split('?', 1)[0].split('#', 1)[0])
    path = posixpath.normpath(path)
    parts = [p for p in path.split('/') if p and p not in ('.', '..')]
    full = os.path.abspath(os.path.join(root, *parts))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def _reply(status: int, reason: str, body: bytes, ctype: str,
           cache: str = 'no-store') -> Response:
    return Response(status, reason, Headers({
        'Content-Type': ctype,
        'Content-Length': str(len(body)),
        'Cache-Control': cache,
    }), body)


def http_handler(connection, request):
    """websockets process_request hook.

    Returning None lets the WebSocket handshake carry on as normal. Returning a
    Response serves an ordinary HTTP request instead, which is how one port ends
    up doing both jobs.
    """
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return None

    root = web_root()
    if root is None:
        return _reply(200, 'OK', NOT_BUILT, TYPES['.html'])

    target = _resolve(root, request.path or '/')
    if target is None:
        return _reply(403, 'Forbidden', b'forbidden', TYPES['.txt'])

    if os.path.isdir(target):
        target = os.path.join(target, 'index.html')

    # Anything unrecognised falls back to the app itself. The front end owns its
    # routing, so a deep link has to reach index.html rather than a 404.
    if not os.path.isfile(target):
        target = os.path.join(root, 'index.html')

    try:
        with open(target, 'rb') as f:
            body = f.read()
    except OSError:
        return _reply(404, 'Not Found', b'not found', TYPES['.txt'])

    ext = os.path.splitext(target)[1].lower()
    ctype = TYPES.get(ext) or mimetypes.guess_type(target)[0] or 'application/octet-stream'

    # Vite fingerprints asset filenames, so those are safe to cache hard. The
    # entry document must not be, or a rebuild leaves the browser holding an
    # index.html that points at assets which no longer exist.
    cache = 'no-store'
    if os.path.dirname(target) == os.path.join(root, 'assets'):
        cache = 'public, max-age=31536000, immutable'

    return _reply(200, 'OK', body, ctype, cache)
