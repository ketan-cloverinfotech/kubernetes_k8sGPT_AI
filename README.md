# Kubernetes AIOps Integration (GKE) — End-to-End Setup From Scratch

This guide documents the exact pipeline you built (based on the manifests/files you shared) and how to recreate it from scratch.

**Goal:**  
When a Kubernetes alert fires (e.g., CrashLoopBackOff), the system automatically:
1) routes the alert to **n8n** via **Alertmanager**,  
2) calls your **AI-Reco** service to fetch K8s context (pod/events/logs),  
3) generates a recommendation (LLM), and  
4) sends the result by **email**.

---

## 1) What components/tools are used

### Kubernetes / Monitoring
- **GKE cluster + kubectl**
- **Helm**
- **kube-prometheus-stack** (Prometheus + Alertmanager + Grafana)
- **Prometheus Operator CRDs**
  - `PrometheusRule` (your alert rules)
  - `AlertmanagerConfig` (your routing to n8n)

### AIOps Automation
- **n8n** (Webhook → HTTP Request → Code → Send Email)
- **AI-Reco** (custom Python/FastAPI microservice: `/recommend`)
- **curlimages/curl** test pods for quick POST testing

### Notifications + AI
- **SMTP** (Gmail/app password or org SMTP)
- **OpenAI API** (used by AI-Reco to generate recommendations)

---

## 2) Files you shared (what each one does)

| File | Purpose |
|---|---|
| `aiops-badpod-rules.yaml` | PrometheusRule: fires alert `PodCrashLoop` when pod enters CrashLoopBackOff |
| `aiops-test-rule.yaml` | PrometheusRule: test alert `AIOpsWebhookTest` (always fires) |
| `amcfg-n8n.yaml` | AlertmanagerConfig: routes alerts to n8n webhook (`/webhook/alertmanager`) |
| `ai-reco-rbac.yaml` | ServiceAccount + ClusterRole + ClusterRoleBinding for AI-Reco to read pod/events/logs |
| `ai-reco-pvc.yaml` | PVC for AI-Reco (workspace/data) |
| `ai-reco-deploy.yaml` | Deployment for AI-Reco (mounts configmap, uses secrets/env) |
| `ai-reco.yaml` | **Not valid YAML currently** (some lines in embedded Python are not indented) — use the split files instead |
| `main.py` | AI-Reco service code (FastAPI endpoint `/recommend`) |
| `n8n-fix.yaml` | n8n deployment + service (you later changed service to NodePort for UI/testing) |
| `k8s-gpt-cr.yaml` | K8sGPT custom resource (optional: for K8sGPT analysis/metrics pipeline) |

---

## 3) Target namespaces (as in your cluster)

- `monitoring` → kube-prometheus-stack (Prometheus/Grafana/Alertmanager) + rules + alertmanager config  
- `aiops` → n8n + ai-reco  
- `ai-test` → sample failing pods (e.g., `badpod`)  
- `k8sgpt-operator-system` → K8sGPT operator + CR (optional)

---

## 4) Setup from scratch (recommended order)

## Step A — Create namespaces
```bash
kubectl create ns monitoring || true
kubectl create ns aiops || true
kubectl create ns ai-test || true
```

---

## Step B — Install kube-prometheus-stack (Prometheus/Grafana/Alertmanager)
If you already installed it, skip.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring
```

### (Optional) Expose UI via NodePort (for testing)
You already have NodePorts like:
- Prometheus: `30990`
- Alertmanager: `30999`
- Grafana: `32434`

To set NodePort manually:
```bash
kubectl -n monitoring patch svc monitoring-kube-prometheus-prometheus -p '{"spec":{"type":"NodePort"}}'
kubectl -n monitoring patch svc monitoring-kube-prometheus-alertmanager -p '{"spec":{"type":"NodePort"}}'
kubectl -n monitoring patch svc monitoring-grafana -p '{"spec":{"type":"NodePort"}}'
```

---

## Step C — Create Prometheus rules (alerts)
Apply your rules:

```bash
kubectl apply -f aiops-badpod-rules.yaml
kubectl apply -f aiops-test-rule.yaml
```

> Note: `aiops-test-rule.yaml` generates a synthetic alert (`vector(1)`).  
> It will NOT have pod/namespace metadata unless you add those labels.

---

## Step D — Deploy n8n (workflow engine)
Apply your n8n manifest:

```bash
kubectl apply -f n8n-fix.yaml
```

### Expose n8n UI
In your cluster, `n8n-svc` is NodePort `32317`. If your service is still ClusterIP, patch it:

```bash
kubectl -n aiops patch svc n8n-svc -p '{"spec":{"type":"NodePort"}}'
```

Check:
```bash
kubectl -n aiops get svc n8n-svc
```

---

## Step E — Configure SMTP in n8n (Email sending)
In n8n:
1. Open **Credentials** → create **SMTP** credential
2. Use:
   - Gmail: **app password** (recommended), SMTP host `smtp.gmail.com`, port `587`, TLS STARTTLS
   - Or your org SMTP

Then in your workflow: **Send Email** node → select that SMTP credential.

---

## Step F — Deploy AI-Reco (AI recommendation service)

### F1) RBAC
```bash
kubectl apply -f ai-reco-rbac.yaml
```

### F2) PVC
```bash
kubectl apply -f ai-reco-pvc.yaml
```

### F3) Create ConfigMap from `main.py`
Your `ai-reco-deploy.yaml` expects a ConfigMap named **ai-reco-app** with `main.py`.

```bash
kubectl -n aiops create configmap ai-reco-app \
  --from-file=main.py=main.py \
  --dry-run=client -o yaml | kubectl apply -f -
```

### F4) Create secrets + env (OpenAI key, model, etc.)
Your deployment uses:
- `envFrom.secretRef.name: ai-reco-secrets`
- `envFrom.configMapRef.name: ai-reco-env`

Create them like this (edit values as needed):

```bash
# ConfigMap for non-secret env
kubectl -n aiops create configmap ai-reco-env \
  --from-literal=OPENAI_MODEL=gpt-4o-mini \
  --from-literal=POD_LOG_TAIL_LINES=50 \
  --dry-run=client -o yaml | kubectl apply -f -

# Secret for API keys
kubectl -n aiops create secret generic ai-reco-secrets \
  --from-literal=OPENAI_API_KEY='YOUR_OPENAI_KEY' \
  --dry-run=client -o yaml | kubectl apply -f -
```

### F5) Deploy AI-Reco
```bash
kubectl apply -f ai-reco-deploy.yaml
kubectl apply -f ai-reco.yaml  # (Optional) only if you FIX it; otherwise skip
```

Check:
```bash
kubectl -n aiops get deploy,po,svc | egrep 'ai-reco|NAME'
```

AI-Reco service in your cluster:
- `ai-reco.aiops.svc.cluster.local:8080`

---

## Step G — Configure Alertmanager → n8n routing

### G1) Create basic auth secret (used by AlertmanagerConfig)
Your `amcfg-n8n.yaml` references secret **n8n-webhook-basic-auth** in `monitoring`.

```bash
kubectl -n monitoring create secret generic n8n-webhook-basic-auth \
  --from-literal=username='youruser' \
  --from-literal=password='yourpass'
```

### G2) In n8n Webhook node, enable Basic Auth
- Webhook node → **Authentication: Basic Auth**
- Set username/password to match the secret

### G3) Apply AlertmanagerConfig
```bash
kubectl apply -f amcfg-n8n.yaml
```

> The webhook URL configured in `amcfg-n8n.yaml` is:
> `http://n8n-svc.aiops.svc.cluster.local:5678/webhook/alertmanager`  
> This is the **Production** webhook URL in n8n (not webhook-test).

---

## 5) Build the n8n workflow (correct config)

Your workflow nodes:
1) **Webhook** (path: `/alertmanager`)
2) **HTTP Request** (POST to AI-Reco `/recommend`)
3) **Code (JavaScript)**
4) **Send Email**

### Node 1 — Webhook
- Method: POST
- Path: `alertmanager`
- Response: Immediately
- Auth: Basic Auth (if used with AlertmanagerConfig)

**Important:**
- **Test URL**: `/webhook-test/alertmanager` (works only while listening in editor)
- **Production URL**: `/webhook/alertmanager` (works when workflow is active)

### Node 2 — HTTP Request (CRITICAL FIX)
Your Webhook output is an object like:
```json
{
  "headers": {...},
  "params": {...},
  "query": {...},
  "body": { "status": "...", "alerts": [...] }
}
```

But AI-Reco expects the **body only**:
```json
{ "status": "...", "alerts": [...] }
```

✅ Correct body expression in HTTP Request node:
- **Body Content Type**: JSON
- **JSON** field (Expression):
```text
={{ $json.body }}
```
(or)
```text
={{ $node["Webhook"].json.body }}
```

If you pass the whole webhook object, AI-Reco will return:
`alertname=unknown, namespace=unknown, pod not fetched` — exactly what you observed.

### Node 3 — Code (JavaScript) — updated
Use this Code node to produce a clean email body and keep the raw response:

```javascript
const item = $input.first();
const d = item.json;

// AI-Reco returns: { alert: {...}, k8s_context: {...}, recommendation: "..." }
const a = d.alert || {};
const ctx = d.k8s_context || {};

const title = `🚨 AIOps AI Suggestion`;

const meta = [
  `Alert: ${a.alertname || 'unknown'}`,
  `Severity: ${a.severity || 'unknown'}`,
  `Namespace: ${a.namespace || 'unknown'}`,
  a.pod ? `Pod: ${a.pod}` : null,
  a.deployment ? `Deployment: ${a.deployment}` : null,
  a.summary ? `Summary: ${a.summary}` : null,
].filter(Boolean).join('\n');

const podSummary = ctx.pod_summary
  ? `\n\n--- Pod summary ---\n${ctx.pod_summary}`
  : `\n\n--- Pod summary ---\n(pod not fetched)`;

const events = (ctx.events || [])
  .slice(0, 6)
  .map(e => `- [${e.type || ''}/${e.reason || ''}] ${e.message || ''} (x${e.count || 1})`)
  .join('\n');

const eventsBlock = events ? `\n\n--- Events (top 6) ---\n${events}` : '';
const logs = ctx.logs_tail ? `\n\n--- Logs (tail) ---\n${ctx.logs_tail}` : '';

const reco = d.recommendation
  ? `\n\n--- Recommendation ---\n${d.recommendation}`
  : `\n\n--- Recommendation ---\n(No recommendation returned)`;

const msg = `${title}\n\n${meta}${podSummary}${eventsBlock}${logs}${reco}`;

return [{ json: { message: msg, raw: d } }];
```

### Node 4 — Send Email
Use these expressions:

**Subject**
```text
={{ $json.raw.alert.alertname }} | {{ $json.raw.alert.namespace }}/{{ $json.raw.alert.pod }} | {{ $json.raw.alert.severity }}
```

**Body** (HTML or Text)
```text
={{ $json.message }}
```

✅ This avoids the “Referenced node doesn’t exist” error and avoids `undefined` emails.

---

## 6) End-to-end testing commands (inside cluster)

### Test AI-Reco directly
```bash
kubectl -n monitoring run recotest --rm -i --restart=Never --image=curlimages/curl -- \
  sh -c 'curl -sS -XPOST http://ai-reco.aiops.svc.cluster.local:8080/recommend \
  -H "Content-Type: application/json" \
  -d "{\"status\":\"firing\",\"alerts\":[{\"labels\":{\"alertname\":\"PodCrashLoop\",\"severity\":\"warning\",\"namespace\":\"ai-test\",\"pod\":\"badpod\"},\"annotations\":{\"summary\":\"badpod is crashing\"}}]}"; echo'
```

### Test n8n Webhook (Test mode)
Only works when Webhook node is listening in editor:

```bash
kubectl -n aiops run n8ntrigger --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'curl -sS -XPOST http://n8n-svc.aiops.svc.cluster.local:5678/webhook-test/alertmanager \
  -H "Content-Type: application/json" \
  -d "{\"status\":\"firing\",\"alerts\":[{\"labels\":{\"alertname\":\"PodCrashLoop\",\"severity\":\"warning\",\"namespace\":\"ai-test\",\"pod\":\"badpod\"},\"annotations\":{\"summary\":\"badpod is crashing\"}}]}"; echo'
```

### Test n8n Webhook (Production mode)
Works when workflow is **Active**:

```bash
kubectl -n aiops run n8ntrigger --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'curl -sS -XPOST http://n8n-svc.aiops.svc.cluster.local:5678/webhook/alertmanager \
  -H "Content-Type: application/json" \
  -d "{\"status\":\"firing\",\"alerts\":[{\"labels\":{\"alertname\":\"PodCrashLoop\",\"severity\":\"warning\",\"namespace\":\"ai-test\",\"pod\":\"badpod\"},\"annotations\":{\"summary\":\"badpod is crashing\"}}]}"; echo'
```

---

## 7) Troubleshooting (based on issues you hit)

### A) Email is received but shows “unknown / pod not fetched”
**Cause:** HTTP Request node sent the wrong JSON (whole webhook object).  
**Fix:** Use:
```text
={{ $json.body }}
```

### B) HTTP Request error: “JSON parameter needs to be valid JSON”
**Cause:** You pasted object text (shows `[object Object]`).  
**Fix:** Use expression in JSON field:
```text
={{ $json.body }}
```

### C) Send Email error: “Referenced node doesn’t exist”
**Cause:** your expression referenced node name `Code` but node is named `Code in JavaScript`.  
**Fix:** use `$json.message` (from input), not `$node["Code"]...`.

### D) Webhook shows “Listening for test event” but nothing triggers
You are calling **/webhook/** instead of **/webhook-test/** while in editor mode.  
- Editor listen = `/webhook-test/...`
- Active workflow = `/webhook/...`

### E) Connection timed out (SMTP)
- check firewall / egress
- correct SMTP host/port
- if running in private cluster, allow egress or Cloud NAT

---

## 8) “From scratch” quick apply (using your manifests)

Run in this order from the folder where the files exist:

```bash
kubectl create ns monitoring || true
kubectl create ns aiops || true
kubectl create ns ai-test || true

# monitoring stack (if not installed)
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install monitoring prometheus-community/kube-prometheus-stack -n monitoring

# alerts + routing
kubectl apply -f aiops-badpod-rules.yaml
kubectl apply -f aiops-test-rule.yaml
kubectl -n monitoring create secret generic n8n-webhook-basic-auth \
  --from-literal=username='youruser' --from-literal=password='yourpass' || true
kubectl apply -f amcfg-n8n.yaml

# n8n
kubectl apply -f n8n-fix.yaml
kubectl -n aiops patch svc n8n-svc -p '{"spec":{"type":"NodePort"}}' || true

# ai-reco
kubectl apply -f ai-reco-rbac.yaml
kubectl apply -f ai-reco-pvc.yaml
kubectl -n aiops create configmap ai-reco-app --from-file=main.py=main.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n aiops create configmap ai-reco-env \
  --from-literal=OPENAI_MODEL=gpt-4o-mini \
  --from-literal=POD_LOG_TAIL_LINES=50 \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n aiops create secret generic ai-reco-secrets \
  --from-literal=OPENAI_API_KEY='YOUR_OPENAI_KEY' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f ai-reco-deploy.yaml
```

---

## 9) Notes about `ai-reco.yaml`
`ai-reco.yaml` currently contains embedded Python content that is not indented correctly, so it’s invalid YAML.

✅ Best practice (what you are already doing):
- keep code as a separate file (`main.py`)
- create ConfigMap using `--from-file`
- deploy using `ai-reco-deploy.yaml`, `ai-reco-rbac.yaml`, `ai-reco-pvc.yaml`

---

## Appendix — Your current cluster references (from `kubectl get all -A`)
- n8n service (NodePort): `n8n-svc.aiops` → `5678:32317`
- AI-Reco service: `ai-reco.aiops` → `8080`
- Alertmanager (NodePort): `monitoring-kube-prometheus-alertmanager` → `9093:30999`
- Prometheus (NodePort): `monitoring-kube-prometheus-prometheus` → `9090:30990`
- Grafana (NodePort): `monitoring-grafana` → `80:32434`

---

If you want, I can also generate a **single “all-in-one” clean manifest** (one YAML with namespaces, RBAC, ConfigMaps, deployments, services, rules, alertmanagerconfig) so you can do: `kubectl apply -f all.yaml`.
