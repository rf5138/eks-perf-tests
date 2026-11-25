#!/bin/bash
# HPA controller test: runs the fake external metrics API (fake-metrics-server.py) and sets its value (run by the
# Exec steps of config.yaml). The server is registered as the external.metrics.k8s.io APIService, so every HPA
# reads the same number as its metric.
#   install                      namespace, certificate, 2 server pods, Service and APIService; value 100 (no HPA changes)
#   set <value> <start file>     writes the start time to <start file>, then sets the value on every server pod
#   stats                        prints each pod's value and the metric requests it answered since the last call
#   remove                       deletes the APIService and the namespace
# Every kubectl call has a 30 s request timeout on its API requests.
# Usage: hpa-metrics.sh <install|set|stats|remove> [value] [start file]   env: KUBECONFIG
set -euo pipefail
OP=${1:-}
NS=kcm-fake-metrics
DIR=$(cd "$(dirname "$0")" && pwd)
KC="kubectl --request-timeout=30s"
case $OP in
  install|set|stats|remove) ;;
  *) echo "usage: hpa-metrics.sh <install|set|stats|remove> [value] [start file]" >&2; exit 2 ;;
esac

pods() { $KC -n "$NS" get pods -l app=fake-metrics -o name; }
# Calls the server's loopback admin port in one pod.
admin() { $KC -n "$NS" exec "$1" -- python3 -c "import urllib.request,sys; print(urllib.request.urlopen('http://127.0.0.1:8080/$2').read().decode())"; }

if [ "$OP" = install ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  host=fake-metrics.$NS.svc
  openssl req -x509 -newkey rsa:2048 -nodes -days 3 -subj "/CN=fake-metrics-ca" -keyout "$tmp/ca.key" -out "$tmp/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$host" -keyout "$tmp/tls.key" -out "$tmp/tls.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s,DNS:fake-metrics.%s,DNS:fake-metrics\n' "$host" "$NS" > "$tmp/san.ext"
  openssl x509 -req -in "$tmp/tls.csr" -CA "$tmp/ca.crt" -CAkey "$tmp/ca.key" -CAcreateserial -days 3 -extfile "$tmp/san.ext" -out "$tmp/tls.crt" 2>/dev/null
  $KC create namespace "$NS" --dry-run=client -o yaml | $KC apply -f -
  $KC -n "$NS" create secret generic fake-metrics-certs --from-file=tls.crt="$tmp/tls.crt" --from-file=tls.key="$tmp/tls.key" \
    --dry-run=client -o yaml | $KC apply -f -
  $KC -n "$NS" create configmap fake-metrics-app --from-file=server.py="$DIR/fake-metrics-server.py" \
    --dry-run=client -o yaml | $KC apply -f -
  cat <<YAML | $KC apply -f -
apiVersion: apps/v1
kind: Deployment
metadata: {name: fake-metrics, namespace: $NS}
spec:
  replicas: 2
  selector: {matchLabels: {app: fake-metrics}}
  template:
    metadata: {labels: {app: fake-metrics}}
    spec:
      # The real (monitoring) node; the KWOK nodes are tainted and this has no toleration for them
      tolerations:
      - {key: monitoring, operator: Equal, value: "true", effect: NoSchedule}
      containers:
      - name: server
        image: public.ecr.aws/docker/library/python:3.12-slim
        command: ["python", "-u", "/app/server.py"]
        ports: [{containerPort: 8443}]
        readinessProbe: {httpGet: {path: /healthz, port: 8443, scheme: HTTPS}, periodSeconds: 5}
        resources: {requests: {cpu: 250m, memory: 128Mi}, limits: {memory: 512Mi}}
        volumeMounts:
        - {name: app, mountPath: /app}
        - {name: certs, mountPath: /certs, readOnly: true}
      volumes:
      - {name: app, configMap: {name: fake-metrics-app}}
      - {name: certs, secret: {secretName: fake-metrics-certs}}
---
apiVersion: v1
kind: Service
metadata: {name: fake-metrics, namespace: $NS}
spec:
  selector: {app: fake-metrics}
  ports: [{port: 443, targetPort: 8443}]
---
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata: {name: v1beta1.external.metrics.k8s.io}
spec:
  group: external.metrics.k8s.io
  version: v1beta1
  service: {name: fake-metrics, namespace: $NS, port: 443}
  caBundle: $(base64 -w0 "$tmp/ca.crt")
  groupPriorityMinimum: 100
  versionPriority: 100
YAML
  $KC -n "$NS" rollout status deployment/fake-metrics --timeout=5m
  $KC wait --for=condition=Available apiservice/v1beta1.external.metrics.k8s.io --timeout=3m
  for p in $(pods); do admin "$p" "set?value=100"; done
elif [ "$OP" = set ]; then
  V=${2:?value}; START=${3:?start file}
  [[ $V =~ ^[0-9]+$ ]] || { echo "value must be a number" >&2; exit 2; }
  python3 -c 'import time; print(time.time())' > "$START"
  for p in $(pods); do admin "$p" "set?value=$V"; done
elif [ "$OP" = stats ]; then
  for p in $(pods); do echo "$p $(admin "$p" stats)"; done
else
  $KC delete apiservice v1beta1.external.metrics.k8s.io --ignore-not-found
  $KC delete namespace "$NS" --ignore-not-found --wait=false
fi
