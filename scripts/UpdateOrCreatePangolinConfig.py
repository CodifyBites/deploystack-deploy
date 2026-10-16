"""Pangolin reconciliation for GitHub Actions.

Ported from the shared GitLab pipeline (workflows-pangolin-portainer-shared,
deploy/UpdateOrCreatePangolinConfig.py) with these adaptations:

- The Pangolin API base URL comes from PANGOLIN_API_BASE_URL (repository
  variable) with the verified production default.
- The compose file comes from COMPOSE_FILE_PATH, defaulting to
  ./docker-compose.yaml at the repository root (the script lives in scripts/).

Fail-closed behaviour (differences from the old script, which could swallow
errors):

- Missing PANGOLIN_ORG_KEY / PANGOLIN_ORG_ID fail immediately.
- A resource whose reconciliation raises aborts the run with the full error
  (the old script printed and continued).
- pangolin.enable=false is reported explicitly as intentionally skipped.
"""

import requests
import os
import dataclasses
from DockerComposeLabelParser import DockerComposeLabelParser

PANGOLIN_ORG_KEY = os.getenv('PANGOLIN_ORG_KEY')
PANGOLIN_ORG_ID = os.getenv('PANGOLIN_ORG_ID')
API_PANGOLIN_URL = os.getenv(
    'PANGOLIN_API_BASE_URL', 'https://api-pangolin.prod.codifybites.com/v1'
).rstrip('/')

# Compose file in THIS checkout (workflow runs from the repo root).
COMPOSE_FILE = os.getenv('COMPOSE_FILE_PATH', 'docker-compose.yaml')

HEADERS = {
    "Authorization": f"Bearer {PANGOLIN_ORG_KEY}",
    "Content-Type": "application/json"
}

@dataclasses.dataclass
class PangolinTarget:
    id: int = None
    protocol: str = None
    service_name: str = None
    service_port: int = None

@dataclasses.dataclass
class PangolinRule:
    id: int = None
    action: str = None
    match: str = None
    value: str = None
    priority: int = 0
    enabled: bool = True

def ProcessRequest(url, method="GET", headers=None, json=None):
    if method == "GET":
        response = requests.get(url, headers=headers, timeout=10)
    elif method == "POST":
        response = requests.post(url, headers=headers, json=json, timeout=10)
    elif method == "PUT":
        response = requests.put(url, headers=headers, json=json, timeout=10)
    elif method == "DELETE":
        response = requests.delete(url, headers=headers, timeout=10)
    else:
        raise ValueError("Unsupported HTTP method.")

    try:
        response.raise_for_status()
        if response.json().get('error'):
            raise Exception(f"API Error: {response.json().get('message')}")
    except requests.exceptions.HTTPError as e:
        print("Response content:", response.text)
        print("HTTP error occurred:", e)
        raise
    return response

def GetOrgByID(org_id, custom=None):
    url = f"{API_PANGOLIN_URL}/org/{org_id}"
    if custom:
        url = f"{API_PANGOLIN_URL}/org/{org_id}/{custom}"

    print(f"Fetching Pangolin organization with ID '{org_id}'...")
    response = ProcessRequest(
        url,
        method="GET",
        headers=HEADERS
    )
    org = response.json()
    if not org:
        raise Exception(f"No organization found with ID '{org_id}'.")

    if 'data' not in org:
        raise Exception(f"No organization data available for ID '{org_id}'.")

    return org['data']


def GetAllDomains(org_id):
    print("Fetching domain list...")
    domains = GetOrgByID(
        org_id,
        "domains"
    )

    if not domains:
        raise Exception(f"No domain exist in {org_id}.")

    if len(domains['domains']) == 0:
        raise Exception(f"No domains available in organization '{org_id}'.")
    return domains['domains']


MAX_RESOURCE_PAGES = 50  # hard safety cap; 50 * 200 = 10,000 resources


def GetAllResources(org_id):
    print(f"Fetching all Pangolin resources from organization '{org_id}'...")

    # The Pangolin API paginates GET /org/{org_id}/resources (defaults:
    # page=1, pageSize=20). Requesting every page is mandatory — reading only
    # the first makes later resources invisible to GetSubdomainID, which then
    # takes the create branch and duplicates them. The API's `query` parameter
    # does NOT filter, so pagination is the only working approach.
    all_resources = []
    page = 1
    total = None
    while page <= MAX_RESOURCE_PAGES:
        resources = GetOrgByID(
            org_id,
            f"resources?page={page}&pageSize=200"
        )
        if not resources:
            raise Exception(f"No resources found in organization '{org_id}'.")

        page_resources = resources.get('resources', [])
        pagination = resources.get('pagination', {}) or {}
        total = pagination.get('total', total)
        all_resources.extend(page_resources)

        # Stop when a page comes back empty/short or everything is collected.
        if not page_resources or len(page_resources) < 200:
            break
        if total is not None and len(all_resources) >= total:
            break

        page += 1
    else:
        raise Exception(
            f"Exceeded {MAX_RESOURCE_PAGES} pages while fetching resources "
            f"from organization '{org_id}'."
        )

    if total is not None and len(all_resources) < total:
        raise Exception(
            f"Fetched {len(all_resources)} of {total} resources from "
            f"organization '{org_id}'; refusing to continue with a partial list."
        )

    if len(all_resources) == 0:
        raise Exception(f"No resources available in organization '{org_id}'.")

    return all_resources


def GetResourceByID(resource_id, custom=None):
    url = f"{API_PANGOLIN_URL}/resource/{resource_id}"
    if custom:
        url = f"{API_PANGOLIN_URL}/resource/{resource_id}/{custom}"

    print(f"Fetching Pangolin resource with ID '{resource_id}'...")
    response = ProcessRequest(
        url,
        method="GET",
        headers=HEADERS
    )
    resource = response.json()
    if not resource:
        raise Exception(f"No resource found with ID '{resource_id}'.")

    if 'data' not in resource:
        raise Exception(f"No resource data available for ID '{resource_id}'.")

    return resource['data']

def GetAllTargets(resource_id):
    print(f"Fetching all targets for resource ID '{resource_id}'...")
    targets = GetResourceByID(
        resource_id,
        "targets"
    )

    if not targets:
        raise Exception(f"No targets found for resource ID '{resource_id}'.")

    if len(targets['targets']) == 0:
        print(f"No targets available for resource ID '{resource_id}'.")
        return []
    return targets['targets']


def GetAllSites(org_id):
    print(f"Fetching all Pangolin sites from organization '{org_id}'...")
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/org/{org_id}/sites",
        method="GET",
        headers=HEADERS
    )
    sites = response.json()
    if not sites:
        raise Exception(f"No sites found in organization '{org_id}'.")

    if 'data' not in sites or len(sites['data']['sites']) == 0:
        raise Exception(f"No sites available in organization '{org_id}'.")

    return sites['data']['sites']


def GetDomainID(domain_name, org_id):
    print(f"Getting domain ID for domain '{domain_name}'...")
    domains = GetAllDomains(org_id)
    for domain in domains:
        if domain['baseDomain'] == domain_name:
            return domain['domainId']

    raise Exception(f"Domain '{domain_name}' not found in organization '{org_id}'.")


def GetSiteID(site_name, org_id):
    print(f"Getting site ID for site '{site_name}'...")
    resources = GetAllSites(org_id)
    for resource in resources:
        if resource['orgName'].lower() == site_name.lower():
            return resource["siteId"]
    return None


def GetTargetFromResource(resource_id, service_name, service_port):
    print(f"Getting target for service '{service_name}' on port '{service_port}'...")
    targets = GetAllTargets(resource_id)
    for target in targets:
        if target['ip'] == service_name and target['port'] == service_port:
            return target
    return None


def GetSubdomainID(subdomain_name, domain_id, org_id):
    print(f"Getting subdomain ID for subdomain '{subdomain_name}'...")
    resources = GetAllResources(org_id)
    # An empty subdomain means the apex resource: its fullDomain is the bare
    # domain, never ".<domain>".
    expected_fqdn = DOMAIN if not subdomain_name else f"{subdomain_name}.{DOMAIN}"
    for resource in resources:
        if (resource['domainId'] == domain_id and resource['fullDomain'] == expected_fqdn):
            print(f"Subdomain '{subdomain_name}' already exists with ID: {resource['niceId']}")
            return resource
    return None


def GetRulesFromResource(resource_id):
    print(f"Getting rules for resource ID '{resource_id}'...")
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/resource/{resource_id}/rules",
        method="GET",
        headers=HEADERS
    )
    rules = response.json()
    if not rules:
        raise Exception(f"No rules found for resource ID '{resource_id}'.")

    if 'data' not in rules or len(rules['data']['rules']) == 0:
        print(f"No rules available for resource ID '{resource_id}'.")
        return []

    return rules['data']['rules']


def GetResourceConfig(resource_id):
    print(f"Fetching Pangolin resource config for resource ID '{resource_id}'...")
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/resource/{resource_id}",
        method="GET",
        headers=HEADERS
    )

    print("Successfully fetched Pangolin resource config.")
    return response.json()['data']


def CreateResourceConfig(subdomain_id, domain_id, org_id):
    if SUBDOMAIN:
        # Non-empty subdomain: payload unchanged from the original behaviour.
        payload = {
            "name": SUBDOMAIN,
            "subdomain": subdomain_id,
            "http": True,
            "protocol": "tcp",
            "domainId": domain_id,
            "stickySession": False
        }
    else:
        # Apex resource: no subdomain — Pangolin expects `subdomain: null`,
        # which yields a resource whose fullDomain is the bare domain.
        payload = {
            "name": DOMAIN,
            "subdomain": None,
            "http": True,
            "protocol": "tcp",
            "domainId": domain_id,
            "stickySession": False
        }

    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/org/{org_id}/resource",
        method="PUT",
        headers=HEADERS,
        json=payload
    )

    print("Successfully created Pangolin resource config.")
    return response.json()


def AddResourceRule(resource_id, rule: PangolinRule):
    print(f"Adding rules to resource ID '{resource_id}'...")
    payload = {
        "action": rule.action,
        "match": rule.match,
        "value": rule.value,
        "priority": rule.priority,
        "enabled": rule.enabled
    }

    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/resource/{resource_id}/rule",
        method="PUT",
        headers=HEADERS,
        json=payload
    )

    print("Successfully added Pangolin resource rule.")
    return response.json()['data']


def AddTargetToResource(resource_id, service_protocol, service_name, service_port, org_id):
    print(f"Adding target for service '{service_name}' on port '{service_port}' to resource ID '{resource_id}'...")
    site_id = GetSiteID(PANGOLIN_ORG_ID, org_id)
    if site_id is None:
        raise Exception(
            f"Pangolin site '{PANGOLIN_ORG_ID}' not found in organization "
            f"'{org_id}'; cannot attach target to resource '{resource_id}'."
        )

    payload = {
        "siteId": site_id,
        "ip": service_name,
        "method": service_protocol,
        "port": service_port,
        "enabled": True,
        "hcEnabled": False,
        "path": "/",
        "pathMatchType": "prefix",
        "priority": 100
    }
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/resource/{resource_id}/target",
        method="PUT",
        headers=HEADERS,
        json=payload
    )
    response_json = response.json()
    return response_json['data']

def UpdateResource(resource_id, value=None, custom=None):
    if not value:
        print(f"Nothing to update to {resource_id} as the value is None")

    url = f"{API_PANGOLIN_URL}/resource/{resource_id}"
    if custom:
        url = f"{API_PANGOLIN_URL}/resource/{resource_id}/{custom}"

    response = ProcessRequest(
        url,
        method="POST",
        headers=HEADERS,
        json=value
    )
    print("Successfully updated Pangolin resource.")
    return response.json()

def DeleteResource(resource_id):
    print(f"Deleting Pangolin resource with ID '{resource_id}'...")
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/resource/{resource_id}",
        method="DELETE",
        headers=HEADERS
    )
    print("Successfully deleted Pangolin resource.")
    return response.json()


def DeleteTargetFromResource(resource_id, target: PangolinTarget):
    print(f"Deleting target ID '{target.id}' from resource ID '{resource_id}'...")
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/target/{target.id}",
        method="DELETE",
        headers=HEADERS
    )
    print("Successfully deleted target from Pangolin resource.")
    return response.json()

def DeleteRuleFromResource(resource_id, rule: PangolinRule):
    print(f"Deleting rule '{rule.action}|{rule.match}|{rule.value}' from resource ID '{resource_id}'...")
    response = ProcessRequest(
        f"{API_PANGOLIN_URL}/resource/{resource_id}/rule/{rule.id}",
        method="DELETE",
        headers=HEADERS,
    )
    print("Successfully deleted rule from Pangolin resource.")
    return response.json()['data']


def UpdateTargetsInResource(resource_id):
    print(f"Updating targets in resource ID '{resource_id}'...")

    existing_targets = GetAllTargets(resource_id)
    new_targets = []
    old_targets = []
    stay_targets = []
    remove_targets = []

    for service in SERVICES:
        exist = False
        protocol, service_name, service_port = service.split(':')
        for target in existing_targets:
            if target['method'] == protocol and target['ip'] == service_name and target['port'] == int(service_port):
                exist = True
                old_targets.append(PangolinTarget(target['targetId'], target['method'], target['ip'], target['port']))
                stay_targets.append(PangolinTarget(target['targetId'], target['method'], target['ip'], target['port']))
                break

        if not exist:
            new_targets.append(PangolinTarget(None, protocol, service_name, int(service_port)))
            stay_targets.append(PangolinTarget(None, protocol, service_name, int(service_port)))

    # Get those target need to be remove as it does not exist in the new target list or old target list
    for target in existing_targets:
        pangolin_target = PangolinTarget(target['targetId'], target['method'], target['ip'], target['port'])
        if pangolin_target not in stay_targets and pangolin_target not in new_targets:
            remove_targets.append(pangolin_target)

    print(f"Removing {len(remove_targets)} targets...")
    for target in remove_targets:
        DeleteTargetFromResource(resource_id, target)

    print(f"Adding {len(new_targets)} new targets...")
    for target in new_targets:
        AddTargetToResource(resource_id, target.protocol, target.service_name, target.service_port, PANGOLIN_ORG_ID)

    print(f"Successfully updated {len(stay_targets)} targets to {resource_id}.")


def UpdateResourceRules(resource_id):
    print(f"Ensuring the resource have rules enabled...")
    resource_config = GetResourceConfig(resource_id)
    print("Resource config:", resource_config)
    if not resource_config['applyRules']:
        print(f"Enabling rules for resource ID '{resource_id}'...")
        payload = {
            "applyRules": True
        }
        ProcessRequest(
            f"{API_PANGOLIN_URL}/resource/{resource_id}",
            method="POST",
            headers=HEADERS,
            json=payload
        )
        print(f"Successfully enabled rules for resource ID '{resource_id}'.")

    print(f"Updating rules in resource ID '{resource_id}'...")
    all_rules = GetRulesFromResource(resource_id)
    old_rules = []
    new_rules = []
    stay_rules = []
    remove_rules = []
    priority_list = [rule['priority'] for rule in all_rules]
    priority_list.sort()
    priority_counter = 1

    for rule_str in RULES:
        action, match, value = rule_str.split('|')
        exist = False
        for rule in all_rules:
            if rule['action'] == action and rule['match'] == match and rule['value'] == value:
                exist = True
                old_rules.append(PangolinRule(id=rule['ruleId'], action=rule['action'], match=rule['match'], value=rule['value'], priority=rule['priority'], enabled=rule['enabled']))
                stay_rules.append(PangolinRule(id=rule['ruleId'], action=rule['action'], match=rule['match'], value=rule['value'], priority=rule['priority'], enabled=rule['enabled']))
                break
        if not exist:
            # Create priority that does not exist with the list
            while priority_counter in priority_list:
                priority_counter += 1
            priority = priority_counter
            new_rules.append(PangolinRule(action=action, match=match, value=value, priority=priority, enabled=True))
            stay_rules.append(PangolinRule(action=action, match=match, value=value, priority=priority, enabled=True))
            priority_list.append(priority)
            priority_counter += 1

    # Get those rules need to be remove as it does not exist in the new rule list or old rule list
    for rule in all_rules:
        pangolin_rule = PangolinRule(id=rule['ruleId'], action=rule['action'], match=rule['match'], value=rule['value'], priority=rule['priority'], enabled=rule['enabled'])
        if pangolin_rule not in stay_rules and pangolin_rule not in new_rules:
            remove_rules.append(pangolin_rule)

    print(f"Removing {len(remove_rules)} rules...")
    for rule in remove_rules:
        DeleteRuleFromResource(resource_id, rule)

    print(f"Adding {len(new_rules)} new rules...")
    for rule in new_rules:
        AddResourceRule(resource_id, rule)

    print(f"Successfully updated rules to {resource_id}.")

def UpdateResourceProtection(resource_id, protected=True):
    current_config = GetResourceConfig(resource_id)
    if current_config["sso"] == protected:
        print("No update required.")
    else:
        UpdateResource(
            resource_id,
            {
                "sso": protected
            }
        )

def UpdateResourceRoles(resource_id, org_id, roles):
    current_config = GetResourceConfig(resource_id)
    if 'roles' not in current_config or set(current_config['roles']) != set(roles):
        print(f"Updating resource roles to {roles}...")
        org_roles = GetOrgByID(
            org_id,
            "roles"
        )
        print("Resource roles:", org_roles)
        if len(org_roles["roles"]) == 0:
            print("No existing roles found. No roles to update")
            return

        roles_ids = [role['roleId'] for role in org_roles['roles'] if role['name'].lower() in [r.lower() for r in roles]]
        print(f"Updating resource roles to IDs {roles_ids}...")

        UpdateResource(
            resource_id,
            {
                "roleIds": roles_ids
            },
            custom="roles"
        )
    else:
        print("No update required for resource roles.")

def _reconcile_config(pangolin_config):
    """Reconcile a single validated pangolin config (module globals pattern
    retained from the original script)."""
    global PROTECTED, DOMAIN, SUBDOMAIN, SERVICES, RULES, ROLES

    PROTECTED = pangolin_config['protected']
    DOMAIN = pangolin_config['domain']
    SUBDOMAIN = pangolin_config['subdomain']
    SERVICES = pangolin_config['services']
    RULES = pangolin_config['rules']
    ROLES = pangolin_config['roles']

    print("Pangolin Configuration:")
    print(f"  Domain: {DOMAIN}")
    print(f"  Subdomain: {SUBDOMAIN}")
    print(f"  Protected: {PROTECTED}")
    print(f"  Services: {SERVICES}")
    print(f"  Rules: {RULES}")
    print(f"  Roles: {ROLES}")

    domain_id = GetDomainID(DOMAIN, PANGOLIN_ORG_ID)
    subdomain = GetSubdomainID(SUBDOMAIN, domain_id, PANGOLIN_ORG_ID)
    if not subdomain:
        subdomain = CreateResourceConfig(SUBDOMAIN, domain_id, PANGOLIN_ORG_ID)

    subdomain = subdomain['data'] if 'data' in subdomain else subdomain
    resource_id = subdomain['resourceId']
    # Add target to the resource
    UpdateTargetsInResource(resource_id)
    UpdateResourceRules(resource_id)
    UpdateResourceProtection(resource_id, PROTECTED)
    UpdateResourceRoles(resource_id, PANGOLIN_ORG_ID, ROLES)


def main():
    if not PANGOLIN_ORG_KEY:
        raise Exception(
            "PANGOLIN_ORG_KEY environment variable is not set. It must be a "
            "GitHub Actions secret on this repository (or the organization)."
        )
    if not PANGOLIN_ORG_ID:
        raise Exception(
            "PANGOLIN_ORG_ID environment variable is not set. It must be a "
            "GitHub Actions secret/variable on this repository (or the organization)."
        )

    # Parse docker-compose labels
    parser = DockerComposeLabelParser(COMPOSE_FILE)
    pangolin_configs = parser.get_pangolin_config()

    # Validate every config up front: one invalid label set fails the whole
    # reconciliation before any API mutation happens.
    enabled_configs = []
    for pangolin_config in pangolin_configs:
        parser.validate_pangolin_config(pangolin_config)
        if pangolin_config['enabled']:
            enabled_configs.append(pangolin_config)
        else:
            print(
                "Pangolin is intentionally skipped for this service: "
                "pangolin.enable=false."
            )

    if not enabled_configs:
        print(
            "No service enables Pangolin (pangolin.enable=true) in this stack; "
            "Pangolin reconciliation intentionally skipped."
        )
        return

    errors = []
    for pangolin_config in enabled_configs:
        try:
            _reconcile_config(pangolin_config)
        except Exception as e:
            # Fail closed: record the failure and abort with it after the
            # loop (subsequent configs are still attempted so a multi-route
            # stack converges on rerun, but the job never goes green on error).
            print(f"Pangolin reconciliation error for config {pangolin_config}: {e}")
            errors.append(e)

    if errors:
        raise Exception(
            f"Pangolin reconciliation failed for {len(errors)} resource(s): "
            + "; ".join(str(e) for e in errors)
        )

    print("Pangolin configuration script completed.")


if __name__ == "__main__":
    main()
