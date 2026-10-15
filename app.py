"""Vercel's entrypoint: the xo server (`xo serve`) as a WSGI app. See docs/deploy-vercel.md.

Settings come from the Vercel project's environment variables, never from this repo.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from xo.server import wsgi_app  # noqa: E402

app = wsgi_app()
