# Retrieval (vss2)

Query the indexed VSS archive through the **backend API** (`source-code/retrieval/video-backend`). Every route below `/api/v1` (except `/metadata/ingest-config`) needs a JWT — start with `login`.

Base URL: `http://<backend-host>` → API prefix `/api/v1`.
Resolve `INGRESS_URL`, `USERNAME`, and `PASSWORD` from the single
`/config/*.config` team file. Never search the repository's `team-configs/`.

## Skills → routes

| Skill | Route(s) |
|-------|----------|
| [login](login/SKILL.md) | `POST /auth/login`, `GET /auth/me` |
| [search](search/SKILL.md) | `POST /search` (+ `tools/search`, `agent/search-and-answer`) |
| [list-metadata](list-metadata/SKILL.md) | `GET /metadata/schema`, `/metadata/values`, `/metadata/ingest-config` |
| [dashboard](dashboard/SKILL.md) | `GET /dashboard/stats` |
| [suggest-prompts](suggest-prompts/SKILL.md) | `GET /suggestions` |
| [videos](videos/SKILL.md) | `/videos/explore`, `/stream`, `/playback-url`, `/detections`, `/metadata`, `POST /videos/synthesize` |
| [agent-qa](agent-qa/SKILL.md) | `POST /agent/ask`, `/agent/search-and-answer`, `tools/*` |

Raw VastDB SDK access lives under [vast-database](../vast-database/README.md) (`vastdb-read` / `vastdb-write`), not the JWT API.

## Auth pattern

```bash
TOKEN=$(curl -s -X POST "$BACKEND/api/v1/auth/login" -H "Content-Type: application/json" \
  -d '{"username":"<user>","password":"<pass>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s "$BACKEND/api/v1/..." -H "Authorization: Bearer $TOKEN"
```

`/videos/stream` and `/videos/playback-url` take the JWT as a `?token=` query param (HTML5 `<video>` can't set headers); everything else uses the `Authorization` header.

## Routes that do NOT exist (don't invent)

No `/reports`, `/alerts`, `/analytics`, `/videos/ask`, `/tags`, `/locations`, or `/extra-metadata`. Reporting/summaries = `POST /videos/synthesize`; Q&A = `POST /agent/ask`; filter discovery = `/metadata/*`; aggregates = `/dashboard/stats`.

## vss2 backing store

VastDB `vss-db` / `vss-schema` / `vss-collection` (+ `vss-prompts-events`); Cosmos-Embed1 256-dim hybrid vectors (`vectors` text + `vectors_visual`). All row access is ACL-filtered by the caller's user.
