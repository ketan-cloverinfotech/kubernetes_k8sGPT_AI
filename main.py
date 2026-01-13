import os, json, time, sqlite3, re, ssl, urllib.request, urllib.parse
from typing import Any, Dict, Optional, List
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from openai import OpenAI

client = OpenAI()  # uses OPENAI_API_KEY from env

DB_PATH = os.getenv("DB_PATH", "/data/aiops.db")
RUNBOOKS_PATH = os.getenv("RUNBOOKS_PATH", "/data/runbooks.txt")

# K8s in-cluster
K8S_API = os.getenv("K8S_API", "https://kubernetes.default.svc")
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

REQ_TOTAL = Counter("aiops_reco_requests_total", "Total recommendation requests", ["status"])
LAT = Histogram("aiops_reco_request_latency_seconds", "Recommendation latency (seconds)")
OPENAI_TOTAL = Counter("aiops_reco_openai_calls_total", "OpenAI calls", ["status"])
K8S_TOTAL = Counter("aiops_reco_k8s_calls_total", "Kubernetes API calls", ["kind", "status"])

app = FastAPI(title="AIOps Recommendation API")

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            alertname TEXT,
            severity TEXT,
            namespace TEXT,
            pod TEXT,
            deployment TEXT,
            summary TEXT,
            recommendation TEXT,
            k8s_context TEXT,
            raw_json TEXT
        )
    """)
    conn.commit()
    return conn

def load_runbooks() -> str:
    return open(RUNBOOKS_PATH, "r", encoding="utf-8").read() if os.path.exists(RUNBOOKS_PATH) else ""

def retrieve(runbooks: str, q: str, max_chars: int = 2000) -> str:
    qtok = set(re.findall(r"[a-z0-9]+", q.lower()))
    best, best_score = "", 0
    for chunk in runbooks.split("\n\n"):
        ctok = set(re.findall(r"[a-z0-9]+", chunk.lower()))
        score = len(qtok & ctok)
        if score > best_score:
            best, best_score = chunk, score
    return best[:max_chars] if best else ""

def extract(payload: Dict[str, Any]) -> Dict[str, str]:
    alerts = payload.get("alerts") or []
    a = alerts[0] if alerts else {}
    labels = a.get("labels") or {}
    ann = a.get("annotations") or {}
    return {
        "alertname": labels.get("alertname", "unknown"),
        "severity": labels.get("severity", "unknown"),
        "namespace": labels.get("namespace", labels.get("kubernetes_namespace", "unknown")),
        "pod": labels.get("pod", labels.get("pod_name", "")),
        "deployment": labels.get("deployment", labels.get("kubernetes_deployment", "")),
        "summary": (ann.get("summary") or ann.get("description") or "")[:500],
    }

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=SA_CA_PATH) if os.path.exists(SA_CA_PATH) else ssl.create_default_context()
    return ctx

def k8s_get(path: str, timeout: int = 5) -> Optional[Dict[str, Any]]:
    if not os.path.exists(SA_TOKEN_PATH):
        return None
    token = open(SA_TOKEN_PATH, "r", encoding="utf-8").read().strip()
    url = K8S_API.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            K8S_TOTAL.labels(kind=path.split("/")[1] if path.startswith("/api/") else "get", status="success").inc()
            return data
    except Exception:
        K8S_TOTAL.labels(kind=path.split("/")[1] if path.startswith("/api/") else "get", status="error").inc()
        return None

def fetch_pod(namespace: str, pod: str) -> Optional[Dict[str, Any]]:
    if not (namespace and pod):
        return None
    return k8s_get(f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/{urllib.parse.quote(pod)}")

def fetch_pod_events(namespace: str, pod: str, max_items: int = 15) -> List[Dict[str, Any]]:
    if not (namespace and pod):
        return []
    qs = "fieldSelector=" + urllib.parse.quote(f"involvedObject.kind=Pod,involvedObject.name={pod}")
    data = k8s_get(f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/events?{qs}")
    items = (data or {}).get("items") or []
    def ts(e):
        return e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp") or ""
    items.sort(key=ts, reverse=True)
    out = []
    for e in items[:max_items]:
        out.append({
            "type": e.get("type",""),
            "reason": e.get("reason",""),
            "message": (e.get("message","") or "")[:300],
            "count": e.get("count", 1),
            "lastTimestamp": ts(e),
        })
    return out

def fetch_pod_logs(namespace: str, pod: str, tail: int = 80) -> str:
    if not (namespace and pod):
        return ""
    # logs endpoint returns plain text
    if not os.path.exists(SA_TOKEN_PATH):
        return ""
    token = open(SA_TOKEN_PATH, "r", encoding="utf-8").read().strip()
    url = K8S_API.rstrip("/") + f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/{urllib.parse.quote(pod)}/log?tailLines={tail}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=6) as r:
            return r.read().decode("utf-8", errors="ignore")[:4000]
    except Exception:
        return ""

def summarize_pod(pod_obj: Optional[Dict[str, Any]]) -> str:
    if not pod_obj:
        return "(pod not fetched)"
    st = pod_obj.get("status") or {}
    cs = st.get("containerStatuses") or []
    lines = []
    lines.append(f"phase={st.get('phase','')}")
    if cs:
        c0 = cs[0]
        lines.append(f"container={c0.get('name','')}")
        lines.append(f"restartCount={c0.get('restartCount',0)}")
        state = c0.get("state") or {}
        last = c0.get("lastState") or {}
        if "waiting" in state:
            w = state["waiting"]
            lines.append(f"state.waiting.reason={w.get('reason','')}")
            lines.append(f"state.waiting.message={(w.get('message','') or '')[:200]}")
        if "terminated" in state:
            t = state["terminated"]
            lines.append(f"state.terminated.reason={t.get('reason','')}")
            lines.append(f"state.terminated.exitCode={t.get('exitCode','')}")
        if "terminated" in last:
            t = last["terminated"]
            lines.append(f"last.terminated.reason={t.get('reason','')}")
            lines.append(f"last.terminated.exitCode={t.get('exitCode','')}")
    return "\n".join(lines)[:2000]

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/recommend")
def recommend(payload: Dict[str, Any]):
    t0 = time.time()
    try:
        info = extract(payload)
        runbooks = load_runbooks()

        # K8s context (if labels include pod)
        pod_obj = fetch_pod(info["namespace"], info["pod"]) if info.get("pod") else None
        events = fetch_pod_events(info["namespace"], info["pod"]) if info.get("pod") else []
        logs = fetch_pod_logs(info["namespace"], info["pod"]) if info.get("pod") else ""
        pod_summary = summarize_pod(pod_obj)

        # lightweight runbook retrieval
        ctx = retrieve(runbooks, f"{info['alertname']} {info['summary']} {info['namespace']} {info['severity']} {info.get('pod','')}")

        system = (
            "You are a Kubernetes SRE assistant. Provide safe, step-by-step troubleshooting guidance. "
            "No self-healing actions. Use the provided Kubernetes evidence (pod status/events/logs) to be specific. "
            "Include kubectl commands and what to validate in Prometheus/Grafana."
        )

        user = f"""Alert:
- alertname: {info['alertname']}
- severity: {info['severity']}
- namespace: {info['namespace']}
- pod: {info.get('pod','')}
- deployment: {info.get('deployment','')}
- summary: {info['summary']}

Kubernetes evidence (most important):
Pod summary:
{pod_summary}

Recent events (newest first):
{json.dumps(events, indent=2) if events else "(no events or no pod label provided)"}

Log tail:
{logs if logs else "(no logs or no pod label provided)"}

Runbook context:
{ctx if ctx else "(no matching runbook snippet)"}

Return:
1) Probable cause (based on evidence)
2) Immediate checks (commands)
3) Mitigation steps
4) What confirms recovery
"""

        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        try:
            resp = client.responses.create(model=model, instructions=system, input=user)
            text = resp.output_text
            OPENAI_TOTAL.labels("success").inc()
        except Exception as e:
            text = f"OpenAI call failed: {e}"
            OPENAI_TOTAL.labels("error").inc()

        # Store
        k8s_context = {
            "pod_summary": pod_summary,
            "events": events,
            "logs_tail": logs[:4000],
        }
        conn = db()
        conn.execute(
            "INSERT INTO recommendations(ts, alertname, severity, namespace, pod, deployment, summary, recommendation, k8s_context, raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), info["alertname"], info["severity"], info["namespace"], info.get("pod",""), info.get("deployment",""),
             info["summary"], text, json.dumps(k8s_context)[:200000], json.dumps(payload)[:200000]),
        )
        conn.commit()
        conn.close()

        REQ_TOTAL.labels("success").inc()
        LAT.observe(time.time() - t0)
        return {"alert": info, "k8s_context": k8s_context, "recommendation": text}

    except Exception as e:
        REQ_TOTAL.labels("error").inc()
        LAT.observe(time.time() - t0)
        return {"error": str(e)}
