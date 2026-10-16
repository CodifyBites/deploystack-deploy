"""Arcane GitOps deployment for GitHub Actions.

Ported from the shared GitLab pipeline (workflows-pangolin-portainer-shared,
deploy/AddOrUpdateArcane.py) with these GitHub adaptations:

- Project name comes from SERVICE_NAME (explicit) or GITHUB_REPOSITORY's repo
  component — never CI_PROJECT_NAME.
- The GitOps clone URL defaults to GITHUB_SERVER_URL/GITHUB_REPOSITORY + ".git"
  (a token-free https URL). Arcane authenticates with the SEPARATE
  ARCANE_GIT_USERNAME / ARCANE_GIT_TOKEN fields (GitHub Actions secrets), so no
  credential is ever embedded in the URL or a file.
- The compose file defaults to COMPOSE_FILE_PATH or ./docker-compose.yaml
  relative to the repository root (the script lives in scripts/).

Fail-closed behaviour: missing ARCANE_API_KEY, missing project name, missing
clone credentials for a private repo, missing Arcane environments in labels,
per-environment deployment failures, and failed syncs all raise — errors are
never swallowed.
"""

import os
import time
import json
import requests
from DockerComposeLabelParser import DockerComposeLabelParser

ARCANE_API_KEY = os.getenv('ARCANE_API_KEY')
ARCANE_BASE_URL = os.getenv('ARCANE_BASE_URL', 'https://arcane.prod.codifybites.com').rstrip('/')

# --- Project identity (GitHub) -----------------------------------------------------

# SERVICE_NAME is the explicit, preferred source (set in the workflow). Falling
# back to the repository name keeps workflow_dispatch runs consistent.
_GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')
PROJECT_NAME = (
    os.getenv('SERVICE_NAME')
    or (_GITHUB_REPOSITORY.split('/')[-1] if _GITHUB_REPOSITORY else None)
)

# --- GitOps source configuration ---------------------------------------------------
#
# In the GitOps model Arcane clones the service repository itself and deploys the
# compose file from it, so the workflow only has to *register* the source once and
# then nudge Arcane to sync. The compose content is never uploaded by the pipeline.

# Clean, TOKEN-FREE https URL Arcane will clone. Defaults to this GitHub
# repository. Credentials travel separately via ARCANE_GIT_USERNAME/ARCANE_GIT_TOKEN.
_GITHUB_SERVER_URL = os.getenv('GITHUB_SERVER_URL', 'https://github.com')
GIT_REPO_URL = os.getenv('ARCANE_GIT_REPO_URL') or (
    f"{_GITHUB_SERVER_URL}/{_GITHUB_REPOSITORY}.git" if _GITHUB_REPOSITORY else None
)
# Name of the reusable Git Repository object stored in Arcane.
GIT_REPO_NAME = os.getenv('ARCANE_GIT_REPO_NAME') or PROJECT_NAME
# Branch Arcane tracks.
GIT_BRANCH = os.getenv('ARCANE_GIT_BRANCH', 'main')
# Path to the compose file inside the service repository.
COMPOSE_PATH = os.getenv('ARCANE_COMPOSE_PATH', 'docker-compose.yaml')
# Credentials Arcane uses to clone the private repo (fine-grained GitHub PAT or
# similar read-only token, passed as SEPARATE fields — never in the URL).
GIT_USERNAME = os.getenv('ARCANE_GIT_USERNAME')
GIT_TOKEN = os.getenv('ARCANE_GIT_TOKEN')
# Whether Arcane should poll the repo on an interval and redeploy on new commits.
AUTO_SYNC = os.getenv('ARCANE_AUTO_SYNC', 'true').lower() == 'true'
SYNC_INTERVAL = int(os.getenv('ARCANE_SYNC_INTERVAL', '60'))
# Sync every file in the compose directory (Arcane "Sync Files"), not just the
# compose file — needed when the compose references sibling files (.env, configs).
SYNC_FILES = os.getenv('ARCANE_SYNC_FILES', 'true').lower() == 'true'
# How long to wait for the deployed project's containers to come up, and how
# often to poll, before failing the job.
DEPLOY_TIMEOUT = int(os.getenv('ARCANE_DEPLOY_TIMEOUT', '180'))
POLL_INTERVAL = int(os.getenv('ARCANE_POLL_INTERVAL', '5'))
# Image pull policy used when bringing the project up: missing | always | never
PULL_POLICY = os.getenv('ARCANE_PULL_POLICY', 'missing')
# When true, every service must be running to pass; otherwise at least one
# running container is enough (an up-but-unhealthy container still counts).
REQUIRE_ALL_RUNNING = os.getenv('ARCANE_REQUIRE_ALL_RUNNING', 'false').lower() == 'true'
# Build images before deploying when the compose declares `build:` services, and how
# long to allow for that build. Compose only builds a *missing* image, so without
# this a re-deploy silently reuses the previous pipeline's image.
BUILD_PROJECTS = os.getenv('ARCANE_BUILD_PROJECTS', 'true').lower() == 'true'
BUILD_TIMEOUT = int(os.getenv('ARCANE_BUILD_TIMEOUT', '900'))

# Compose file in THIS checkout, read only to discover the target environments
# from the ``arcane.server.hosts`` labels. Workflow runs from the repo root.
COMPOSE_FILE = os.getenv('COMPOSE_FILE_PATH', 'docker-compose.yaml')

HEADERS = {
    "X-Api-Key": ARCANE_API_KEY
}
JSON_HEADERS = {
    "X-Api-Key": ARCANE_API_KEY,
    "Content-Type": "application/json"
}


def _raise_for_status(response):
    """Raise for an HTTP error, printing the response body for diagnostics."""
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        print("Response content:", response.text)
        raise


def _parse_json(response, context):
    """Parse a JSON response, raising a clear error for empty/non-JSON bodies.

    A 2xx response with an empty or HTML body (e.g. an SSO/auth proxy in front
    of Arcane, a wrong ARCANE_BASE_URL, or a silently dropped API key) otherwise
    surfaces as an opaque JSONDecodeError. Turn it into an actionable message.
    """
    try:
        return response.json()
    except ValueError:
        snippet = (response.text or '').strip()
        raise Exception(
            f"{context}: expected a JSON response but got HTTP {response.status_code} "
            f"with Content-Type '{response.headers.get('Content-Type', 'unknown')}'. "
            f"Body starts with: {snippet[:500]!r}. "
            "Verify ARCANE_BASE_URL points directly at the Arcane API, that "
            "ARCANE_API_KEY is valid, and that the API is not behind an auth/SSO proxy."
        )


def GetAllEnvironments():
    """Return every Arcane environment record (list from the 'data' envelope)."""
    print("Fetching Arcane environments...")
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/environments?limit=1000",
        headers=HEADERS,
        timeout=10
    )
    _raise_for_status(response)
    return _parse_json(response, "List environments").get('data', [])


def GetEnvironmentID(name):
    """Resolve an Arcane environment name (or id) to its environment id.

    The built-in local Docker host is environment ``0``; ``0`` and ``local``
    are accepted as aliases for it without hitting the API.
    """
    if name.lower() in ('0', 'local'):
        return "0"

    print("Fetching Arcane environments...")
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/environments?limit=1000",
        headers=HEADERS,
        timeout=10
    )
    _raise_for_status(response)

    environments = _parse_json(response, "List environments").get('data', [])
    for environment in environments:
        if environment.get('name', '').lower() == name.lower():
            return str(environment['id'])
        # Allow targeting an environment by its literal id as well.
        if str(environment.get('id')) == name:
            return str(environment['id'])

    raise Exception(f"Arcane environment with name '{name}' not found.")


def GetGitRepository(name):
    """Return the reusable Arcane Git Repository matching ``name`` (or None)."""
    print(f"Fetching Arcane git repositories with name '{name}'...")
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/customize/git-repositories?limit=1000",
        headers=HEADERS,
        timeout=10
    )
    _raise_for_status(response)

    repositories = _parse_json(response, "List git repositories").get('data', [])
    for repository in repositories:
        if repository.get('name') == name:
            return repository
    return None


def EnsureGitRepository():
    """Ensure a Git Repository object exists in Arcane with current credentials.

    - Missing  -> create it with the configured URL + username/token.
    - Existing -> update it so the username/token (and URL) are always current,
      which is what lets Arcane clone a private repo. This matters when the repo
      was created before credentials were set, or when the token is rotated.

    Returns the repository id.
    """
    if not GIT_REPO_URL:
        raise Exception(
            "No git repository URL available. Set ARCANE_GIT_REPO_URL or run in "
            "GitHub Actions so GITHUB_REPOSITORY is defined."
        )

    existing = GetGitRepository(GIT_REPO_NAME)
    if existing:
        print(
            f"Git repository '{GIT_REPO_NAME}' already exists with id '{existing['id']}'; "
            "refreshing its URL and credentials."
        )
        response = requests.put(
            f"{ARCANE_BASE_URL}/api/customize/git-repositories/{existing['id']}",
            headers=JSON_HEADERS,
            json=_GitRepositoryPayload(),
            timeout=30
        )
        _raise_for_status(response)
        return existing['id']

    print(f"Creating Arcane git repository '{GIT_REPO_NAME}' -> {GIT_REPO_URL}")
    response = requests.post(
        f"{ARCANE_BASE_URL}/api/customize/git-repositories",
        headers=JSON_HEADERS,
        json=_GitRepositoryPayload(),
        timeout=30
    )
    _raise_for_status(response)

    repo_id = _parse_json(response, "Create git repository")['data']['id']
    print(f"Successfully created git repository '{GIT_REPO_NAME}' with id '{repo_id}'.")
    return repo_id


def _GitRepositoryPayload():
    """Build the Git Repository create/update body, including clone credentials.

    When ``ARCANE_GIT_TOKEN`` is set the repository is configured for HTTP auth
    with the username/token Arcane uses to clone the private repo (separate
    fields — the token is never embedded in the URL); otherwise it is treated
    as public (``authType: none``).
    """
    payload = {
        "name": GIT_REPO_NAME,
        "url": GIT_REPO_URL,
        "authType": "http" if GIT_TOKEN else "none",
        "enabled": True,
    }
    if GIT_TOKEN:
        payload["username"] = GIT_USERNAME
        payload["token"] = GIT_TOKEN
    return payload


def SyncRepositoriesToEnvironment(env_id):
    """Best-effort push of git repositories/credentials to a remote environment.

    Local environment ``0`` clones directly and needs no sync, so it is skipped.
    Failures are non-fatal: the environment may already have the repository.
    """
    if env_id in ('0', 'local'):
        return

    print(f"Syncing git repositories to environment '{env_id}'...")
    try:
        response = requests.post(
            f"{ARCANE_BASE_URL}/api/environments/{env_id}/sync",
            headers=JSON_HEADERS,
            timeout=30
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Warning: failed to sync repositories to environment '{env_id}': {e}")


def GetGitOpsSync(project_name, env_id):
    """Return the GitOps sync managing ``project_name`` in an environment (or None)."""
    print(f"Fetching Arcane GitOps syncs for project '{project_name}' in environment '{env_id}'...")
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/gitops-syncs?limit=1000",
        headers=HEADERS,
        timeout=10
    )
    _raise_for_status(response)

    syncs = _parse_json(response, "List GitOps syncs").get('data', [])
    for sync in syncs:
        if sync.get('projectName') == project_name or sync.get('name') == project_name:
            return sync
    return None


def _is_disconnected_edge_agent_response(response):
    """Return True only for Arcane's known offline-environment response."""
    if response.status_code != 502:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = ((body.get('data') or {}).get('error') or '').strip().lower()
    return error == 'edge agent is not connected'


def GetAllGitOpsSyncs(env_id, tolerate_disconnected=False):
    """Return every GitOps sync record in an environment (read-only listing).

    During post-deployment migration cleanup, a disconnected obsolete source
    environment cannot answer this read-only listing. That specific Arcane
    response is reported and treated as "cleanup deferred" when explicitly
    requested; every other failure remains fail-closed.
    """
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/gitops-syncs?limit=1000",
        headers=HEADERS,
        timeout=10
    )
    if tolerate_disconnected and _is_disconnected_edge_agent_response(response):
        print(
            f"Warning: obsolete source environment '{env_id}' is disconnected "
            "(Arcane edge agent is not connected); deferring cleanup."
        )
        return []
    _raise_for_status(response)
    return _parse_json(response, "List GitOps syncs").get('data', [])


def CreateGitOpsSync(env_id, repo_id):
    """Create a GitOps sync. Arcane runs the initial sync on creation, which
    creates the project and deploys it automatically."""
    print(f"Creating GitOps sync '{PROJECT_NAME}' in environment '{env_id}'...")
    payload = {
        "name": PROJECT_NAME,
        "repositoryId": repo_id,
        "branch": GIT_BRANCH,
        "composePath": COMPOSE_PATH,
        "targetType": "project",
        "projectName": PROJECT_NAME,
        "autoSync": AUTO_SYNC,
        "syncInterval": SYNC_INTERVAL,
        "syncDirectory": SYNC_FILES,
    }
    response = requests.post(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/gitops-syncs",
        headers=JSON_HEADERS,
        json=payload,
        timeout=120
    )
    _raise_for_status(response)

    sync = _parse_json(response, "Create GitOps sync")['data']
    print(
        f"Successfully created GitOps sync '{PROJECT_NAME}' with id '{sync['id']}' "
        f"(autoSync={AUTO_SYNC}, interval={SYNC_INTERVAL}m). "
        "Arcane performed the initial sync, creating and deploying the project."
    )
    return sync


def TriggerSync(env_id, sync_id):
    """Trigger an immediate sync so the latest commit is pulled and redeployed.

    Only an explicit ``success: true`` is treated as OK; anything else fails.
    """
    print(f"Triggering GitOps sync '{sync_id}' in environment '{env_id}'...")
    response = requests.post(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/gitops-syncs/{sync_id}/sync",
        headers=JSON_HEADERS,
        timeout=600
    )
    _raise_for_status(response)

    result = _parse_json(response, "Trigger GitOps sync").get('data', {})
    if not result.get('success'):
        raise Exception(
            f"GitOps sync did not succeed: "
            f"{result.get('error') or result.get('message') or result}"
        )
    print(f"Sync succeeded. Last commit: {result.get('lastSyncCommit', 'n/a')}")


def GetProject(env_id, project_id):
    """Return the Arcane project details for ``project_id`` in an environment."""
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/projects/{project_id}",
        headers=HEADERS,
        timeout=15
    )
    _raise_for_status(response)
    return _parse_json(response, "Get project").get('data', {})


def ConsumeOperationStream(response, action):
    """Consume Arcane's NDJSON operation stream, failing on an error frame.

    Frames are one of ``{"log": ...}``, ``{"activityId": ...}``,
    ``{"done": true}`` (success) or ``{"error": ...}`` (failure).
    """
    _raise_for_status(response)
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            print(raw_line)
            continue

        if 'log' in message:
            print(message['log'].rstrip('\n'))
        elif 'activityId' in message:
            print(f"Activity started: {message['activityId']}")
        elif message.get('done'):
            print(f"{action} completed.")
            return
        elif 'error' in message:
            raise Exception(f"{action} failed: {message['error']}")

    print(f"{action} stream ended without an explicit completion signal.")


def DeployProject(env_id, project_id):
    """Bring the project up (docker compose up) so its containers start."""
    print(f"Bringing project '{project_id}' up in environment '{env_id}'...")
    response = requests.post(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/projects/{project_id}/up",
        headers=JSON_HEADERS,
        json={"pullPolicy": PULL_POLICY},
        stream=True,
        timeout=600
    )
    ConsumeOperationStream(response, "Deploy")


def GetBuildServices():
    """Service names in the compose file that declare a ``build:`` directive.

    Returns an empty list when the local compose copy cannot be read (the deploy
    itself reports a real problem); a pull-only stack must never fail because of this
    optional detection step.
    """
    try:
        return DockerComposeLabelParser(COMPOSE_FILE).get_build_services()
    except Exception as exc:
        print(f"Could not read '{COMPOSE_FILE}' to detect build services: {exc}")
        return []


def BuildProject(env_id, project_id, services):
    """Build the project's images from the synced source before deploying it.

    Compose only builds an image when it is *missing*. On a re-deploy the image from
    the previous pipeline already exists, so ``up`` reuses it: the pipeline goes green
    while the running container keeps serving the old build. Building explicitly makes
    the deployed image match the commit that triggered the pipeline.
    """
    print(f"Building images for services {services} in project '{project_id}'...")
    response = requests.post(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/projects/{project_id}/build",
        headers=JSON_HEADERS,
        json={"services": list(services), "load": True, "push": False},
        stream=True,
        timeout=BUILD_TIMEOUT
    )
    ConsumeOperationStream(response, "Build")


def VerifyProjectRunning(env_id, project_id):
    """Poll until the project's containers start, else fail.

    By default a deploy passes as soon as at least one container is running
    (health is ignored); set ARCANE_REQUIRE_ALL_RUNNING=true to require every
    service. A project that never starts any container still fails.
    """
    deadline = time.monotonic() + DEPLOY_TIMEOUT
    while True:
        project = GetProject(env_id, project_id)
        status = (project.get('status') or '').lower()
        running = project.get('runningCount', 0)
        total = project.get('serviceCount', 0)
        print(f"Project '{PROJECT_NAME}' status: {status} ({running}/{total} running)")

        if REQUIRE_ALL_RUNNING:
            ok = total > 0 and running >= total
        else:
            ok = running > 0

        if ok:
            print(f"Project '{PROJECT_NAME}' is running ({running}/{total}).")
            return

        if time.monotonic() >= deadline:
            raise Exception(
                f"Project '{PROJECT_NAME}' did not start within "
                f"{DEPLOY_TIMEOUT}s (status='{status}', running={running}/{total})."
            )
        time.sleep(POLL_INTERVAL)


def _AssertSyncSucceeded(sync):
    """Fail unless the sync's last status is success."""
    status = (sync.get('lastSyncStatus') or '').lower()
    if status and status != 'success':
        raise Exception(
            f"GitOps sync '{PROJECT_NAME}' reported status '{status}': "
            f"{sync.get('lastSyncError') or 'no error detail provided'}"
        )


def EnsureGitOpsSyncDeployed(env_id, repo_id):
    """Ensure the project's GitOps sync exists, succeeded, and is running.

    - Missing sync  -> create it (Arcane's initial sync creates and deploys it).
    - Existing sync -> trigger an immediate sync for the latest commit.

    Then the sync is re-read and the run fails unless the sync status is success
    and the deployed project's containers actually come up.
    """
    existing = GetGitOpsSync(PROJECT_NAME, env_id)
    if existing is None:
        CreateGitOpsSync(env_id, repo_id)
    else:
        print(f"GitOps sync '{PROJECT_NAME}' already exists with id '{existing['id']}'.")
        TriggerSync(env_id, existing['id'])

    # Re-read the sync for its final status and the project it is bound to.
    sync = GetGitOpsSync(PROJECT_NAME, env_id)
    if sync is None:
        raise Exception(f"GitOps sync '{PROJECT_NAME}' not found after sync; deployment failed.")

    _AssertSyncSucceeded(sync)

    project_id = sync.get('projectId')
    if not project_id:
        raise Exception(
            f"GitOps sync '{PROJECT_NAME}' has no bound project after sync; deployment failed."
        )
    # A compose `build:` service is only built by Compose when its image is missing,
    # so on a re-deploy the previous image is reused and the pipeline goes green while
    # the container serves old code. Build explicitly first, from the just-synced source.
    build_services = GetBuildServices() if BUILD_PROJECTS else []
    if build_services:
        BuildProject(env_id, project_id, build_services)

    # Arcane's sync only redeploys an already-running project, so explicitly bring
    # it up to start a freshly created (stopped) project's containers.
    DeployProject(env_id, project_id)
    VerifyProjectRunning(env_id, project_id)


# --- Cross-environment migration cleanup ------------------------------------------

def StopProject(env_id, project_id, project_name):
    """Stop a project in an environment (docker compose down, NO volume removal)."""
    print(f"Stopping old project '{project_name}' in environment '{env_id}'...")
    response = requests.post(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/projects/{project_id}/down",
        headers=JSON_HEADERS,
        timeout=300
    )
    _raise_for_status(response)
    print(f"Stop request for project '{project_name}' accepted.")


def VerifyProjectStopped(env_id, project_id, project_name):
    """Poll until the project's runningCount reaches zero, else fail."""
    deadline = time.monotonic() + DEPLOY_TIMEOUT
    while True:
        project = GetProject(env_id, project_id)
        status = (project.get('status') or '').lower()
        running = project.get('runningCount', 0)
        print(f"Project '{project_name}' status: {status} ({running} running)")

        if running == 0:
            print(f"Project '{project_name}' is stopped.")
            return

        if time.monotonic() >= deadline:
            raise Exception(
                f"Old project '{project_name}' (id '{project_id}') in environment "
                f"'{env_id}' did not stop within {DEPLOY_TIMEOUT}s "
                f"(status='{status}', running={running})."
            )
        time.sleep(POLL_INTERVAL)


def DeleteGitOpsSync(env_id, sync_id, project_name):
    """Delete a GitOps sync configuration (removes the old GitOps automation only).

    404 is treated as already deleted (idempotent reruns); any other failure
    raises so the CI job fails loudly with actionable context.
    """
    print(f"Deleting old GitOps sync '{sync_id}' for project '{project_name}' in environment '{env_id}'...")
    response = requests.delete(
        f"{ARCANE_BASE_URL}/api/environments/{env_id}/gitops-syncs/{sync_id}",
        headers=HEADERS,
        timeout=60
    )
    if response.status_code == 404:
        print(f"GitOps sync '{sync_id}' already deleted; nothing to do.")
        return
    _raise_for_status(response)
    print(f"Successfully deleted GitOps sync '{sync_id}'.")


def _canonical_repo_url(url):
    """Normalise a git URL for comparison (case-insensitive host, no .git suffix,
    no trailing slash), so https://…/repo.git and https://…/repo match."""
    if not url:
        return None
    canonical = url.strip().lower()
    if canonical.endswith('/'):
        canonical = canonical.rstrip('/')
    if canonical.endswith('.git'):
        canonical = canonical[:-len('.git')]
    return canonical


def FindOldSourceSyncs(dest_env_id):
    """Find old deployments of this project on other Arcane environments.

    A candidate must match BOTH the project name and the git repository URL
    (git repositories are global objects in Arcane; each sync references one by
    id, so the ids are resolved to canonical URLs before comparing).

    Returns a list of {env_id, sync, repo_url} dicts, EXCLUDING the destination
    environment. Fail-closed: any ambiguity (multiple matching syncs in one
    environment, or a referenced repository that cannot be resolved) raises
    before any destructive call is made.
    """
    candidates = []
    environments = GetAllEnvironments()
    repo_urls = {}  # repo id -> canonical url (global across environments)

    for environment in environments:
        env_id = str(environment.get('id'))
        if env_id == dest_env_id:
            continue

        syncs = GetAllGitOpsSyncs(env_id, tolerate_disconnected=True)
        for sync in syncs:
            # The *deployed* project identity is projectName; the sync 'name' is
            # just a label. Only fall back to 'name' when projectName is absent,
            # so a sync labelled like our project but deploying a different
            # project is never stopped.
            sync_project = sync.get('projectName') or sync.get('name')
            if sync_project != PROJECT_NAME:
                continue

            repo_id = sync.get('repositoryId')
            if not repo_id:
                raise Exception(
                    f"Ambiguous source match: GitOps sync '{sync.get('id')}' for project "
                    f"'{PROJECT_NAME}' in environment '{env_id}' has no repositoryId; "
                    "cannot verify it deploys the same git repository. Failing closed."
                )
            if repo_id not in repo_urls:
                repo_record = GetGitRepositoryById(repo_id)
                if repo_record is None:
                    raise Exception(
                        f"Ambiguous source match: GitOps sync '{sync.get('id')}' in environment "
                        f"'{env_id}' references repository id '{repo_id}' which cannot be "
                        "resolved to a URL. Failing closed."
                    )
                repo_urls[repo_id] = _canonical_repo_url(repo_record.get('url'))

            if repo_urls[repo_id] != _canonical_repo_url(GIT_REPO_URL):
                print(
                    f"Skipping sync '{sync.get('id')}' in environment '{env_id}': same "
                    f"project name but different repository ({repo_urls[repo_id]})."
                )
                continue

            candidates.append({'env_id': env_id, 'sync': sync, 'repo_url': repo_urls[repo_id]})

    # Per-environment uniqueness: two matching syncs in one environment is
    # ambiguous and must not be acted on.
    per_env = {}
    for candidate in candidates:
        per_env.setdefault(candidate['env_id'], []).append(candidate)
    for env_id, matches in per_env.items():
        if len(matches) > 1:
            raise Exception(
                f"Ambiguous source match: {len(matches)} GitOps syncs for project "
                f"'{PROJECT_NAME}' from repository '{GIT_REPO_URL}' found in environment "
                f"'{env_id}'. Failing closed rather than stopping the wrong deployment."
            )

    return candidates


def GetGitRepositoryById(repo_id):
    """Return a single global git repository record by id (or None)."""
    response = requests.get(
        f"{ARCANE_BASE_URL}/api/customize/git-repositories/{repo_id}",
        headers=HEADERS,
        timeout=10
    )
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return _parse_json(response, "Get git repository").get('data')


def CleanupMigratedSources(dest_env_id):
    """After the destination is verified healthy, stop and detach old deployments.

    For each old environment running the same project from the same repository:
    stop the project (down, volumes preserved), verify it stopped, then delete
    only that old GitOps sync. Idempotent: an already-stopped project skips the
    stop call, and an already-deleted sync (404) is fine.
    """
    candidates = FindOldSourceSyncs(dest_env_id)
    if not candidates:
        print("No old deployments of this project found on other environments; nothing to clean up.")
        return

    for candidate in candidates:
        env_id = candidate['env_id']
        sync = candidate['sync']
        sync_id = sync.get('id')
        print(f"Cleaning up old deployment in environment '{env_id}' (sync '{sync_id}').")

        project_id = sync.get('projectId')
        if project_id:
            project = GetProject(env_id, project_id)
            if project.get('runningCount', 0) > 0:
                StopProject(env_id, project_id, PROJECT_NAME)
            else:
                print(f"Old project '{PROJECT_NAME}' in environment '{env_id}' is already stopped.")
            VerifyProjectStopped(env_id, project_id, PROJECT_NAME)
        else:
            print(
                f"Old sync '{sync_id}' in environment '{env_id}' has no bound project; "
                "only removing the GitOps sync."
            )

        DeleteGitOpsSync(env_id, sync_id, PROJECT_NAME)

    print("Old-environment cleanup completed.")


def main():
    parser = DockerComposeLabelParser(COMPOSE_FILE)
    environments = parser.get_arcane_environments()

    print(f"Project Name: {PROJECT_NAME}")
    print(f"Arcane Base URL: {ARCANE_BASE_URL}")
    print(f"Git Repository URL: {GIT_REPO_URL}")
    print(f"Compose path in repo: {COMPOSE_PATH}")
    print(f"Detected environments from labels: {environments}")
    # Presence-only diagnostics (never print secret values).
    print(f"ARCANE_API_KEY present: {bool(ARCANE_API_KEY)}")
    print(f"Git credentials present: {bool(GIT_TOKEN and GIT_USERNAME)}")

    if not ARCANE_API_KEY:
        raise Exception(
            "ARCANE_API_KEY environment variable is not set. It must be a GitHub "
            "Actions secret on this repository (or the organization)."
        )

    if not PROJECT_NAME:
        raise Exception(
            "No project name available. Set SERVICE_NAME or run in GitHub Actions "
            "so GITHUB_REPOSITORY is defined."
        )

    if GIT_TOKEN and not GIT_USERNAME:
        raise Exception(
            "ARCANE_GIT_TOKEN is set but ARCANE_GIT_USERNAME is not. Arcane needs "
            "both separate fields to clone the private repository."
        )

    if len(environments) == 0:
        raise Exception("No Arcane environments found in docker-compose labels.")

    # The Git Repository is a global Arcane resource; create it once and reuse
    # across every target environment.
    repo_id = EnsureGitRepository()

    dest_env_id = None
    for host in environments:
        # Fail closed: the first failing environment aborts the deployment.
        # (The old GitLab script counted errors and continued; it must not.)
        print(f"Processing environment: {host}")
        env_id = GetEnvironmentID(host)
        print(f"Environment ID for '{host}': {env_id}")
        dest_env_id = env_id

        SyncRepositoriesToEnvironment(env_id)
        EnsureGitOpsSyncDeployed(env_id, repo_id)

    # Single-target migration cleanup: with exactly one declared environment,
    # the destination above is now verified healthy, so stop the same project
    # (same name AND same git repository) on any other environment and remove
    # its old GitOps sync. Multi-target projects are never touched.
    if len(environments) == 1:
        try:
            CleanupMigratedSources(dest_env_id)
        except Exception as e:
            print(f"Error cleaning up old deployment of '{PROJECT_NAME}': {e}")
            raise Exception(
                f"Migration cleanup failed for project '{PROJECT_NAME}' "
                f"(destination environment '{environments[0]}' is healthy, but the "
                f"old source could not be cleaned up): {e}"
            )

    print("Arcane GitOps deployment script completed.")


if __name__ == "__main__":
    main()
