"""A stand-in Anthropic Messages endpoint that scripts the agent's replies, so a task's harness path (the post-turn hook,
the horizon, the grade) runs end to end with a real agent and no model spend.

The first turn of a conversation answers without DONE, so the hook has to let time pass and wake the agent; once the
conversation holds a reply, or the hook's wake-up ("[03:05 ET] ..."), it answers DONE. It never calls a tool.

    python tests/e2e/fake_model.py --host 172.17.0.1 --port 18999 &   # the docker bridge, so local containers reach it
    LITELLM_BASE_URL=http://172.17.0.1:18999 LITELLM_API_KEY=fake \\
        agent-env run src/agentenv_awsbench/bundles/awsbench --task t01-draft
"""
import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIRST = "Paged in. Looking at the incident; I'll check back when something arrives."
WAKE = re.compile(r"\[\d{1,2}:\d{2} ET\]")


def reply_for(body):
    messages = body.get("messages") or []
    texts = json.dumps([m.get("content") for m in messages if m.get("role") == "user"])
    later = any(m.get("role") == "assistant" for m in messages) or WAKE.search(texts)
    return "Handoff is on OPS-4452.\nDONE" if later else FIRST


def events(text, model):
    usage = {"input_tokens": 10, "output_tokens": 0}
    yield "message_start", {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": usage}}
    yield "content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    yield "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    yield "content_block_stop", {"type": "content_block_stop", "index": 0}
    yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": len(text.split())}}
    yield "message_stop", {"type": "message_stop"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"data": [], "object": "list"})

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        route = self.path.split("?", 1)[0]
        if route.endswith("/count_tokens"):
            return self._json({"input_tokens": 100})
        if not route.endswith("/messages"):
            return self._json({"error": {"type": "not_found", "message": f"fake model serves /v1/messages, not {route}"}},
                              404)
        text, model = reply_for(body), body.get("model", "fake")
        print(f"{route} model={model} messages={len(body.get('messages') or [])} -> {text.splitlines()[-1]!r}",
              file=sys.stderr, flush=True)
        if not body.get("stream"):
            return self._json({"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                               "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                               "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for name, data in events(text, model):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18999)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
