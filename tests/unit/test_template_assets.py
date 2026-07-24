"""Template asset-URL contract.

Regression cover for the mixed-content bug: in production Caddy terminates TLS
and proxies to the app over plain HTTP, so the app sees scheme="http". An
absolute ``url_for()`` link therefore rendered the stylesheet as
``http://<domain>/static/style.css`` inside a page served over ``https://``.
Browsers block that as mixed content, so no CSS ever loaded and the dashboard
rendered as bare unstyled HTML.

The contract: asset links must never be scheme-absolute, regardless of the
scheme the app believes it is serving.
"""
from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient
from fastapi.templating import Jinja2Templates

_LINK_RE = re.compile(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"')


def _render_login(base_url: str) -> str:
    """Render login.html (extends base.html) and return the stylesheet href.

    Mirrors app/main.py's static mount without importing the full app, which
    would require a live database.
    """
    templates = Jinja2Templates(directory="app/templates")

    async def login(request: Request):
        return templates.TemplateResponse(
            request=request, name="login.html", context={}
        )

    app = Starlette(
        middleware=[Middleware(SessionMiddleware, secret_key="test-only")],
        routes=[
            Route("/login", login),
            Mount("/static", StaticFiles(directory="app/static"), name="static"),
        ],
    )
    with TestClient(app, base_url=base_url) as client:
        html = client.get("/login").text

    match = _LINK_RE.search(html)
    assert match is not None, "no stylesheet <link> rendered"
    return match.group(1)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://arthur.example.com",  # behind TLS proxy, uvicorn sees http
        "https://arthur.example.com",  # direct TLS
    ],
)
def test_stylesheet_link_is_never_scheme_absolute(base_url):
    """A page served over HTTPS must not link an HTTP stylesheet."""
    href = _render_login(base_url)
    assert not href.startswith("http://"), (
        f"stylesheet rendered as {href!r} — an http:// asset on an https:// "
        "page is blocked by browsers as mixed content"
    )
    assert not href.startswith("https://"), (
        f"stylesheet rendered as {href!r} — scheme-absolute asset URLs break "
        "when the proxy scheme differs from what the app sees; use a "
        "root-relative path"
    )


def test_stylesheet_link_is_root_relative():
    """The stylesheet resolves same-origin under any scheme."""
    assert _render_login("http://arthur.example.com") == "/static/style.css"
