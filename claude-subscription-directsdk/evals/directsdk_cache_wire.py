"""Offline native cache-prefix regression (no inference or account credentials).

Run: python3 evals/directsdk_cache_wire.py /absolute/path/to/claude
Uses the real DirectSDK and native binary, with a synthetic loopback Messages peer.
Checks exact cached-prefix reuse, not cache hit rates; usage is deliberately synthetic.
After a tool loop longer than the cache lookback, the next turn must carry a breakpoint on the
prefix written before the loop's first thinking block (#122).
Only compact hash receipts go to stdout. No raw traces or fixtures are retained.
"""
import argparse
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


THINKING = {"type": "thinking", "thinking": "PUBLIC SYNTHETIC THINKING",
            "signature": "public-fixture-opaque-signature"}
TOOLS = [{"type": "function", "function": {"name": "probe", "description": "Public probe",
          "parameters": {"type": "object", "properties": {}}}}]
USAGE = {"input_tokens": 101, "output_tokens": 37, "cache_read_input_tokens": 211,
         "cache_creation_input_tokens": 313}
# Pure tool continuation, two ordinary user turns, then a 14-round loop with thinking and the turn after it.
SCHEDULE = [True, True, False, False] + [True] * 14 + [False, True]
LOOKBACK = 20


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def cut(prompt, at):
    i, j = at
    prefix = copy.deepcopy(prompt)
    prefix["messages"] = prefix["messages"][:i + 1]
    prefix["messages"][-1]["content"] = prefix["messages"][-1]["content"][:j + 1]
    return prefix


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def content(value):
    # Directives move to the newest breakpoint; they are not cached prompt text.
    if isinstance(value, dict):
        return {key: content(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [content(item) for item in value]
    return value


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.split("?")[0] == "/v1/messages/count_tokens":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        if self.path.split("?")[0] != "/v1/messages":
            self.send_error(403)
            return
        self.server.wires.append(body)
        number = len(self.server.wires)
        blocks = [THINKING, {"type": "tool_use", "id": f"toolu_public_{number}",
                  "name": body["tools"][0]["name"], "input": {}}] if self.server.tools else [
                      {"type": "text", "text": "PUBLIC SYNTHETIC ANSWER"}]
        stop = "tool_use" if self.server.tools else "end_turn"
        message = {"id": f"msg_public_{number}", "type": "message", "role": "assistant",
                   "model": body["model"], "content": blocks, "stop_reason": stop,
                   "stop_sequence": None, "usage": USAGE}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(event):
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

        emit({"type": "message_start", "message": {**message, "content": [], "stop_reason": None}})
        for index, block in enumerate(blocks):
            kind = block["type"]
            start = ({"type": kind, "thinking": "", "signature": ""} if kind == "thinking"
                     else {**block, "input": {}} if kind == "tool_use" else {"type": kind, "text": ""})
            emit({"type": "content_block_start", "index": index, "content_block": start})
            deltas = ([{"type": "thinking_delta", "thinking": block["thinking"]},
                       {"type": "signature_delta", "signature": block["signature"]}] if kind == "thinking"
                      else [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
                      if kind == "tool_use" else [{"type": "text_delta", "text": block["text"]}])
            for delta in deltas:
                emit({"type": "content_block_delta", "index": index, "delta": delta})
            emit({"type": "content_block_stop", "index": index})
        emit({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": USAGE})
        emit({"type": "message_stop"})


def run(binary, model):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        "cache_directsdk", root / "directsdk.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    with tempfile.TemporaryDirectory(prefix="directsdk-cache-") as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Peer) as peer:
        peer.wires, peer.tools = [], True
        worker = threading.Thread(target=peer.serve_forever, daemon=True)
        worker.start()
        env = {"PATH": os.defpath, "HOME": tmp, "HERMES_HOME": tmp,
               "CLAUDE_CONFIG_DIR": str(Path(tmp) / "config"), "XDG_CONFIG_HOME": tmp,
               "ANTHROPIC_API_KEY": "sk-ant-public-offline-fixture",
               "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{peer.server_port}",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
               "DISABLE_ERROR_REPORTING": "1", "NO_PROXY": "127.0.0.1,localhost",
               "no_proxy": "127.0.0.1,localhost"}
        env.update({key: "http://127.0.0.1:1" for key in
                    ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
        version = subprocess.check_output([str(binary), "--version"], env=env,
                                          stdin=subprocess.DEVNULL, text=True, timeout=15).strip()
        client = native.Client(command=str(binary), env=env, timeout=45)
        history = [{"role": "system", "content": "PUBLIC SYNTHETIC SYSTEM\n" * 300}]
        for i in range(12):
            history.extend([{"role": "user", "content": f"Public earlier question {i}"},
                            {"role": "assistant", "content": f"Public earlier answer {i}"}])
        history.append({"role": "user", "content": "Run the public probe."})
        receipts, previous, written, followups = [], None, set(), []
        try:
            for round_number, tools in enumerate(SCHEDULE):
                peer.tools = tools
                original = copy.deepcopy(history)
                start = len(peer.wires)
                result = client.create(model=model, messages=history, tools=TOOLS)
                assert history == original, "caller history mutated"
                assert len(peer.wires) == start + 1, "unexpected extra inference request"
                wire = peer.wires[-1]
                markers = [node["cache_control"] for node in nodes(wire) if "cache_control" in node]
                fixed = [block.get("cache_control") for block in wire["tools"] + wire["system"]]
                message_markers = [(i, j) for i, msg in enumerate(wire["messages"]) if isinstance(msg["content"], list)
                                   for j, block in enumerate(msg["content"]) if "cache_control" in block]
                assert len(markers) <= 4 and 1 <= len(message_markers) <= 2, "extra cache breakpoints"
                assert all(marker == markers[0] for marker in markers), "mixed native cache TTLs"
                prompt = content({key: wire[key] for key in ("tools", "system", "messages")})
                if previous is not None:
                    old, old_fixed, at = previous
                    candidate = cut(prompt, at)
                    assert candidate == old, f"cached prefix changed at round {round_number}: {digest(old)} != {digest(candidate)}"
                    assert fixed == old_fixed, "native cache TTL/breakpoint policy changed"
                    receipts.append({"round": round_number, "old_prefix_sha256": digest(old),
                                     "replayed_prefix_sha256": digest(candidate)})
                if round_number == len(SCHEDULE) - 1:
                    # The turn after the long loop: the recurring breakpoint is too far from the loop's
                    # opening frame, so a second one must sit on it, on a prefix an earlier request wrote.
                    (i, j), last = message_markers[0], message_markers[-1]
                    opening = next(k for k, msg in enumerate(wire["messages"]) if msg["role"] == "user" and
                                   isinstance(msg["content"], list) and
                                   {"type": "text", "text": followups[-2]} in content(msg["content"]))
                    assert len(message_markers) == 2 and i == opening, f"no breakpoint on the loop's opening frame: {message_markers}"
                    span = sum(len(msg["content"]) if isinstance(msg["content"], list) else 1
                               for msg in wire["messages"][i:last[0]]) - j + last[1] + 1
                    assert span > LOOKBACK and digest(cut(prompt, (i, j))) in written, "turn breakpoint not on a written prefix"
                    receipts.append({"round": round_number, "turn_breakpoint": [i, j], "positions_to_recurring": span})
                written.update(digest(cut(prompt, at)) for at in message_markers)
                previous = cut(prompt, message_markers[-1]), fixed, message_markers[-1]
                if round_number in (1, 2, 5):
                    assert THINKING in list(nodes(wire["messages"])), "signed thinking lost or changed"
                assistant = result.choices[0].message.model_dump()
                history.append(assistant)
                if peer.tools:
                    history.extend({"role": "tool", "tool_call_id": call["id"],
                                    "content": "Public tool result\n" * 1000}
                                   for call in assistant["tool_calls"])
                else:
                    followups.append(f"Public followup {round_number}")
                    history.append({"role": "user", "content": followups[-1]})
            # A fresh conversation: native's environment message must read the same on its opening request and on
            # every later one; the opening request carries the date there, so later ones must too (#38).
            fresh, environments = [{"role": "system", "content": "PUBLIC FRESH SYSTEM"},
                                   {"role": "user", "content": "Run the public probe."}], []
            for tools in (True, True, False):
                peer.tools = tools
                assistant = client.create(model=model, messages=fresh, tools=TOOLS).choices[0].message.model_dump()
                environment = peer.wires[-1]["messages"][1]["content"]
                environments.append(environment if isinstance(environment, str) else
                                    "".join(block.get("text", "") for block in environment))
                fresh.append(assistant)
                fresh.extend({"role": "tool", "tool_call_id": call["id"], "content": "ok"} for call in assistant["tool_calls"] or ())
            assert len(set(environments)) == 1, "native environment message changed after the opening request"
            # A Hermes auxiliary one-shot (a summary: system plus one user turn, no tools) keeps native's system
            # marks but carries no message mark: nothing replays its turn, so a mark would buy a never-read write.
            peer.tools = False
            single_turn = [{"role": "system", "content": "PUBLIC SUMMARY POLICY\n" * 50},
                           {"role": "user", "content": "Public transcript to summarize.\n" * 200}]
            # The same single tool-less turn from a chat (no auxiliary mark) keeps its message mark: turn 2 reads it.
            client.create(model=model, messages=single_turn)
            assert [block for msg in peer.wires[-1]["messages"] if isinstance(msg["content"], list)
                    for block in msg["content"] if "cache_control" in block], "tool-free chat turn lost its message mark"
            start = len(peer.wires)
            client.create(model=model, messages=single_turn, hermes_auxiliary=True)
            assert len(peer.wires) == start + 1, "unexpected extra inference request"
            one_shot = peer.wires[-1]
            one_shot_marks = [block for msg in one_shot["messages"] if isinstance(msg["content"], list)
                              for block in msg["content"] if "cache_control" in block]
            system_marks = [block["cache_control"] for block in one_shot["system"] if "cache_control" in block]
            assert not one_shot_marks, f"one-shot message cache mark on {len(one_shot_marks)} block(s)"
            assert system_marks, "native system cache marks lost on a one-shot"
            assert all("<total_tokens>" not in json.dumps(wire) for wire in peer.wires), "native budget reminder present"
            assert sum(len(msg["content"]) for msg in peer.wires[0]["messages"]) > 20
            return {"native_version": version, "native_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "requests": len(peer.wires), "synthetic_usage_not_cache_measurement": True,
                    "cache_controls": markers, "prefix_receipts": receipts,
                    "one_shot": {"message_marks": len(one_shot_marks), "system_marks": system_marks}}
        finally:
            client.close()
            peer.shutdown()
            worker.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--model", default="claude-sonnet-5")
    args = parser.parse_args()
    print(json.dumps(run(args.binary.resolve(), args.model), indent=2))
