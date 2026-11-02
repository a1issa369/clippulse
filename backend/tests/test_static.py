import base64
import gzip
import json

import pytest

from clippulse.api import App
from clippulse.static import CSP, StaticSite


@pytest.fixture
def site_dir(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>ClipPulse</title>" + "x" * 500)
    (tmp_path / "assets" / "main-ABC123.js").write_text("console.log('hi');" * 50)
    (tmp_path / "assets" / "main-ABC123.css").write_text("body{margin:0}")
    (tmp_path / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "logo.png").write_bytes(bytes(range(256)))
    (tmp_path.parent / "secret.txt").write_text("do not serve")
    return tmp_path


def get(site, path, method="GET", enc=""):
    return site.serve(method, path, enc)


def test_root_serves_index_html_uncached(site_dir):
    r = get(StaticSite(site_dir), "/")
    assert r["statusCode"] == 200 and "ClipPulse" in r["body"] and not r.get("isBase64Encoded")
    assert r["headers"]["content-type"] == "text/html; charset=utf-8"
    assert r["headers"]["cache-control"] == "no-cache"


def test_hashed_assets_are_cached_forever_and_have_correct_types(site_dir):
    site = StaticSite(site_dir)
    js, css = get(site, "/assets/main-ABC123.js"), get(site, "/assets/main-ABC123.css")
    assert js["headers"]["content-type"].startswith("text/javascript")
    assert css["headers"]["content-type"].startswith("text/css")
    assert js["headers"]["cache-control"] == "public, max-age=31536000, immutable"


def test_every_response_carries_the_security_headers(site_dir):
    for path in ("/", "/assets/main-ABC123.js", "/nope"):
        h = get(StaticSite(site_dir), path)["headers"]
        assert h["content-security-policy"] == CSP and h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY" and "strict-transport-security" in h


def test_gzip_only_when_the_browser_accepts_it(site_dir):
    site = StaticSite(site_dir)
    plain = get(site, "/assets/main-ABC123.js")
    zipped = get(site, "/assets/main-ABC123.js", enc="gzip, deflate, br")
    assert "content-encoding" not in plain["headers"] and not plain.get("isBase64Encoded")
    assert zipped["headers"]["content-encoding"] == "gzip" and zipped["isBase64Encoded"] is True
    assert gzip.decompress(base64.b64decode(zipped["body"])).decode() == plain["body"]
    assert len(base64.b64decode(zipped["body"])) < len(plain["body"])
    assert zipped["headers"]["vary"] == "accept-encoding"


def test_binary_files_are_base64_and_never_gzipped(site_dir):
    r = get(StaticSite(site_dir), "/logo.png", enc="gzip")
    assert r["isBase64Encoded"] is True and base64.b64decode(r["body"]) == bytes(range(256))
    assert "content-encoding" not in r["headers"]


def test_head_returns_headers_without_a_body(site_dir):
    r = get(StaticSite(site_dir), "/", method="HEAD")
    assert r["statusCode"] == 200 and r["body"] == "" and "content-type" in r["headers"]


@pytest.mark.parametrize("path", ["/missing.js", "/assets", "/../secret.txt", "/%2e%2e/secret.txt",
                                  "/assets/../../secret.txt", "/%2e%2e%2fsecret.txt", "/a\x00b"])
def test_unknown_and_traversal_paths_are_404(site_dir, path):
    r = get(StaticSite(site_dir), path)
    assert r["statusCode"] == 404 and "do not serve" not in r["body"]


def test_missing_build_gives_a_helpful_404(tmp_path):
    r = get(StaticSite(tmp_path / "does-not-exist"), "/")
    assert r["statusCode"] == 404 and "deploy.sh" in r["body"]


# --- wired into the API app -------------------------------------------------

@pytest.fixture
def app(service, site_dir):
    return App(lambda: service, static_dir=site_dir)


def event(method, path, headers=None):
    return {"rawPath": path, "headers": headers or {}, "requestContext": {"http": {"method": method, "path": path}}}


def test_website_and_api_share_one_app(app):
    assert "ClipPulse" in app.handle(event("GET", "/"))["body"]
    assert json.loads(app.handle(event("GET", "/api/health"))["body"]) == {"ok": True}


def test_gzip_is_negotiated_from_event_headers(app):
    r = app.handle(event("GET", "/assets/main-ABC123.js", {"Accept-Encoding": "gzip"}))
    assert r["headers"]["content-encoding"] == "gzip"


def test_api_paths_never_fall_through_to_the_website(app):
    r = app.handle(event("GET", "/api/missing"))
    assert r["statusCode"] == 404 and json.loads(r["body"])["error"]["code"] == "not_found"
    assert app.handle(event("GET", "/apix"))["statusCode"] == 404          # not the API, and not a file
    assert json.loads(app.handle(event("GET", "/api"))["body"])["error"]["code"] == "not_found"


def test_website_rejects_writes(app):
    assert app.handle(event("POST", "/"))["statusCode"] == 405
