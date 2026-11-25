#!/usr/bin/env python3
"""HPA controller test, Reconcile step (an Exec step of config.yaml).

Waits until every test HPA has status.desiredReplicas equal to <desired replicas>: the HPA controller has read
the metric, computed the new replica count and written it. This times the HPA controller alone; the pods that follow
are created by the Deployment and ReplicaSet controllers and are waited for by a later step. One list, then one
watch from the list's resourceVersion (kubectl get --raw), not repeated lists. Done once <hpas> HPAs have the count.
A watch that ends early (an error, a closed watch) is started again with a new list.

Times from <start file> (the half-wave's start, written by hpa-metrics.sh set), or from its own start without one.
Exits 1 if that takes more than <limit> seconds, but only once done or after <timeout> seconds, so the next half-wave
starts reconciled; exits 0 when done within <limit>. The list has a 60 s request timeout; the watch ends at <timeout>.
Usage: hpa-wait.py <hpas> <desired replicas> <limit seconds> <timeout seconds> [start file]   env: KUBECONFIG
"""
import json
import subprocess
import sys
import threading
import time
import urllib.parse

hpas, want = int(sys.argv[1]), int(sys.argv[2])
limit, timeout = float(sys.argv[3]), float(sys.argv[4])
start = time.time()
if len(sys.argv) > 5:
    try:
        start = float(open(sys.argv[5]).read())
    except (OSError, ValueError):
        pass
URL = "/apis/autoscaling/v2/horizontalpodautoscalers?labelSelector=" + urllib.parse.quote("group=hpa-test", safe="")
desired = {}  # HPA namespace/name -> status.desiredReplicas


def key(o):
    m = o.get("metadata") or {}
    return f"{m.get('namespace')}/{m.get('name')}"


def entry(o):
    return (o.get("status") or {}).get("desiredReplicas") or 0


def done():
    return len(desired) == hpas and all(n == want for n in desired.values())


def finish(reached):
    e = time.time() - start
    good = sum(1 for n in desired.values() if n == want)
    state = f"{good} of {hpas} HPAs with desiredReplicas {want} after {e:.1f}s"
    if reached and e <= limit:
        print(f"done: {state} (limit {limit:g}s)", flush=True)
        sys.exit(0)
    print(f"{'slow' if reached else 'timeout'}: {state}, limit {limit:g}s", flush=True)
    sys.exit(1)


while True:
    left = timeout - (time.time() - start)
    if left <= 0:
        finish(False)
    try:
        out = subprocess.run(["kubectl", "get", "--raw", URL, "--request-timeout=60s"], capture_output=True,
                             text=True, check=True, timeout=max(left, 1)).stdout
        lst = json.loads(out)
    except (subprocess.SubprocessError, ValueError) as err:
        print(f"list failed: {str(err)[:200]}", flush=True)
        time.sleep(1)
        continue
    desired = {key(o): entry(o) for o in lst.get("items") or []}
    if done():
        finish(True)
    rv = (lst.get("metadata") or {}).get("resourceVersion", "")
    left = timeout - (time.time() - start)
    # busybox timeout ends the watch at the deadline even if this script is killed first
    proc = subprocess.Popen(["timeout", str(int(max(left, 1)) + 1), "kubectl", "get", "--raw",
                             f"{URL}&watch=1&resourceVersion={rv}", "--request-timeout=0"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timer = threading.Timer(max(left, 0.1), proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                print(f"watch: {line.strip()[:200]}", flush=True)
                continue
            kind, o = ev.get("type"), ev.get("object") or {}
            if kind in ("ADDED", "MODIFIED"):
                desired[key(o)] = entry(o)
            elif kind == "DELETED":
                desired.pop(key(o), None)
            elif kind == "ERROR":
                print(f"watch error: {json.dumps(o)[:200]}", flush=True)
                break
            else:
                continue
            if done():
                finish(True)
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
    print(f"watch ended after {time.time() - start:.1f}s; listing again", flush=True)
    time.sleep(1)
