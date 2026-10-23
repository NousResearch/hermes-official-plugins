import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE = r"""
import json, os, pathlib, sys, time
if os.environ.get('PID_FILE'):
 open(os.environ['PID_FILE'],'w').write(str(os.getpid()))
if '--version' in sys.argv:
 print('2.1.263 (Claude Code)'); sys.exit()
rows=[]
for line in sys.stdin:
 r=json.loads(line); rows.append(r)
 if r.get('shouldQuery') is False:
  print(json.dumps({'type':'result','num_turns':0,'is_error':False}),flush=True)
if os.environ.get('NATIVE_ERROR'):
 # An error native answers itself (no login: authentication_failed), with no request to ANTHROPIC_BASE_URL.
 code,text=os.environ['NATIVE_ERROR'].split(':',1)
 print(json.dumps({'type':'assistant','error':code,'is_api_error_message':True,'message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':text}],'stop_reason':'stop_sequence'}}),flush=True)
 print(json.dumps({'type':'result','subtype':'success','is_error':True,'num_turns':1,'result':text}),flush=True)
 sys.exit(1)
if os.environ.get('HANG'):
 if os.environ.get('PID_FILE'):
  # Real native is a shim -> node tree; cancellation must take the grandchild down with it.
  import subprocess
  child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  open(os.environ['PID_FILE'] + '.child','w').write(str(child.pid))
 print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'started'}}}),flush=True)
 time.sleep(60)
settings=json.loads(pathlib.Path(sys.argv[sys.argv.index('--settings')+1]).read_text())
wire=json.loads(settings['env']['CLAUDE_CODE_EXTRA_BODY'])
assert wire['tools'][0]['description'].endswith('TAIL')
assert '--max-turns' in sys.argv and sys.argv[sys.argv.index('--max-turns')+1]=='1'
assert sys.argv[sys.argv.index('--permission-mode')+1]=='dontAsk'
assert sys.argv[sys.argv.index('--tools')+1]==''
assert rows[-1]['type']=='user'
assert 'metadata' not in wire
blocks=[{'type':'thinking','thinking':'private','signature':'signed-test'}, {'type':'text','text':'hello\n'}, {'type':'tool_use','id':'toolu_test','name':os.environ.get('TOOL_NAME','mcp__hermes__probe'),'input':{'value':'x'}}]
if len(rows)>1:
 if rows[1]['message']['content'][0]['type']=='thinking':
  assert rows[1]['message']['content']==[dict(b, name=os.environ.get('REPLAY_NAME', b['name'])) if b['type']=='tool_use' else b for b in blocks]
 else:
  assert rows[1]['message']['content'][0]['text']=='middleware changed'
 blocks=[{'type':'text','text':'done'}]
for b in blocks:
 if b['type']=='thinking':
  print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':b['thinking']}}}),flush=True)
 if b['type']=='text':
  print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':b['text']}}}),flush=True)
print(json.dumps({'type':'assistant','message':{'role':'assistant','content':blocks,'id':'msg_test','model':'sonnet','stop_reason':'tool_use' if len(blocks)>1 else 'end_turn'}}),flush=True)
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}),flush=True)
u={'input_tokens':3,'output_tokens':5,'cache_read_input_tokens':7,'cache_creation_input_tokens':11,'output_tokens_details':{'thinking_tokens':4}}
print(json.dumps({'type':'result','num_turns':2 if len(blocks)>1 else 1,'subtype':'error_max_turns' if len(blocks)>1 else 'success','is_error':len(blocks)>1,'usage':u,'total_cost_usd':.012345,'modelUsage':{'sonnet':{'costBasis':'list'}}}),flush=True)
sys.exit(1 if len(blocks)>1 else 0)
"""


def _wait_gone(*pids, timeout=15):
    # Reaping is event-driven; the bound only guards a hang and must tolerate a loaded runner.
    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE for pid in pids):
            return True
        time.sleep(0.02)
    return False


class Contract(unittest.TestCase):
    def client(self, tmp, **kw):
        import directsdk

        script = Path(tmp) / "native.py"
        script.write_text(FAKE)
        return directsdk.Client(
            command=[sys.executable, str(script)],
            env={"PATH": os.environ["PATH"], "HOME": tmp, **kw},
        )

    def request(self) -> dict:
        return dict(
            model="sonnet",
            messages=[{"role": "user", "content": "go"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "description": "long " * 600 + "TAIL",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    },
                }
            ],
        )

    def test_canonical_request_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client(tmp)
            self.assertEqual(client.api_key, "external-process")
            self.assertEqual(client.base_url, "process://claude-subscription-directsdk-experimental")
            for streaming in (False, True):
                req = self.request()
                req["timeout"] = SimpleNamespace(read=10)
                result = client.chat.completions.create(**req, stream=streaming)
                if streaming:
                    chunks = list(result)
                    self.assertEqual(''.join(getattr(c.choices[0].delta, 'reasoning_content', None) or '' for c in chunks), 'private')
                    self.assertEqual(
                        "".join(
                            c.choices[0].delta.content or ""
                            for c in chunks
                            if c.choices
                        ),
                        "hello\n",
                    )
                    final = chunks[-1]
                    msg = {
                        "role": "assistant",
                        "content": "hello",
                        "tool_calls": [
                            {
                                "id": "toolu_test",
                                "type": "function",
                                "function": {
                                    "name": "probe",
                                    "arguments": '{"value":"x"}',
                                },
                            }
                        ],
                        "reasoning_details": final.choices[0].delta.reasoning_details,
                    }
                    self.assertEqual(final.choices[0].finish_reason, "tool_calls")
                else:
                    final = result
                    msg = result.choices[0].message.model_dump()
                    self.assertEqual(msg['reasoning_content'], 'private')
                    self.assertEqual(msg["tool_calls"][0]["function"]["name"], "probe")
                self.assertEqual(final.usage.prompt_tokens, 21)
                self.assertEqual(final.usage.completion_tokens, 5)
                from agent.usage_pricing import normalize_usage
                canonical = normalize_usage(final.usage, api_mode='chat_completions')
                self.assertEqual(canonical.reasoning_tokens, 4)
                self.assertEqual(final.usage.model_dump()['native_cost'], {'total_cost_usd': .012345, 'modelUsage': {'sonnet': {'costBasis': 'list'}}})
                msg["content"] = (msg.get("content") or "").strip()
                req["messages"] += [
                    msg,
                    {
                        "role": "tool",
                        "tool_call_id": "toolu_test",
                        "content": " host\n result",
                    },
                ]
                self.assertEqual(
                    client.chat.completions.create(**req).choices[0].message.content,
                    "done",
                )
                msg["content"] = "middleware changed"
                self.assertEqual(client.chat.completions.create(**req).choices[0].message.content, "done")
            client.close()

    def test_tool_outside_the_inventory_reaches_the_host(self):
        """Hermes Tool Search defers MCP tools behind tool_search/tool_describe/tool_call, so they
        are not in this request's inventory. Claude sometimes calls one directly by the name it read
        in tool_describe output (#39). Hermes owns tool validation: its unknown-tool path answers
        with a recoverable error listing the callable tools, as it does for every other provider.
        Failing the whole request here left the model no way to correct itself."""
        with tempfile.TemporaryDirectory() as tmp:
            for native_name, host_name in (("mcp__fastmail__draft_email", "mcp__fastmail__draft_email"),
                                           ("mcp__hermes__unlisted", "unlisted")):
                client = self.client(tmp, TOOL_NAME=native_name)
                req = self.request()
                result = client.chat.completions.create(**req)
                self.assertEqual(result.choices[0].finish_reason, "tool_calls")
                msg = result.choices[0].message.model_dump()
                self.assertEqual(msg["tool_calls"][0]["function"]["name"], host_name)
                # The host's error result goes back with the call replayed as native produced it.
                msg["content"] = (msg.get("content") or "").strip()
                req["messages"] += [msg, {"role": "tool", "tool_call_id": "toolu_test",
                                          "content": f"Tool '{host_name}' does not exist. Available tools: probe"}]
                self.assertEqual(client.chat.completions.create(**req).choices[0].message.content, "done")
                client.close()

    def test_bare_host_name_maps_to_the_tool_and_replays_with_the_offered_name(self):
        # Native sometimes drops the MCP prefix on a tool it was offered (#62: `search_files`,
        # `terminal`). The host gets the tool, and the carried native block is rewritten so the next
        # request replays the name native was offered, not the slip it would keep imitating.
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client(tmp, TOOL_NAME="probe", REPLAY_NAME="mcp__hermes__probe")
            for streaming in (False, True):
                req = self.request()
                result = client.chat.completions.create(**req, stream=streaming)
                if streaming:
                    chunks = list(result)
                    calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
                    details = [d for c in chunks if c.choices for d in (getattr(c.choices[0].delta, "reasoning_details", None) or [])]
                else:
                    calls, details = result.choices[0].message.tool_calls, result.choices[0].message.reasoning_details
                self.assertEqual([c.function.name for c in calls], ["probe"])
                carried = [b["name"] for m in details[0]["messages"] for b in m["content"] if b["type"] == "tool_use"]
                self.assertEqual(carried, ["mcp__hermes__probe"])
            msg = client.chat.completions.create(**req).choices[0].message.model_dump()
            msg["content"] = (msg.get("content") or "").strip()
            req["messages"] += [msg, {"role": "tool", "tool_call_id": "toolu_test", "content": "ok"}]
            # The fake native asserts the replayed tool_use carries REPLAY_NAME.
            self.assertEqual(client.chat.completions.create(**req).choices[0].message.content, "done")
            client.close()

    def test_logged_out_native_raises_the_login_hint(self):
        import directsdk
        from directsdk_setup import LOGGED_OUT_HINT

        with tempfile.TemporaryDirectory() as tmp:
            client = self.client(tmp, NATIVE_ERROR="authentication_failed:Not logged in \u00b7 Please run /login")
            for streaming in (False, True):
                with self.assertRaises(directsdk.ClaudeCodeLoggedOut) as raised:
                    result = client.chat.completions.create(**self.request(), stream=streaming)
                    if streaming:
                        list(result)
                self.assertIn(LOGGED_OUT_HINT, str(raised.exception))
                self.assertIn("Not logged in", str(raised.exception))
                self.assertEqual(raised.exception.status_code, 401)
            client.close()
            # Any other error native answers itself keeps its own text.
            other = self.client(tmp, NATIVE_ERROR="unknown:API Error: something else")
            with self.assertRaisesRegex(RuntimeError, "^Native API error: API Error: something else$") as raised:
                other.chat.completions.create(**self.request())
            self.assertNotIsInstance(raised.exception, directsdk.ClaudeCodeLoggedOut)
            self.assertIsNone(raised.exception.status_code)
            other.close()

    def test_fail_closed_and_cancellation(self):
        import directsdk

        # `sonnet` resolves to Sonnet 5.5, which rejects the disable; Sonnet 5 accepts it.
        disabled = json.loads(
            directsdk.request_body(
                {**self.request(), "model": "claude-sonnet-5", "extra_body": {"reasoning": {"enabled": False}}}
            )[0]
        )
        self.assertEqual(disabled["thinking"], {"type": "disabled"})
        self.assertEqual(disabled["context_management"], {"edits": []})
        # Fable and Opus 5.5 reject the disable (HTTP 400 "thinking.type.disabled is not
        # supported"), so a caller's disable is omitted rather than sent: thinking stays on, the
        # request survives. Every spelling of the route must hit the gate (#22).
        for route in ("fable", "opus", "claude-opus-5-5", "claude-opus-5-5[1m]"):
            mandatory = json.loads(
                directsdk.request_body(
                    {**self.request(), "model": route, "extra_body": {"reasoning": {"enabled": False}}}
                )[0]
            )
            self.assertNotIn("thinking", mandatory, route)
            self.assertNotIn("context_management", mandatory, route)
        # Opus 5 still accepts the disable; the Opus 5.5 gate must not swallow it by prefix.
        opus5 = json.loads(
            directsdk.request_body(
                {**self.request(), "model": "claude-opus-5", "extra_body": {"reasoning": {"enabled": False}}}
            )[0]
        )
        self.assertEqual(opus5["thinking"], {"type": "disabled"})
        effort = json.loads(
            directsdk.request_body(
                {**self.request(), "extra_body": {"reasoning": {"effort": "low"}}}
            )[0]
        )
        self.assertEqual(effort["output_config"], {"effort": "low"})
        enabled = {"reasoning": {"enabled": True, "effort": "medium"}}
        adaptive = json.loads(
            directsdk.request_body({**self.request(), "extra_body": enabled})[0]
        )
        self.assertEqual(adaptive["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(adaptive["output_config"], {"effort": "medium"})
        for route in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
            # Haiku 4.5 answers `adaptive thinking is not supported on this model` with a 400.
            haiku = json.loads(
                directsdk.request_body(
                    {**self.request(), "model": route, "extra_body": enabled}
                )[0]
            )
            self.assertNotIn("thinking", haiku)
            self.assertEqual(haiku["output_config"], {"effort": "medium"})
        off = json.loads(
            directsdk.request_body(
                {
                    **self.request(),
                    "model": "claude-haiku-4-5",
                    "extra_body": {"reasoning": {"enabled": False}},
                }
            )[0]
        )
        self.assertEqual(off["thinking"], {"type": "disabled"})
        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        }
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": "title", "strict": True, "schema": schema},
        }
        formatted = json.loads(
            directsdk.request_body(
                {**self.request(), "extra_body": {"response_format": fmt}}
            )[0]
        )
        self.assertEqual(
            formatted["output_config"]["format"],
            {"type": "json_schema", "schema": schema},
        )
        routed = directsdk.Client(
            env={"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/native/test"}
        )
        self.assertEqual(routed.command, ["/native/test"])
        from unittest.mock import patch

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-conflicting-key"}):
            with self.assertRaisesRegex(ValueError, "OAuth"):
                directsdk.Client(command="/does/not/exist").create(**self.request())
        client = directsdk.Client(command="/does/not/exist", env={})
        with self.assertRaises((FileNotFoundError, RuntimeError)):
            client.chat.completions.create(**self.request())
        with tempfile.TemporaryDirectory() as tmp:
            client = self.client(tmp)
            for bad in (
                {"extra_body": {"metadata": {}}},
                {"extra_body": []},
                {"temperature": float("nan")},
                {"tool_choice": "required"},
                {"n": 2},
            ):
                with self.assertRaises(ValueError):
                    client.chat.completions.create(**self.request(), **bad)

            async def run():
                result = await client.chat.completions.create(**self.request())
                self.assertEqual(result.choices[0].finish_reason, "tool_calls")

            asyncio.run(run())

            # A *sync* caller that happens to run on an event-loop thread (Hermes' NeMo Relay
            # managed execution drives the agent's sync provider callback inside asyncio.run)
            # must get a completed response, not an un-awaited coroutine.
            async def sync_caller_in_loop():
                def sync_provider_callback():
                    return client.chat.completions.create(**self.request())
                return sync_provider_callback()

            result = asyncio.run(sync_caller_in_loop())
            self.assertFalse(asyncio.iscoroutine(result), "sync caller inside a running loop got a coroutine")
            self.assertEqual(result.choices[0].finish_reason, "tool_calls")
            cancel_pidfile = Path(tmp) / "cancel-pid"
            hanging = self.client(tmp, HANG="1", PID_FILE=str(cancel_pidfile))
            stream = hanging.chat.completions.create(**self.request(), stream=True)
            self.assertEqual(next(stream).choices[0].delta.content, "started")
            native_pid, grandchild_pid = int(cancel_pidfile.read_text()), int((Path(str(cancel_pidfile) + ".child")).read_text())
            hanging.cancel()
            with self.assertRaisesRegex(RuntimeError, "cancel"):
                list(stream)
            # cancel() must take the whole tree down, on every OS (Windows: shim -> node grandchild).
            self.assertTrue(_wait_gone(native_pid, grandchild_pid), "cancel() left native or its grandchild running")
            hanging.close()
            # Closing a paused stream must reap without asking for another chunk.
            pidfile = Path(tmp) / "pid"
            hanging = self.client(tmp, HANG="1", PID_FILE=str(pidfile))
            paused = hanging.chat.completions.create(**self.request(), stream=True)
            next(paused)
            pid = int(pidfile.read_text())
            process = paused.request.process
            paused.close()
            self.assertTrue(process.stdout.closed)
            self.assertEqual(len(hanging._requests), 0)
            self.assertTrue(_wait_gone(pid), "Closed paused stream left its native child unreaped")
            hanging.close()
            unstarted = self.client(tmp)
            stream = unstarted.create(**self.request(), stream=True)
            unstarted.close()
            self.assertEqual(len(unstarted._requests), 0)


    def test_tool_schemas_are_normalized_for_the_native_validator(self):
        """Anthropic hard-400s on top-level oneOf/allOf/anyOf and on the null branch of nullable
        unions. The host normalizes both, but only on the api_mode='messages' path, so a single
        Hermes tool carrying a conditional-required hint would fail every request on this
        transport. Nested unions that are not nullable stay as the tool declared them."""
        import directsdk

        req = self.request()
        req["tools"][0]["function"]["parameters"] = {
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "proposal": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "ref": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "required": ["mode"],
            "allOf": [{"if": {"properties": {"mode": {"const": "proposal"}}},
                       "then": {"required": ["proposal"]}}],
        }
        encoded, manifest, _ = directsdk.request_body(req)
        wire = json.loads(encoded)["tools"][0]["input_schema"]
        # The inert MCP manifest and the request body must advertise the same shape.
        for schema in (wire, manifest[0]["inputSchema"]):
            self.assertNotIn("allOf", schema)
            self.assertEqual(schema["required"], ["mode"])
            self.assertEqual(schema["properties"]["proposal"], {"type": "string"})
            self.assertEqual(schema["properties"]["ref"],
                             {"oneOf": [{"type": "string"}, {"type": "integer"}]})
        # A combinator-only schema still reaches the validator as a usable object.
        req["tools"][0]["function"]["parameters"] = {"anyOf": [{"type": "object"}]}
        bare = json.loads(directsdk.request_body(req)[0])["tools"][0]["input_schema"]
        self.assertEqual(bare, {"type": "object", "properties": {}})


if __name__ == "__main__":
    unittest.main()
