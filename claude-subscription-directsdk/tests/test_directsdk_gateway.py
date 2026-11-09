"""#46: one explicit, per-profile, fail-closed opt-in routes the Claude login through an org gateway
(upstream_url), an HTTP CONNECT proxy (upstream_proxy) and a TLS-inspecting CA (upstream_ca_file). Every relay
uses it (turn, /usage, picker); an inherited ANTHROPIC_BASE_URL stays refused; a bad setting never falls back
to the direct route."""
import json
import logging
import os
import ssl
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_NAME, popen_from_run
from test_directsdk_admission import NATIVE

REQUEST = dict(model="sonnet", messages=[{"role": "user", "content": "go"}])
PROXY_AUTH = "CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY_AUTH"


def _settings(**values):
    from hermes_cli.config import get_config_path
    lines = "".join(f"        {k}: '{v}'\n" for k, v in values.items())
    get_config_path().write_text(f"plugins:\n  enabled: []\n  entries:\n    {PLUGIN_NAME}:\n      settings:\n{lines}",
                                 encoding="utf-8")


class _Gateway:
    """A loopback 'org gateway' that answers one streamed message and records each request."""

    def __init__(self, context=None):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                seen.append((self.path, self.headers.get("Authorization")))
                usage = {"input_tokens": 1, "output_tokens": 1}
                events = [{"type": "message_start", "message": {"id": "gw", "role": "assistant", "model": "sonnet",
                                                                "content": [], "usage": usage}},
                          {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                          {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "GW"}},
                          {"type": "content_block_stop", "index": 0},
                          {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage},
                          {"type": "message_stop"}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        if context is not None:
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def clean(profile, monkeypatch, tmp_path):
    import directsdk_setup
    for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", PROXY_AUTH):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    getattr(directsdk_setup, "_warned", set()).clear()  # the bearer-host warning is once per process and route
    return profile


def _turn(tmp_path):
    import directsdk
    native = tmp_path / "native.py"
    native.write_text(NATIVE, encoding="utf-8")
    client = directsdk.Client(command=[sys.executable, str(native)])
    try:
        return client.create(**REQUEST)
    finally:
        client.close()


def test_turns_go_to_the_opted_in_gateway(clean, tmp_path, caplog, monkeypatch):
    gateway = _Gateway()
    try:
        _settings(upstream_url=f"http://127.0.0.1:{gateway.port}/org/anthropic")
        with caplog.at_level(logging.WARNING):
            assert _turn(tmp_path).choices[0].message.content == "GW"
            _turn(tmp_path)
        # The opt-in is the plugin setting only: an inherited ANTHROPIC_BASE_URL stays refused.
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example.com")
        with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL"):
            _turn(tmp_path)
    finally:
        gateway.close()
    assert [path for path, _ in gateway.seen] == ["/org/anthropic/v1/messages"] * 2
    warnings = [r.getMessage() for r in caplog.records if "bearer token goes to" in r.getMessage()]
    assert warnings == [warnings[0]] and "127.0.0.1" in warnings[0]


@pytest.mark.parametrize("probe", ["fetch_usage", "discover_models", "turn"])
def test_the_clis_own_calls_take_the_configured_proxy_and_ca(clean, tmp_path, monkeypatch, probe):
    """Greptile 4511161868. Only Messages requests follow ANTHROPIC_BASE_URL; get_usage, bootstrap, the MCP directory
    and telemetry go to api.anthropic.com from the CLI itself. With upstream_proxy / upstream_ca_file set they must
    take that proxy and CA too (an org that requires egress through it), while the loopback relay stays direct. An IPv6
    proxy keeps its brackets (Greptile 4512444873)."""
    import directsdk_setup
    ca, _ = _tls_pair(tmp_path)
    monkeypatch.chdir(tmp_path)  # a relative CA path is Hermes' cwd, and the CLI runs in another one
    _settings(upstream_url="https://llm-gateway.example.com/anthropic", upstream_proxy="http://[::1]:3128",
              upstream_ca_file=ca.name)
    monkeypatch.setenv(PROXY_AUTH, "svc:p@ss")
    monkeypatch.setenv("NO_PROXY", "internal.example.com")
    seen = []
    if probe == "turn":
        native = tmp_path / "native.py"
        native.write_text("import json, os\nopen(os.environ['ENV_CAPTURE'], 'w').write(json.dumps(dict(os.environ)))\n"
                          + NATIVE, encoding="utf-8")
        monkeypatch.setenv("ENV_CAPTURE", str(tmp_path / "env.json"))
        import directsdk
        client = directsdk.Client(command=[sys.executable, str(native)], timeout=5)
        try:
            with pytest.raises(Exception):  # nothing listens at the gateway; the spawn env is what is checked
                client.create(**REQUEST)
        finally:
            client.close()
        seen.append(json.loads((tmp_path / "env.json").read_text()))
    else:
        status = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}

        def run(argv, **kwargs):
            if argv[1:3] == ["auth", "status"]:
                return SimpleNamespace(stdout=json.dumps(status), returncode=0)
            seen.append(kwargs["env"])
            return SimpleNamespace(stdout="", returncode=0)
        monkeypatch.setattr(directsdk_setup.subprocess, "run", run)
        monkeypatch.setattr(directsdk_setup.subprocess, "Popen", popen_from_run(run))
        getattr(directsdk_setup, probe)(command=[sys.executable], env=dict(os.environ, PATH=os.defpath))
    env, = seen
    assert _spellings(env, "HTTPS_PROXY") == ["http://svc:p%40ss@[::1]:3128"] * _SPELLINGS
    assert env["NODE_EXTRA_CA_CERTS"] == str(ca)
    assert [value.split(",") for value in _spellings(env, "NO_PROXY")] == [
        ["internal.example.com", "127.0.0.1", "localhost"]] * _SPELLINGS
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")


# Windows env names ignore case: one entry there, both spellings on POSIX.
_SPELLINGS = 1 if os.name == "nt" else 2


def _spellings(env, name):
    return [value for key, value in env.items() if key.upper() == name]


@pytest.mark.parametrize("raw, named", [
    ("upstream_url: 'http://gateway.example.com'", "upstream_url"),
    ("upstream_url: 'https://user:pw@gateway.example.com'", "upstream_url"),
    ("upstream_url: 'https://gateway.example.com/?key=1'", "upstream_url"),
    ("upstream_proxy: 'socks5://egress.example.com:1080'", "upstream_proxy"),
    ("upstream_proxy: 'http://user:pw@egress.example.com:3128'", "upstream_proxy"),
    ("upstream_ca_file: '/no/such/ca.pem'", "upstream_ca_file"),
    # A non-string value (Greptile 4511161853) used to read as unset, so the turn went direct.
    ("upstream_proxy: false", "upstream_proxy"), ("upstream_url: 123", "upstream_url"),
    ("upstream_ca_file: [a.pem]", "upstream_ca_file"),
])
def test_a_bad_gateway_setting_refuses_and_never_goes_direct(clean, tmp_path, monkeypatch, raw, named):
    import directsdk_setup
    from agent.error_classifier import classify_api_error
    from hermes_cli.config import get_config_path
    get_config_path().write_text(f"plugins:\n  enabled: []\n  entries:\n    {PLUGIN_NAME}:\n      settings:\n"
                                 f"        {raw}\n", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(directsdk_setup.subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(directsdk_setup.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(directsdk_setup.ClaudeUpstreamConfigError, match=named) as raised:
        _turn(tmp_path)
    verdict = classify_api_error(raised.value, provider=PLUGIN_NAME, model="sonnet")
    assert not verdict.retryable and not verdict.should_fallback
    limits, reason = directsdk_setup.fetch_usage(command=[sys.executable], env={"PATH": os.defpath})
    assert limits is None and named in reason
    assert directsdk_setup.discover_models(command=[sys.executable], env={"PATH": os.defpath}) is None
    assert spawned == []


def _tls_pair(tmp_path):
    """A throwaway CA and a localhost certificate it signed."""
    pytest.importorskip("cryptography")
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = datetime.now(timezone.utc)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "inspecting proxy CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(2)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(pem) + leaf_key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp_path / "leaf.pem")
    return tmp_path / "ca.pem", server


def _post_through_relay(upstream):
    import directsdk_setup
    gate = directsdk_setup.relay(upstream, 5)
    try:
        request = urllib.request.Request(gate.url + "/v1/messages", data=b"{}", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except OSError:
            pass
        return gate.status, gate.failure
    finally:
        gate.close()


def test_the_ca_file_adds_trust_and_keeps_certificate_checks(clean, tmp_path):
    import directsdk_setup
    ca, server = _tls_pair(tmp_path)
    gateway = _Gateway(server)
    try:
        url = f"https://localhost:{gateway.port}"
        _settings(upstream_url=url)
        status, _ = _post_through_relay(directsdk_setup.resolve_upstream())
        assert gateway.seen == [] and status is None  # system roots alone refuse the inspecting CA
        _settings(upstream_url=url, upstream_ca_file=str(ca))
        upstream = directsdk_setup.resolve_upstream()
        assert upstream.context.verify_mode == ssl.CERT_REQUIRED and upstream.context.check_hostname
        result = _post_through_relay(upstream)
        assert result[0] == 200 and len(gateway.seen) == 1, result
        _settings(upstream_url=f"https://127.0.0.1:{gateway.port}", upstream_ca_file=str(ca))
        assert _post_through_relay(directsdk_setup.resolve_upstream())[0] is None  # hostname check stays on
        assert len(gateway.seen) == 1
    finally:
        gateway.close()
