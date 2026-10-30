"""Read-only deployment checks. Never prints rendered Compose or credentials."""
from __future__ import annotations
import json
import os
import subprocess
import sys


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        # Compose stderr may contain interpolated configuration values.
        raise RuntimeError('Required command failed: ' + args[0])
    return result.stdout


def check(config, info, version, volume_names):
    errors = []
    services = config.get('services', {})
    if 'runsc' not in info.get('Runtimes', {}):
        errors.append('gVisor/runsc is not installed; no runc fallback is allowed')
    try:
        if tuple(map(int, version['ApiVersion'].split('.'))) < (1, 45):
            errors.append('Docker Engine API 1.45+ is required for volume subpaths')
    except (KeyError, ValueError):
        errors.append('Cannot verify Docker Engine API version')
    backend = services.get('backend', {})
    if backend.get('user') != '10001:10001' or not backend.get('read_only'):
        errors.append('Backend must run as UID 10001 with a read-only root')
    if not {'ALL'} <= set(backend.get('cap_drop', [])):
        errors.append('Backend capabilities must all be dropped')
    if backend.get('environment', {}).get('DOCKER_HOST') != 'tcp://sandbox-launcher:2375':
        errors.append('Backend must use the template launcher')
    for name, service in services.items():
        for mount in service.get('volumes', []):
            if 'docker.sock' in mount.get('source', '') and name != 'sandbox-launcher':
                errors.append('Only sandbox-launcher may mount docker.sock')
        for port in service.get('ports', []):
            if name != 'frontend' and port.get('host_ip') not in ('127.0.0.1', '::1'):
                errors.append('Infrastructure port exposed: ' + name)
    for logical in ('hermes_data', 'valkey_data', 'qdrant_data', 'omniroute_data'):
        volume = config.get('volumes', {}).get(logical, {})
        if not volume.get('external') or volume.get('name') not in volume_names:
            errors.append('Explicit existing data volume required: ' + logical)
    origins = backend.get('environment', {}).get('HERMES_ALLOWED_ORIGINS', '')
    if '*' in origins:
        errors.append('Wildcard credential origin refused')
    return errors


def main():
    # Docker/Compose binaries may be specified by the operator; no shell eval.
    docker = os.getenv('HERMES_DOCKER_BIN', 'docker')
    compose = os.getenv('HERMES_COMPOSE_BIN')
    compose_args = [compose] if compose else [docker, 'compose']
    config = json.loads(command(*compose_args, 'config', '--format', 'json'))
    info = json.loads(command(docker, 'info', '--format', '{{json .}}'))
    version = json.loads(command(docker, 'version', '--format', '{{json .Server}}'))
    names = set(command(docker, 'volume', 'ls', '--format', '{{.Name}}').splitlines())
    errors = check(config, info, version, names)
    for error in errors:
        print('BLOCKED: ' + error)
    if not errors:
        print('Preflight passed. Migration, backup restore and staging acceptance still required.')
    return bool(errors)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError):
        print('BLOCKED: prerequisites/configuration unavailable; secrets have not been printed')
        raise SystemExit(1)
