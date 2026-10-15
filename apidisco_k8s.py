#!/usr/bin/env python3
"""apidisco_k8s.py — Kubernetes-only API discovery from pod logs (no njs).

Discovers NGINX-ish pods through the Kubernetes API, streams their stdout logs
(plain access lines and/or apidisco JSON records), builds one OpenAPI 3.0 spec
per group, and serves specs/inventory. Optionally correlates the declared
surface (Ingress / Gateway API HTTPRoute / NGINX VirtualServer) and reports
declared-vs-observed gaps.

Everything comes from logs the pods already write — no njs module, no NGINX
config change, no sidecar.

Usage
  # one-shot: read the tail of every nginx-ish pod and write specs, exit
  python3 apidisco_k8s.py --kubeconfig ~/.kube/config --once --out ./specs

  # follow: keep streaming as pods come and go, serve on :9000
  python3 apidisco_k8s.py --kubeconfig ~/.kube/config --namespace prod \
      --name-contains nginx-ic --serve 0.0.0.0:9000
"""
import argparse
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "collector"))
import apidisco_specs as A  # noqa: E402


def pods_path(namespace, selector):
    base = ("/api/v1/namespaces/%s/pods" % namespace) if namespace else "/api/v1/pods"
    return base + (("?labelSelector=%s" % selector) if selector else "")


def pod_log_path(namespace, name, container, tail, follow=True):
    p = ("/api/v1/namespaces/%s/pods/%s/log?tailLines=%d&follow=%s"
         % (namespace, name, tail, "true" if follow else "false"))
    if container:
        p += "&container=%s" % container
    return p


def read_once(client, store, targets, tail, pattern):
    """Read up to `tail` lines from each target (ns, name, container); return count."""
    n = 0
    for ns, name, container in targets:
        stop = threading.Event()
        for line in client.stream(pod_log_path(ns, name, container, tail, follow=False),
                                  stop=stop):
            if A.ingest_line(store, line, pattern):
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kubeconfig", default=os.path.expanduser("~/.kube/config"))
    ap.add_argument("--k8s-context", default=None)
    ap.add_argument("--k8s-server", default=None)
    ap.add_argument("--k8s-token", default=None)
    ap.add_argument("--k8s-ca-file", default=None)
    ap.add_argument("--k8s-insecure", action="store_true")
    ap.add_argument("--namespace", default=None,
                    help="comma list of namespaces; default: all")
    ap.add_argument("--pod-selector", default=None, help="label selector")
    ap.add_argument("--name-contains", default="nginx",
                    help="only pods whose name contains this (default: nginx)")
    ap.add_argument("--container", default=None,
                    help="container name; default: nginx-ingress/nginx/first")
    ap.add_argument("--tail", type=int, default=200)
    ap.add_argument("--interval", type=int, default=30,
                    help="seconds between pod re-lists (follow mode)")
    ap.add_argument("--access-pattern", default=None,
                    help="named-group regex override for access lines")
    ap.add_argument("--access-format", default="with-host",
                    choices=sorted(A.ACCESS_FORMATS),
                    help="built-in access-log format preset (default: with-host)")
    ap.add_argument("--valid-status", default=A.DEFAULT_VALID_STATUS,
                    help="statuses that count as a real response for /gaps")
    ap.add_argument("--no-backend-errors", dest="backend_errors",
                    action="store_false", default=True,
                    help="do NOT count backend-origin 5xx as valid")
    ap.add_argument("--no-declared", dest="declared", action="store_false",
                    default=True, help="skip Ingress/HTTPRoute/VirtualServer")
    ap.add_argument("--once", action="store_true",
                    help="read each pod once and exit")
    ap.add_argument("--out", default="./specs")
    ap.add_argument("--group-by", default="server,api,upstream")
    ap.add_argument("--formats", default="openapi,csv")
    ap.add_argument("--serve", default="0.0.0.0:9000")
    a = ap.parse_args()

    cfg = A.resolve_k8s(a)
    if not cfg.get("server"):
        print("apidisco_k8s: no API server in kubeconfig (use --k8s-server)",
              file=sys.stderr)
        return 2
    client = A.k8s_client(cfg)
    store = A.Store([f.strip() for f in a.group_by.split(",") if f.strip()],
                    valid_status=a.valid_status, backend_errors=a.backend_errors)
    formats = {f.strip() for f in a.formats.split(",") if f.strip()}
    namespaces = ([s.strip() for s in a.namespace.split(",") if s.strip()]
                  if a.namespace else [None])
    pattern = a.access_pattern or A.ACCESS_FORMATS[a.access_format]

    def targets():
        out = []
        for ns in namespaces:
            for pod in A.select_pods(client.list(pods_path(ns, a.pod_selector)),
                                     a.name_contains):
                pns = pod["metadata"]["namespace"]
                nm = pod["metadata"]["name"]
                out.append((pns, nm, a.container or A.choose_container(pod)))
        return out

    if a.declared:
        recs = A.load_declared(client, [n for n in namespaces if n] or None)
        print("apidisco_k8s: %d declared routes" % len(recs), file=sys.stderr)
        for r in recs:
            store.add(r)

    if a.once:
        n = read_once(client, store, targets(), a.tail, pattern)
        A.write_all(store, a.out, formats)
        print("apidisco_k8s: %d log lines from %d pods -> %s"
              % (n, len(targets()), a.out))
        return 0

    host, _, port = a.serve.rpartition(":")
    srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), A.make_handler(store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("apidisco_k8s: serving on http://%s" % a.serve, file=sys.stderr)

    lock = threading.Lock()
    active = {}

    def supervisor():
        while True:
            for ns in namespaces:
                for pod in A.select_pods(client.list(pods_path(ns, a.pod_selector)),
                                         a.name_contains):
                    pns = pod["metadata"]["namespace"]
                    nm = pod["metadata"]["name"]
                    cont = a.container or A.choose_container(pod)
                    key = (pns, nm, cont)
                    with lock:
                        if key in active:
                            continue
                        stop = threading.Event()
                        active[key] = stop

                    def run(key=key, cont=cont, stop=stop):
                        try:
                            for line in client.stream(
                                    pod_log_path(key[0], key[1], cont, a.tail),
                                    stop=stop):
                                A.ingest_line(store, line, pattern)
                        finally:
                            with lock:
                                active.pop(key, None)

                    threading.Thread(target=A._run_guarded, args=(run,),
                                     daemon=True).start()
            time.sleep(a.interval)

    threading.Thread(target=A._run_guarded, args=(supervisor,), daemon=True).start()
    while True:
        time.sleep(10)
        A.write_all(store, a.out, formats)


if __name__ == "__main__":
    sys.exit(main() or 0)
