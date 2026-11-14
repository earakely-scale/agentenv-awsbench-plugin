"""tests/e2e/fake_model.py, recording every request it answers: the scripted model that answers turn 1 without DONE and
turn 2 with DONE, so the hook's wake-up digest, the resumed session and the second turn run with no model spend.

    python harbor/qc/multiturn/recorder.py --host 172.17.0.1 --port 18997 --out hb-requests.jsonl

Each line: the route, the request's headers (authorization and x-api-key dropped) and its JSON body.
"""
import argparse
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("fake_model", ROOT / "tests/e2e/fake_model.py")
fake_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_model)
LOCK = threading.Lock()
SECRET_HEADERS = {"authorization", "x-api-key", "proxy-authorization"}


def recording(out):
    class Recorder(fake_model.Handler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            entry = {"route": self.path.split("?", 1)[0],
                     "headers": {k.lower(): v for k, v in self.headers.items() if k.lower() not in SECRET_HEADERS},
                     "body": json.loads(raw or b"{}")}
            with LOCK, open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.rfile = _Replay(raw)
            super().do_POST()
    return Recorder


class _Replay:
    def __init__(self, raw):
        self.raw = raw

    def read(self, n=-1):
        data, self.raw = self.raw, b""
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18997)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), recording(args.out)).serve_forever()


if __name__ == "__main__":
    main()
