"""Registry checks against a real HTTP server that speaks the OCI distribution
manifest endpoint with Harbor-style Bearer token auth."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from baic_preflight.checks import BLOCKER, OK, Registry, _split_ref, check_registry

USER, PASSWORD, TOKEN = "robot$acme+puller", "pw", "tok-123"


def make_server(present: set[str], bearer=True):
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, headers=None, body=b""):
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/service/token"):
                want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    return self._send(401)
                assert "scope=repository%3A" in self.path
                return self._send(200, {"Content-Type": "application/json"},
                                  json.dumps({"token": TOKEN}).encode())
            return self._send(404)

        def do_HEAD(self):
            hits.append(self.path)
            if bearer and self.headers.get("Authorization") != f"Bearer {TOKEN}":
                realm = f"http://127.0.0.1:{self.server.server_port}/service/token"
                return self._send(401, {"WWW-Authenticate":
                                        f'Bearer realm="{realm}",service="harbor-registry"'})
            repo, _, tag = self.path.removeprefix("/v2/").partition("/manifests/")
            self._send(200 if f"{repo}:{tag}" in present else 404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits


@pytest.fixture
def mirror(spec):
    present = {f"baic/{n}:{t}" for n, t in spec.images.items() if n != "deep-crawler"}
    present |= {f"baic-charts/{n}:{v}" for n, v in spec.charts.items()}
    srv, hits = make_server(present)
    yield srv, hits
    srv.shutdown()


def factory(user=USER, password=PASSWORD):
    return lambda host: Registry(f"http://{host}", user, password)


def test_split_ref():
    assert _split_ref("oci://reg.example.com:5000/a/b/") == ("reg.example.com:5000", "a/b")
    assert _split_ref("reg.example.com") == ("reg.example.com", "")


def test_bearer_flow_and_missing_image(spec, mirror):
    srv, hits = mirror
    host = f"127.0.0.1:{srv.server_port}"
    found = check_registry(spec, f"{host}/baic", f"oci://{host}/baic-charts", factory())
    blockers = [f.subject for f in found if f.level == BLOCKER]
    assert blockers == [f"image baic/deep-crawler:{spec.images['deep-crawler']}", "images"]
    assert any(f.subject == "charts" and f.level == OK and "15/15" in f.message for f in found)
    assert len(hits) == 29 + 29  # one 401 challenge + one authed HEAD per artifact


def test_bad_credentials_are_reported_as_auth(spec, mirror):
    srv, _ = mirror
    host = f"127.0.0.1:{srv.server_port}"
    found = check_registry(spec, f"{host}/baic", None, factory(password="wrong"))
    assert all("HTTP 401" in f.message for f in found if f.subject.startswith("image "))


def test_unreachable_registry(spec):
    found = check_registry(spec, "127.0.0.1:1/baic", None, factory())
    assert len(found) == 1 and "unreachable" in found[0].message


def test_plus_in_chart_version_becomes_underscore(spec):
    seen = []

    class Fake:
        def manifest_status(self, repo, tag):
            seen.append(tag)
            return 200

    spec2 = type(spec)(**{**spec.__dict__, "charts": {"x": "1.0.0+build.1"}})
    check_registry(spec2, None, "oci://r/c", lambda h: Fake())
    assert seen == ["1.0.0_build.1"]


@pytest.mark.online
def test_real_docker_hub_bearer_flow():
    reg = Registry("https://registry-1.docker.io")
    try:
        assert reg.manifest_status("library/busybox", "1.36") == 200
        assert reg.manifest_status("library/busybox", "no-such-tag-zz") == 404
    except OSError as e:
        pytest.skip(f"offline: {e}")
