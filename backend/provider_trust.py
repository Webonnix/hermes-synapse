"""The privacy boundary is an exact host policy, never a provider's label."""
import ipaddress
import os
from urllib.parse import urlsplit

_HOSTS = frozenset({'localhost', 'host.docker.internal', 'hermes-llama', 'llama',
                    'lmstudio', 'lm-studio', 'localai', 'vllm'})
_NETWORKS = tuple(ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def trusted_local_endpoint(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password:
            return False
        parsed.port  # Reject malformed ports before dispatch.
        extra = {part.strip().lower().rstrip('.') for part in os.getenv('HERMES_TRUSTED_LLM_HOSTS', '').split(',') if part.strip()}
        if host in _HOSTS | extra:
            return True
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_unspecified or any(address in net for net in _NETWORKS)
    except ValueError:
        return False
