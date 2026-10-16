# deploystack-deploy

Deployment repository for [DeployStack](https://github.com/deploystackio/deploystack)
(open-source MCP hosting platform, AGPL-3.0) on the CodifyBites homelab,
managed by Arcane (environment 0 / CodeServer) and exposed through Pangolin.

This repo contains deployment configuration only — no DeployStack source.
Upstream license is preserved in [LICENSE](LICENSE); DeployStack is
© deploystackio, AGPL-3.0.

## Public routes

| URL | Service | Notes |
|---|---|---|
| https://deploystack.prod.codifybites.com | Caddy gateway | UI + API, Pangolin-protected |

The single route terminates at the **Caddy gateway** (`deploystack-gateway:80`),
which paths-splits internally (see `Caddyfile`):

- `/api/*` → `deploystack-backend:3000` — unbuffered (`flush_interval -1`),
  HTTP/1.1 upstream, so SSE streams (e.g. installations) pass through intact.
- everything else → `deploystack-frontend:80`.

The frontend keeps `VITE_DEPLOYSTACK_BACKEND_URL=https://deploystack.prod.codifybites.com`
(the app appends `/api`), so the browser talks to one origin and no
path-based routing happens at the Pangolin layer. The former separate
`deploystack-api` backend route was removed: one resource, one target, one
gateway.

The route is `pangolin.protected=true` with the standard LAN/internal
accept rules (192.168.101.0/24, 192.168.102.0/24, 172.18.0.0/16,
172.30.0.0/16), matching the pattern of the other CodifyBites stacks.

## Persistence

Host bind mounts (no named volumes for app data):

- `/docker/appdata/deploystack/postgres` — PostgreSQL data
- `/docker/appdata/deploystack/backend` — backend persistent data
- `/docker/appdata/deploystack/satellite` — satellite registration state (later)

## Required protected variables (Arcane project environment)

These must be set as **protected environment variables on the Arcane
`deploystack` project** (or provided by the coordinator) before deploy. They
are never committed to this repo.

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | strong generated PostgreSQL password |
| `DEPLOYSTACK_ENCRYPTION_SECRET` | strong generated backend encryption secret |

Generate with: `openssl rand -base64 36`

## Required GitHub secrets (for the CI/CD workflow)

Same set as the other CodifyBites stack repos:

| Secret | Purpose |
|---|---|
| `ARCANE_API_KEY` | Arcane API key (`X-Api-Key`) |
| `ARCANE_GIT_USERNAME` | username Arcane uses to clone this repo |
| `ARCANE_GIT_TOKEN` | read-only token for that clone |
| `PANGOLIN_ORG_KEY` | Pangolin API bearer key (org scope) |
| `PANGOLIN_ORG_ID` | Pangolin organization ID |

Optional repository variables: `ARCANE_BASE_URL` (default
`https://arcane.prod.codifybites.com`), `PANGOLIN_API_BASE_URL` (default
`https://api-pangolin.prod.codifybites.com/v1`),
`DEPLOYSTACK_HEALTHCHECK_URL` (health endpoint for the verify job; falls
back to local container checks on the runner host).

## First-run setup

1. Ensure the Arcane project env vars above are set, then deploy (push to
   `main` or workflow_dispatch).
2. Open **https://deploystack.prod.codifybites.com** — the upstream
   self-hosted flow starts with initial admin/user registration on first
   load (see [upstream self-hosting docs](https://docs.deploystack.io/self-hosted/quick-start)).
3. Complete the setup wizard / create the admin account.

## Post-setup: satellite (NOT deployed by default)

Functional MCP hosting requires a satellite; its registration token only
exists after first-run setup, so it is deliberately not part of the main
compose. After setup:

1. In the DeployStack UI (admin), generate a satellite registration token
   (Admin → Satellites → registration tokens).
2. Copy `satellite-compose.yml`, set `DEPLOYSTACK_REGISTRATION_TOKEN` (env
   var or Arcane protected var on the satellite project), and deploy it as a
   separate Arcane project (`SERVICE_NAME=deploystack-satellite`).
3. Create the Pangolin route `deploystack-satellite.prod.codifybites.com →
   http:deploystack-satellite:3001` (add the matching `pangolin.*` labels to
   the satellite service once deployed, or reconcile manually).
4. The satellite persists its registration key in
   `/docker/appdata/deploystack/satellite/backend.key.json`; keep that path
   across restarts to avoid re-pairing.

## CI/CD

`.github/workflows/ci-cd.yml` (self-hosted runner, labels
`self-hosted, codeserver`) follows the standard CodifyBites pattern:

1. **validate** — YAML parse + `docker compose config` with throwaway
   placeholder env (secrets never expanded or printed).
2. **deploy** (push to `main` only) — Arcane GitOps via
   `scripts/AddOrUpdateArcane.py`, then Pangolin reconciliation via
   `scripts/UpdateOrCreatePangolinConfig.py`.
3. **verify** — waits for containers / health endpoint.

## Layout

```
docker-compose.yml       # main stack (postgres, backend, frontend, caddy gateway)
Caddyfile                # gateway routing: /api/* -> backend, /* -> frontend
satellite-compose.yml    # post-setup satellite template (no token included)
.env.example             # required variable names only
.github/workflows/ci-cd.yml
scripts/                 # shared Arcane + Pangolin deploy scripts
LICENSE                  # upstream AGPL-3.0 (attribution)
```
