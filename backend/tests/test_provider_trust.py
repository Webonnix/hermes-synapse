import pytest
from unittest.mock import patch
from backend.provider_trust import trusted_local_endpoint
from backend.llm_client import _is_local_provider
from backend.llamacpp_client import LlamaCppClient, LlamaCppError


@pytest.mark.parametrize('url', ['https://localhost.evil.example/v1', 'https://evil.example/localhost',
    'https://evil.example/?target=127.0.0.1', 'https://hermes-llama.attacker.example',
    'https://localhost@evil.example', 'https://evil.example/vllm', 'https://127.0.0.1.evil.example',
    'file:///localhost', 'http://localhost:invalid', 'https://8.8.8.8/v1'])
def test_remote_endpoints_cannot_claim_local_privacy_exemption(url):
    assert not trusted_local_endpoint(url)
    assert not _is_local_provider(url)
    with pytest.raises(LlamaCppError):
        LlamaCppClient(url)


@pytest.mark.parametrize('url', ['http://localhost:8080', 'http://127.0.0.1:8080/v1',
    'http://[::1]:8080/v1', 'http://hermes-llama:8080', 'http://llama:8080', 'http://192.168.1.20:8080'])
def test_local_endpoints_remain_available(url):
    assert trusted_local_endpoint(url)


def test_operator_can_explicitly_trust_an_exact_internal_hostname(monkeypatch):
    monkeypatch.setenv('HERMES_TRUSTED_LLM_HOSTS', 'inference.internal')
    assert trusted_local_endpoint('https://inference.internal/v1')
    assert not trusted_local_endpoint('https://inference.internal.evil.example/v1')


def test_legacy_graph_extraction_never_sends_raw_document_to_a_remote_provider(monkeypatch):
    from backend.memory import SQLiteGraphMemoryEngine
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fake-key')
    monkeypatch.setenv('LLM_API_BASE', 'https://provider.invalid/v1')
    with patch('httpx.Client') as client:
        SQLiteGraphMemoryEngine()._extract_graph_elements('Private Document with Customer Data')
    client.assert_not_called()


def test_browser_use_remote_provider_is_blocked_before_loading_an_sdk(monkeypatch):
    from browser_runner.server import _build_llm
    monkeypatch.setenv('LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('LLM_MODEL', 'external-model')
    with pytest.raises(RuntimeError, match='external screenshots/DOM are blocked'):
        _build_llm()
