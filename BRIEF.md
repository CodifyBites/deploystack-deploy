# Deploy DeployStack on Arcane CodeServer

## Goal
Create a separate CodifyBites deployment repository/project for https://github.com/deploystackio/deploystack and prepare it for Arcane CodeServer deployment without modifying the upstream repository.

## Upstream facts
- Public repo: https://github.com/deploystackio/deploystack.git
- Default branch: main.
- Production compose path: docker-compose.yml.
- Services: postgres:18-alpine, deploystack/backend:latest, deploystack/frontend:latest.
- Official docs say a satellite is required for functional MCP hosting and must be deployed separately after setup.
- Official compose currently uses named volumes and localhost URLs; adapt the deployment for this homelab.

## Homelab requirements
- Deploy to Arcane environment 0 / CodeServer.
- Use host-visible persistence under:
  /docker/appdata/deploystack/postgres
  /docker/appdata/deploystack/backend
- Attach frontend and backend to external Docker network `proxy`; keep postgres on a private app network and backend on both private + proxy.
- Public routes:
  deploystack.prod.codifybites.com -> frontend
  deploystack-api.prod.codifybites.com -> backend
  deploystack-satellite.prod.codifybites.com -> satellite only after pairing; do not invent a registration token.
- Add Arcane labels and Pangolin labels to frontend/backend. Protected status should be true unless source/client constraints require otherwise; use existing LAN/internal rules pattern.
- Frontend backend URL must be the public API URL (or the exact reverse-proxy path chosen by the worker), not localhost.
- Backend DEPLOYSTACK_FRONTEND_URL must be the public frontend URL.
- Use a strong generated `DEPLOYSTACK_ENCRYPTION_SECRET` and a strong generated PostgreSQL password; never commit or print values. Store required protected variables in Arcane/project environment or 1Password only if coordinator handles it.
- Prefer upstream released images; do not build from source unless required.
- Preserve all data on local disk; do not use named volumes for application data.
- Do not deploy a satellite yet because its registration token is generated only after first setup. Prepare a separate satellite compose/template if useful, but stop before requiring the token.

## Required artifact
Create a new public GitHub repo under CodifyBites named `deploystack-deploy` (unless an existing repo with this exact deployment purpose is found). Include:
- docker-compose.yml derived from upstream but adapted as above.
- .env.example containing variable names only, no secrets.
- README.md with deployment URLs, required Arcane protected variables, first-run setup URL, and post-setup satellite instructions. Do not include real credentials.
- GitHub Actions workflow or deployment metadata sufficient for Arcane registration/sync; do not trigger production deploy from worker and do not merge main.
- Keep upstream license/attribution and do not copy unnecessary source tree.

## Verification
- Inspect upstream files before editing.
- Validate Compose syntax with placeholders without printing expanded values.
- Verify no secrets/tokens are present in committed files.
- Verify frontend/backend image names are real public images if network access permits.
- Create a branch and commit; do not merge to main and do not call Arcane up from worker.
- Report exact repository, branch, commit, changed files, validation result, and any blocker (especially image availability or uncertain API paths).

## Security
- Never use OP_SERVICE_ACCOUNT_TOKEN in the worker.
- Never ask for or print a password/token.
- Do not create a satellite registration token or fake one.
