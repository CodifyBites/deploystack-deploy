import yaml
import os
from typing import Dict, List, Optional

class DockerComposeLabelParser:
    def __init__(self, compose_file_path: str = "docker-compose.yaml"):
        self.compose_file_path = compose_file_path
        self.compose_data = self._load_compose_file()
    
    def _load_compose_file(self) -> dict:
        """Load and parse docker-compose.yaml file"""
        if not os.path.exists(self.compose_file_path):
            raise FileNotFoundError(f"Docker compose file not found: {self.compose_file_path}")
        
        with open(self.compose_file_path, 'r') as file:
            return yaml.safe_load(file)
    
    def get_portainer_hosts(self) -> List[str]:
        """Extract portainer hosts from labels"""
        hosts = []
        for service_name, service_config in self.compose_data.get('services', {}).items():
            labels = service_config.get('labels', [])
            for label in labels:
                if isinstance(label, str) and label.startswith('portainer.server.hosts='):
                    hosts_str = label.split('=', 1)[1]
                    hosts.extend([host.strip() for host in hosts_str.split(',')])
        return list(set(hosts))  # Remove duplicates

    def get_arcane_environments(self) -> List[str]:
        """Extract Arcane target environments from labels.

        Reads the ``arcane.server.hosts`` label (comma separated list of Arcane
        environment names or ids). Each value maps to an Arcane environment
        (``0``/``local`` is the built-in local Docker host).
        """
        environments = []
        for service_name, service_config in self.compose_data.get('services', {}).items():
            labels = service_config.get('labels', [])
            for label in labels:
                if isinstance(label, str) and label.startswith('arcane.server.hosts='):
                    hosts_str = label.split('=', 1)[1]
                    environments.extend([host.strip() for host in hosts_str.split(',') if host.strip()])
        return list(set(environments))  # Remove duplicates

    def get_build_services(self) -> List[str]:
        """Names of services that declare a ``build:`` directive.

        A compose ``build:`` can be a bare context string, a mapping with
        ``context``/``dockerfile``, or a list of contexts; any non-empty form counts.
        Used to decide whether a deploy needs an explicit image build first: Compose
        only builds when the image is *missing*, so without this a re-deploy of an
        already-built stack silently reuses the previous image.
        """
        services = []
        for service_name, service_config in self.compose_data.get('services', {}).items():
            if not isinstance(service_config, dict):
                continue
            if service_config.get('build'):
                services.append(service_name)
        return sorted(services)

    def get_pangolin_config(self) -> List[Dict[str, any]]:
        """Extract pangolin configuration from labels"""
        configs = []
        
        for service_name, service_config in self.compose_data.get('services', {}).items():
            config = {
                'enabled': False,
                'protected': False,
                'domain': None,
                'subdomain': None,
                'rules': [],
                'services': [],
                'roles': []
            }
                
            labels = service_config.get('labels', [])
            for label in labels:
                if not isinstance(label, str):
                    continue
                    
                if label.startswith('pangolin.enable='):
                    config['enabled'] = label.split('=', 1)[1].lower() == 'true'
                elif label.startswith('pangolin.protected='):
                    config['protected'] = label.split('=', 1)[1].lower() == 'true'
                elif label.startswith('pangolin.domain='):
                    config['domain'] = label.split('=', 1)[1]
                elif label.startswith('pangolin.subdomain='):
                    subdomain = label.split('=', 1)[1].strip()
                    # Empty label (apex/domain-root resource) normalises to None.
                    config['subdomain'] = subdomain if subdomain else None
                elif label.startswith('pangolin.rules='):
                    rules_str = label.split('=', 1)[1]
                    config['rules'] = [rule.strip() for rule in rules_str.split(',')]
                elif label.startswith('pangolin.services='):
                    services_str = label.split('=', 1)[1]
                    config['services'] = [service.strip() for service in services_str.split(',')]
                elif label.startswith('pangolin.roles='):
                    roles_str = label.split('=', 1)[1]
                    config['roles'] = [role.strip() for role in roles_str.split(',')]
            
            if len(labels) > 0:
                configs.append(config)
        
        return configs
    
    def validate_pangolin_config(self, config: Dict[str, any]) -> bool:
        """Validate pangolin configuration"""
        if not config['enabled']:
            return True
            
        # 'subdomain' is optional: an absent/empty value means the resource is
        # served at the domain apex (bare domain, no subdomain).
        config['subdomain'] = (str(config.get('subdomain')).strip()
                               if config.get('subdomain') is not None else '') or None

        required_fields = ['domain', 'services']
        for field in required_fields:
            if not config.get(field):
                raise ValueError(f"Missing required pangolin configuration: {field}")
        
        # Validate service format (protocol:service_name:port)
        for service in config['services']:
            parts = service.split(':')
            if len(parts) != 3:
                raise ValueError(f"Invalid service format: {service}. Expected format: protocol:service_name:port")
            
            protocol, service_name, port = parts
            if protocol not in ['http', 'https', 'tcp', 'udp']:
                raise ValueError(f"Invalid protocol: {protocol}")
            
            try:
                int(port)
            except ValueError:
                raise ValueError(f"Invalid port number: {port}")
        
        # Validate rule format (ACTION|MATCH|VALUE)
        for rule in config['rules']:
            parts = rule.split('|')
            if len(parts) != 3:
                raise ValueError(f"Invalid rule format: {rule}. Expected format: ACTION|MATCH|VALUE")
        
        return True
