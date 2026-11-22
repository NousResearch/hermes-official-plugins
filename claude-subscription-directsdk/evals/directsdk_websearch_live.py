"""LIVE eval (one real, small haiku search on your Claude subscription): a `web_search` call answered through
the subscription while the main directsdk turn keeps its single-request admission (#389, Shape A).

Not part of evals/ci.sh: it needs a logged-in Claude Code and spends one haiku search.

  1. Main turn 1: real native executable -> loopback synthetic peer, which answers with a `web_search` tool call.
     The relay admits exactly one upstream request.
  2. The companion provider (claude-subscription-directsdk-websearch) serves that `web_search` through its own
     `claude -p --tools WebSearch` call on the real login. It never goes through the relay.
  3. Main turn 2 replays the tool result; it is again exactly one upstream request.

Asserts: the peer saw one request per main turn and none from the search; the search returned real
title/url rows; the search call's argv has exactly one native tool and no MCP config.

Run: python evals/directsdk_websearch_live.py /path/to/claude ["query"]
"""
import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import directsdk

# The companion plugin is a sibling directory; the eval loads it by path, as the proof spans both.
_spec = importlib.util.spec_from_file_location('native_search', ROOT.parent / 'claude-subscription-directsdk-websearch' / 'native_search.py')
native_search = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(native_search)

USAGE = {'input_tokens': 11, 'output_tokens': 7, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
TOOLS = [{'type': 'function', 'function': {'name': 'web_search', 'description': 'Search the web',
                                           'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}}]


def events(query, final):
    blocks = [{'type': 'text', 'text': 'Searching.'}]
    if not final:
        blocks.append({'type': 'tool_use', 'id': 'host_tool_1', 'name': 'mcp__hermes__web_search', 'input': {'query': query}})
    yield {'type': 'message_start', 'message': {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-4-6', 'content': [], 'usage': USAGE}}
    for i, block in enumerate(blocks):
        start = {**block, **({'text': ''} if block['type'] == 'text' else {'input': {}})}
        yield {'type': 'content_block_start', 'index': i, 'content_block': start}
        delta = ({'type': 'text_delta', 'text': block['text']} if block['type'] == 'text'
                 else {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])})
        yield {'type': 'content_block_delta', 'index': i, 'delta': delta}
        yield {'type': 'content_block_stop', 'index': i}
    yield {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn' if final else 'tool_use', 'stop_sequence': None}, 'usage': USAGE}
    yield {'type': 'message_stop'}


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.server.requests += 1
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        final = self.server.requests > 1
        self.wfile.write(''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events(self.server.query, final)).encode())
        self.wfile.flush()


def run(binary, query):
    binary = str(Path(binary).absolute())
    report = {'query': query}
    with tempfile.TemporaryDirectory(prefix='directsdk-websearch-eval-') as home:
        peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        peer.requests, peer.bodies, peer.query = 0, [], query
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        # Main turns: a throwaway HOME and a fixture key against the loopback peer, as the admission eval does.
        env = {'PATH': os.defpath, 'HOME': home, 'HERMES_HOME': home, 'XDG_CONFIG_HOME': home, 'CLAUDE_CONFIG_DIR': home,
               'ANTHROPIC_API_KEY': 'fixture-not-a-real-key', 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}',
               'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1'}
        client = directsdk.Client(command=binary, env=env, timeout=30)
        messages = [{'role': 'user', 'content': 'Find the Hermes web search provider plugin guide.'}]
        try:
            first = client.create(model='claude-sonnet-4-6', messages=messages, tools=TOOLS, max_tokens=2400)
            call = first.choices[0].message.tool_calls[0]
            assert call.function.name == 'web_search', call
            assert peer.requests == 1, peer.requests
            report['main_turn_1'] = {'admission': first.usage.model_dump()['native_admission'], 'upstream_requests': peer.requests}

            # The search itself: the companion provider on the REAL login (no relay, no fixture key, no base URL).
            native_search._setting = lambda key, default=None: {native_search.ENABLE_SETTING: True}.get(key, default)
            log = []
            handler = logging.Handler()
            handler.emit = lambda record: log.append(record.getMessage())
            native_search.logger.addHandler(handler)
            native_search.logger.setLevel(logging.INFO)
            real_env = {k: v for k, v in os.environ.items() if k not in native_search.BACKEND_OVERRIDES}
            os.environ.clear()
            os.environ.update({**real_env, 'CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND': binary})
            argv_seen = []
            real_run = native_search.run_search
            native_search.run_search = lambda argv, *a, **k: (argv_seen.append(list(argv)), real_run(argv, *a, **k))[1]
            started = time.monotonic()
            result = native_search.ClaudeSubscriptionWebSearchProvider().search(json.loads(call.function.arguments)['query'], limit=5)
            report['search_seconds'] = round(time.monotonic() - started, 2)
            assert result['success'] is True, result
            rows = result['data']['web']
            assert rows and all(r['url'].startswith('http') and r['title'] for r in rows), rows
            argv = argv_seen[0]
            assert argv[argv.index('--tools') + 1] == 'WebSearch' and '--strict-mcp-config' in argv and '--max-turns' not in argv
            assert peer.requests == 1, 'the search must not reach the main-turn relay/peer'
            report.update(result_count=len(rows), sample=rows[:2], log=log, search_argv=argv[1:])

            # Main turn 2 replays the answer: again exactly one upstream request.
            message = first.choices[0].message.model_dump()
            second = client.create(model='claude-sonnet-4-6', messages=messages + [
                {**message, 'content': (message['content'] or '').strip()},
                {'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps(result)}], tools=TOOLS, max_tokens=2400)
            assert peer.requests == 2, peer.requests
            report['main_turn_2'] = {'admission': second.usage.model_dump()['native_admission'], 'upstream_requests': peer.requests - 1}
            report['peer_requests_total'] = peer.requests
            sent = json.dumps(peer.bodies[1])
            assert rows[0]['url'] in sent, 'the search rows reached the second main request'
        finally:
            client.close()
            peer.shutdown()
            thread.join()
            peer.server_close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary')
    parser.add_argument('query', nargs='?', default='hermes agent web search provider plugin')
    args = parser.parse_args()
    print(json.dumps(run(args.binary, args.query), indent=2, ensure_ascii=False))
