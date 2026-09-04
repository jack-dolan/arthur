"""Shared base-template contract: asset URLs, and the header tagline.

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

import pathlib
import re

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

_LINK_RE = re.compile(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"')
_ICON_RE = re.compile(r'<link[^>]+rel="(?:apple-touch-)?icon"[^>]+href="([^"]+)"')
_TAGLINE_RE = re.compile(r'<p class="tagline">(.*?)</p>', re.DOTALL)
_INITIAL_RE = re.compile(r'<span class="tagline-initial">([^<]*)</span>')

TAGLINE = "A Robot That Handles Unglamorous Responsibilities"


def _build_app() -> Starlette:
    """Mirror app/main.py's static mount without importing the full app.

    Importing the real app would require a live database. The templates object
    is the app's own (app/templating.py) rather than a fresh instance, so the
    Jinja globals the real pages rely on — the preview banner's, today — are
    present here too.
    """
    from app.templating import templates

    async def login(request: Request):
        return templates.TemplateResponse(
            request=request, name="login.html", context={}
        )

    return Starlette(
        middleware=[Middleware(SessionMiddleware, secret_key="test-only")],
        routes=[
            Route("/login", login),
            Mount("/static", StaticFiles(directory="app/static"), name="static"),
        ],
    )


def _render_login_html(base_url: str) -> str:
    """Render login.html (extends base.html) and return the whole document."""
    with TestClient(_build_app(), base_url=base_url) as client:
        return client.get("/login").text


def _render_login(base_url: str) -> str:
    """Render login.html and return the stylesheet href."""
    match = _LINK_RE.search(_render_login_html(base_url))
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


def test_favicon_links_are_rendered():
    """Every page carries the Arthur icon, including the pre-login page."""
    hrefs = _ICON_RE.findall(_render_login_html("https://arthur.example.com"))
    assert "/static/favicon.ico" in hrefs, (
        f"no /static/favicon.ico <link rel='icon'> rendered — found {hrefs!r}"
    )
    assert "/static/apple-touch-icon.png" in hrefs, (
        "no apple-touch-icon <link> rendered — iOS home-screen bookmarks fall "
        f"back to a screenshot without one; found {hrefs!r}"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://arthur.example.com",  # behind TLS proxy, uvicorn sees http
        "https://arthur.example.com",  # direct TLS
    ],
)
def test_favicon_links_are_never_scheme_absolute(base_url):
    """Icons obey the same mixed-content contract as the stylesheet."""
    for href in _ICON_RE.findall(_render_login_html(base_url)):
        assert not href.startswith(("http://", "https://")), (
            f"icon rendered as {href!r} — scheme-absolute asset URLs break "
            "when the proxy scheme differs from what the app sees"
        )


def test_favicon_assets_are_actually_served():
    """The linked icon files exist under the static mount and are non-empty.

    A <link> pointing at a missing file is a silent 404: the browser just shows
    its default page icon, which looks identical to having no favicon at all.
    """
    hrefs = _ICON_RE.findall(_render_login_html("https://arthur.example.com"))
    assert hrefs, "no icon links to check"
    with TestClient(_build_app()) as client:
        for href in hrefs:
            response = client.get(href)
            assert response.status_code == 200, (
                f"{href} is linked but returns {response.status_code}"
            )
            assert len(response.content) > 0, f"{href} is served but empty"


def test_favicon_ico_carries_a_16px_and_a_32px_entry():
    """Browsers pick per-context sizes; a single large entry gets downscaled.

    Reads the ICO directory header directly: 6-byte file header, then one
    16-byte entry per image whose first two bytes are width and height (0
    meaning 256).
    """
    import struct

    raw = pathlib.Path("app/static/favicon.ico").read_bytes()
    reserved, image_type, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, image_type) == (0, 1), "not a well-formed ICO header"
    assert count >= 2, f"favicon.ico declares only {count} size(s)"

    sizes = set()
    for index in range(count):
        start = 6 + 16 * index
        width, height = raw[start], raw[start + 1]
        sizes.add((width or 256, height or 256))

    assert (16, 16) in sizes, f"no 16x16 entry — sizes present: {sorted(sizes)}"
    assert (32, 32) in sizes, f"no 32x32 entry — sizes present: {sorted(sizes)}"


def test_heading_names_arthur_so_the_tagline_has_something_to_expand():
    """The subtitle is a backronym, so the heading has to carry the name.

    Without it the tagline reads as an unexplained sentence under an unrelated
    heading, which is exactly how it shipped first time round.
    """
    html = _render_login_html("https://arthur.example.com")
    match = re.search(r"<h1>(.*?)</h1>", html, re.DOTALL)
    assert match is not None, "no <h1> rendered"

    heading = " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split())
    assert heading == "ARTHUR - Rental Dashboard", f"heading reads {heading!r}"


def test_browser_tab_title_does_not_mention_arthur():
    """Deliberate: Jack asked for the heading only, not the tab title."""
    html = _render_login_html("https://arthur.example.com")
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert match is not None, "no <title> rendered"

    assert "ARTHUR" not in match.group(1).upper(), (
        f"tab title reads {match.group(1)!r} — it is meant to stay as it was"
    )


def test_header_carries_the_arthur_tagline():
    """The backronym shows under the dashboard heading, on every page."""
    match = _TAGLINE_RE.search(_render_login_html("https://arthur.example.com"))
    assert match is not None, "no <p class='tagline'> rendered in the header"

    visible = re.sub(r"<[^>]+>", "", match.group(1))
    visible = " ".join(visible.split())
    assert visible == TAGLINE, f"tagline reads {visible!r}, expected {TAGLINE!r}"


def test_tagline_initials_spell_arthur():
    """The whole joke is the acronym, so guard it against a reworded tagline.

    Each word's first letter is wrapped in its own span so the stylesheet can
    pick it out. If someone edits the wording and breaks ARTHUR, this fails
    rather than silently shipping a tagline that no longer spells anything.
    """
    match = _TAGLINE_RE.search(_render_login_html("https://arthur.example.com"))
    assert match is not None, "no <p class='tagline'> rendered in the header"

    initials = "".join(_INITIAL_RE.findall(match.group(1)))
    assert initials == "ARTHUR", (
        f"the emphasised letters spell {initials!r}, not 'ARTHUR'"
    )

    # The marked-up letters must be the real first letters, not decoration
    # bolting an unrelated acronym onto whatever the words happen to say.
    assert "".join(word[0] for word in TAGLINE.split()) == "ARTHUR"


def test_icon_master_is_square_and_kept_in_repo():
    """The full-resolution crop stays committed so icons can be regenerated.

    The upstream meme URL is not a dependency we control; if it goes away, this
    file is the only source the smaller sizes can be rebuilt from. Reads the
    PNG IHDR chunk, whose width and height are big-endian uint32 at byte 16.
    """
    import struct

    raw = pathlib.Path("app/static/arthur-fist.png").read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "master icon is not a PNG"
    width, height = struct.unpack(">II", raw[16:24])
    assert width == height, f"master icon is {width}x{height}, not square"
    assert width >= 180, (
        f"master icon is only {width}px — too small to regenerate the "
        "180px apple-touch-icon without upscaling"
    )
