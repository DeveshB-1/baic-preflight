import json

from baic_preflight.checks import BLOCKER, OK, WARN, check_secrets, summarize


def by_level(found, level):
    return {f.subject for f in found if f.level == level}


def test_good_namespace_only_warns_about_ld_probe(spec, fixture):
    found = check_secrets(fixture("secrets-good.json"), spec, "registry.acme.example.com")
    assert by_level(found, BLOCKER) == set()
    assert by_level(found, WARN) == {"launchdarkly"}
    assert summarize(found)[OK] == 15


def test_broken_namespace_finds_each_planted_mistake(spec, fixture):
    found = check_secrets(fixture("secrets-broken.json"), spec, "registry.acme.example.com")
    assert by_level(found, BLOCKER) == {
        "docker-config", "miscellaneous", "rackspace-secret", "launchdarkly",
        "tag-db.jdbc_url", "forge-user-management.APP_JWT_PUBLIC_KEY",
    }
    dc = [f.message for f in found if f.subject == "docker-config"]
    assert any("registry.uniphore.com" in m for m in dc)
    assert any("ImagePullBackOff" in m for m in dc)


def test_findings_never_echo_secret_values(spec, fixture):
    for name in ("secrets-good.json", "secrets-broken.json"):
        text = json.dumps([f.as_dict() for f in check_secrets(fixture(name), spec, "x")])
        for value in ("s3cret", "sdk-123", "MIIE", "MIIB", "robot:pw", "csec"):
            assert value not in text


def test_required_empty_and_placeholder(spec, fixture):
    ns = fixture("secrets-good.json")
    import base64
    for s in ns["items"]:
        if s["metadata"]["name"] == "xforge-db":
            s["data"]["password"] = ""
            s["data"]["host"] = base64.b64encode(b"<YOUR-POSTGRES-HOST>").decode()
            s["data"]["database"] = base64.b64encode(b"baic").decode()
    found = check_secrets(ns, spec)
    msgs = {f.subject: (f.level, f.message) for f in found if f.subject.startswith("xforge-db.")}
    assert msgs["xforge-db.password"][0] == BLOCKER
    assert "placeholder" in msgs["xforge-db.host"][1]
    assert msgs["xforge-db.database"] == (WARN, "differs from the guide's value 'xforge'")


def test_wrong_docker_config_type(spec, fixture):
    ns = fixture("secrets-good.json")
    for s in ns["items"]:
        if s["metadata"]["name"] == "docker-config":
            s["type"] = "Opaque"
    found = check_secrets(ns, spec)
    assert any(f.subject == "docker-config" and f.level == BLOCKER for f in found)
