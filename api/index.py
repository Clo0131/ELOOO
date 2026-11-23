"""Vercel entrypoint.

Vercel loads the `handler` class below for every request. vercel.json rewrites
all paths to this function, and a rewrite keeps the original request path, so
frontend.Handler still does the routing (pages, /invoice/<id>, /font/... and
the /api/* endpoints).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frontend import Handler      # noqa: E402  (path must be set up first)
from logic import init_db         # noqa: E402

# The local server calls init_db() from main(); a serverless function has no main().
init_db()


class handler(Handler):
    pass
