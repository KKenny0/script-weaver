"""Manual browser-test H3 endpoint; never used by production code."""

import json
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def box(kind, body=b""):
    return struct.pack(">I4s", len(body) + 8, kind) + body


VIDEO = box(b"ftyp", b"isom") + box(b"mdat", b"browser") + box(b"moov")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        seconds = payload.get("seconds")
        valid = set(payload) == {
            "task", "model", "conditions", "target", "seconds", "prompt",
            "num_outputs_per_prompt", "num_inference_steps", "flow_shift",
            "audio_flow_shift", "seed",
        }
        valid = valid and payload.get("task") == "fl2va"
        valid = valid and payload.get("model") == "MiniMaxAI/MiniMax-H3"
        valid = valid and isinstance(seconds, int) and 4 <= seconds <= 15
        valid = valid and payload.get("target") == {
            "short_edge": 768, "aspect_ratio": "auto", "duration_seconds": seconds,
        }
        valid = valid and {
            "num_outputs_per_prompt": payload.get("num_outputs_per_prompt"),
            "num_inference_steps": payload.get("num_inference_steps"),
            "flow_shift": payload.get("flow_shift"),
            "audio_flow_shift": payload.get("audio_flow_shift"),
            "seed": payload.get("seed"),
        } == {
            "num_outputs_per_prompt": 1, "num_inference_steps": 50,
            "flow_shift": 12.0, "audio_flow_shift": 3.0,
            "seed": payload.get("seed"),
        }
        valid = valid and isinstance(payload.get("seed"), int)
        conditions = payload.get("conditions")
        valid = valid and isinstance(conditions, list) and 1 <= len(conditions) <= 2
        if valid:
            for index, condition in enumerate(conditions):
                valid = valid and set(condition) == {"type", "uri", "role", "frame_index"}
                valid = valid and condition.get("type") == "image"
                valid = valid and condition.get("role") == "keyframe"
                valid = valid and str(condition.get("uri", "")).startswith("file://")
                valid = valid and condition.get("frame_index") == (0 if index == 0 else -1)
        if not valid:
            self.send_response(422)
            self.end_headers()
            return
        self.send_json({"id": "browser-h3", "status": "queued"})

    def do_GET(self):
        if self.path == "/v1/models":
            return self.send_json({"data": [{"id": "MiniMaxAI/MiniMax-H3"}]})
        if self.path == "/v1/videos/browser-h3":
            return self.send_json({"id": "browser-h3", "status": "completed"})
        if self.path == "/v1/videos/browser-h3/content":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO)))
            self.end_headers()
            return self.wfile.write(VIDEO)
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 39091), Handler)
    print("http://127.0.0.1:39091", flush=True)
    server.serve_forever()
