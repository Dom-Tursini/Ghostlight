#!/usr/bin/env python3
"""
Ghostlight launcher.

Starts the capture service, which also serves the interface, and opens a
browser at it. One command, no Node at runtime.

    python ghostlight.py

Options are passed through to the service:

    python ghostlight.py --port 9000
    python ghostlight.py --host 0.0.0.0     serve to other machines on the LAN
    python ghostlight.py --no-open          do not open a browser

Working on the interface itself is a different job. Run `npm run dev` for hot
reload and leave this running alongside it; the dev server connects back here.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'server'))


def preflight() -> list:
    """Problems worth naming before the traceback does it worse.

    An import error deep in a CUDA library is a miserable first experience for
    someone who has just cloned the repo, and the actual cause is nearly always
    one of these three things.
    """
    problems = []
    try:
        import numpy  # noqa: F401
    except ImportError:
        problems.append('Python dependencies are missing. Run:\n'
                        '    pip install -r server/requirements.txt')
    try:
        import cupy  # noqa: F401
    except ImportError:
        problems.append(
            'CuPy is not installed. It is not in requirements.txt because the\n'
            '    wheel is tied to a CUDA major version. Install the one that\n'
            '    matches your toolkit:\n'
            '        pip install cupy-cuda12x      (CUDA 12)\n'
            '        pip install cupy-cuda11x      (CUDA 11)')
    except Exception as e:
        problems.append('CuPy is installed but cannot start: %s\n'
                        '    This usually means the wheel does not match the\n'
                        '    installed CUDA runtime.' % e)
    return problems


def main():
    ap = argparse.ArgumentParser(description='Run Ghostlight')
    ap.add_argument('--host', default=None,
                    help='bind address, 0.0.0.0 to serve other machines')
    ap.add_argument('--port', type=int, default=None, help='default 8787')
    ap.add_argument('--no-open', action='store_true',
                    help='do not open a browser')
    args = ap.parse_args()

    problems = preflight()
    if problems:
        print('Ghostlight cannot start:\n')
        for p in problems:
            print('  * %s\n' % p)
        return 1

    import webapp
    if webapp.web_root() is None:
        print('The interface has not been built yet. Run:\n')
        print('    npm install')
        print('    npm run build\n')
        print('The service will still start and serve a page saying the same '
              'thing, so this is a warning rather than a stop.\n')

    import service
    service.run(args.host, args.port, open_browser=not args.no_open)
    return 0


if __name__ == '__main__':
    sys.exit(main())
