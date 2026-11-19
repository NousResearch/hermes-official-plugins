import json

import pytest

from hermes_security.errors import PolicyDenied


class FakeService:
    def __init__(self):
        self.calls = []
        self.result = {"scanId": "scan_test"}
        self.error = None

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return self.result
        return call


def invoke(service, suffix, args):
    from hermes_security.tools import make_handler
    return json.loads(make_handler("security_scan_" + suffix, lambda: service)(args, task_id="ignored"))


def test_validation_never_authorizes():
    service = FakeService()
    assert invoke(service, "record_validations", {"scan_id": "s", "plans": []})["ok"]
    assert service.calls[-1][2]["user_authorized"] is False
    for key in ("user_authorized", "origins", "created_by", "grant", "allowLocalValidation"):
        assert not invoke(service, "record_validations", {"scan_id": "s", key: True})["ok"]
    assert len(service.calls) == 1


def test_errors_do_not_leak_unexpected_exception():
    service = FakeService()
    service.error = PolicyDenied("No authorization")
    assert invoke(service, "get", {"scan_id": "s"}) == {
        "ok": False, "error": {"code": "policy_denied", "message": "No authorization"}}
    service.error = RuntimeError("private file contents")
    result = invoke(service, "get", {"scan_id": "s"})
    assert result["error"]["code"] == "internal_error"
    assert "private" not in json.dumps(result)


def test_result_cap_stays_valid_json():
    from hermes_security.tools import make_handler, MAX_RESULT_CHARS
    service = FakeService()
    service.result = {"items": ["x" * 70000]}
    raw = make_handler("security_scan_get", lambda: service)({"scan_id": "s"})
    assert len(raw) <= MAX_RESULT_CHARS
    result = json.loads(raw)
    assert result["ok"] and result["truncated"] and result["pagination_hint"]
    assert result["scan_id"] == "s"
    assert result["offset"] == 0
    service.result = {"scanId": "new_scan", "snapshotDigest": "sha256:abc", "methodologyVersion": "m1",
                      "submit": {"tool": "security_scan_submit_worker_result"}, "secret_blob": "y" * 600,
                      "packets": [{"packetId": "pkt_" + str(i), "files": ["x" * 70000]} for i in range(3)]}
    raw = make_handler("security_scan_start", lambda: service)({"path": "/repo"})
    result = json.loads(raw)
    assert len(raw) <= MAX_RESULT_CHARS and result["scan_id"] == "new_scan" and result["result_omitted"]
    # The truncated envelope keeps the small header an agent needs to continue; bulk and long values stay out.
    header = result["header"]
    assert header["scanId"] == "new_scan" and header["snapshotDigest"] == "sha256:abc"
    assert header["methodologyVersion"] == "m1" and header["submit"]["tool"] == "security_scan_submit_worker_result"
    assert header["packetIds"] == ["pkt_0", "pkt_1", "pkt_2"]
    assert "secret_blob" not in header and "packets" not in header and "summary" in result["pagination_hint"]


@pytest.mark.parametrize("suffix,args,method", [
    ("start", {"path": "/repo"}, "start_scan"),
    ("get", {"scan_id": "s", "limit": 2, "offset": 1}, "get_scan"),
    ("checkpoint", {"scan_id": "s", "coverage": []}, "checkpoint"),
    ("submit_worker_result", {"payload": {"scanId": "s"}}, "submit_worker_result"),
    ("record_chains", {"scan_id": "s", "explanations": {}}, "record_chains"),
    ("finalize", {"scan_id": "s"}, "finalize"),
    ("cancel", {"scan_id": "s"}, "cancel"),
    ("export", {"scan_id": "s", "fmt": "json"}, "export"),
])
def test_tools_forward(suffix, args, method):
    service = FakeService()
    assert invoke(service, suffix, args)["ok"]
    assert service.calls[-1][0] == method


@pytest.mark.parametrize("args", [None, [], {}, {"scan_id": 7}, {"scan_id": "s", "limit": True},
                                      {"scan_id": "s", "offset": -1}, {"scan_id": "s", "unknown": 1}])
def test_bad_arguments(args):
    service = FakeService()
    assert invoke(service, "get", args)["error"]["code"] == "invalid_input"
    assert not service.calls
