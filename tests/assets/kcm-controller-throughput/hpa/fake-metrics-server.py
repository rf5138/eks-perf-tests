#!/usr/bin/env python3
"""A fake external metrics API for the HPA controller test (hpa-metrics.sh runs it as a Deployment).

KWOK pods have no metrics, so an HPA could never compute a replica count. This server is registered as the
external.metrics.k8s.io APIService and returns one number, VALUE, for any metric of any namespace. With an HPA
metric of type Value and target 100 the HPA wants ceil(current replicas x VALUE / 100) replicas, so one write of
VALUE moves every HPA at once: 300 scales 1 replica up to 3, 30 scales 3 back down to 1, 100 changes nothing.

  GET  https://:8443/apis/external.metrics.k8s.io/v1beta1[/namespaces/<ns>/<metric>]   the API server calls this
  GET  http://127.0.0.1:8080/set?value=<n>   set VALUE (loopback only; hpa-metrics.sh calls it with kubectl exec)
  GET  http://127.0.0.1:8080/stats           requests answered since the last call of /stats
"""
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

state = {"value": 100, "metric_requests": 0}
lock = threading.Lock()


class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so the API server reuses connections

    def log_message(self, *args):
        pass

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Metrics(Base):
    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:3] != ["apis", "external.metrics.k8s.io", "v1beta1"]:
            return self.send_json(200, {"ok": True})  # /healthz
        if len(parts) == 3:  # discovery: no listable resources
            return self.send_json(200, {"kind": "APIResourceList", "apiVersion": "v1",
                                        "groupVersion": "external.metrics.k8s.io/v1beta1", "resources": []})
        if len(parts) < 6 or parts[3] != "namespaces":
            return self.send_json(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "code": 404})
        with lock:
            state["metric_requests"] += 1
            value = state["value"]
        self.send_json(200, {
            "kind": "ExternalMetricValueList", "apiVersion": "external.metrics.k8s.io/v1beta1", "metadata": {},
            "items": [{"metricName": parts[5], "metricLabels": {},
                       "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "value": str(value)}]})


class Admin(Base):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/set":
            with lock:
                state["value"] = int(parse_qs(url.query)["value"][0])
                value = state["value"]
            return self.send_json(200, {"value": value})
        if url.path == "/stats":
            with lock:
                n, state["metric_requests"] = state["metric_requests"], 0
            return self.send_json(200, {"value": state["value"], "metric_requests": n})
        self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    admin = ThreadingHTTPServer(("127.0.0.1", 8080), Admin)
    admin.daemon_threads = True
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Metrics)
    server.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/certs/tls.crt", "/certs/tls.key")
    # Handshake in the handler thread, so one slow client cannot block new connections.
    server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    print("fake external metrics API on :8443, admin on 127.0.0.1:8080", flush=True)
    server.serve_forever()
