from ops.security_preflight import check


def test_preflight_refuses_unsafe_runtime_and_missing_data_volumes():
    errors = check({'services': {'backend': {'user': '0', 'ports': [{'host_ip': '0.0.0.0'}],
                                            'volumes': [{'source': '/var/run/docker.sock'}]}}},
                   {'Runtimes': {'runc': {}}}, {'ApiVersion': '1.44'}, set())
    assert any('gVisor' in e for e in errors)
    assert any('subpaths' in e for e in errors)
    assert any('read-only' in e for e in errors)
    assert any('Infrastructure port' in e for e in errors)
    assert any('Only sandbox-launcher' in e for e in errors)
    assert len([e for e in errors if 'data volume' in e]) == 4


def test_preflight_accepts_explicit_existing_volumes_and_constrained_backend():
    volumes = {key: {'external': True, 'name': key} for key in
               ('hermes_data', 'valkey_data', 'qdrant_data', 'omniroute_data')}
    config = {'volumes': volumes, 'services': {'backend': {'user': '10001:10001', 'read_only': True,
              'cap_drop': ['ALL'], 'environment': {'DOCKER_HOST': 'tcp://sandbox-launcher:2375'},
              'ports': [{'host_ip': '127.0.0.1'}]}}}
    assert check(config, {'Runtimes': {'runsc': {}}}, {'ApiVersion': '1.54'}, set(volumes)) == []
