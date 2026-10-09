# Deployment (vss2)

Deploy and operate the **retrieval side** (backend, frontend, batch-sync) on Kubernetes from `deployments/vss-k8s-application/`. Use `KUBECONFIG=/config/team-<num>-k8s.yaml` (e.g. `/config/team-1-k8s.yaml`). The DataEngine ingest pipeline is already running for the hackathon — use `ingest/` skills (upload / re-ingest), not pipeline editing.

For **hackathon mini-apps** on top of VSS when the VM has no Docker/registry, use [deploy-app-no-registry](deploy-app-no-registry/SKILL.md) (on-cluster only: public image + ConfigMap + Secret, Ingress path `/app` on the team host).

## Skills

| Skill | Purpose |
|-------|---------|
| [build-yamls](build-yamls/SKILL.md) | Fill `/config/backend-secret.yaml` + image tags; align with `/config/vss-cli-secret.yaml` |
| [deploy](deploy/SKILL.md) | Build/push images + `QUICK_DEPLOY.sh <ns> <cluster>` + ingress DNS |
| [health](health/SKILL.md) | `kubectl get pods`, `/health` (8000), `GET /api/v1/config` |
| [deploy-app-no-registry](deploy-app-no-registry/SKILL.md) | On-cluster app at team host `/app` without Docker build/push |

## Components

| App label | Port | Probe | Image |
|-----------|------|-------|-------|
| `video-backend` | 8000 | `/health` | `vss-video-backend` |
| `video-frontend` | 80 | — | `vss-video-frontend` |
| `video-batch-sync` | — | — | `vss-video-batch-sync` |

Secret `video-backend-secret` comes from `/config/backend-secret.yaml` (`config.yaml` at `/etc/secrets`). If missing, ask the user to place it there. Never search the repo's `team-configs/`. Ingress: `video-lab.<cluster>.vastdata.com` (frontend + `/api` → backend).

## Critical coupling

Backend secret (underscore keys) must align with ingest `/config/vss-cli-secret.yaml` on S3, models, dims (**256**), and collection names, or search returns nothing. VastDB **endpoint** is the intentional exception: backend `vdb_endpoint` = Query Engine VIP (reads), ingest `vdbendpoint` = data VIP (writes).
