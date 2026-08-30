"""End-to-end pipeline execution against mock providers (§127, §129).

Everything here runs offline: mock providers (§128) stand in for every paid
API, so the retry, fallback, approval, cancellation and idempotency paths are
exercised without a cent of spend and without a network.
"""

import asyncio

import pytest

from backend import database
from backend.content_factory import (
    agents,
    content as content_store,
    db,
    execution,
    pipelines,
    schema,
    storage as storage_module,
)
from backend.content_factory.domain import NodeStatus, RunStatus
from backend.content_factory.providers import mocks, registry
from backend.content_factory.providers.base import ProviderCategory


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cf.db"))
    database._set_backend_for_tests(database.SQLiteBackend())
    database.init_db()
    schema.ensure_schema(force=True)
    agents.seed_agents()

    storage_module.set_storage_for_tests(storage_module.LocalStorage(str(tmp_path / "assets")))

    live = registry.ProviderRegistry()
    live.register(mocks.MockLLMProvider(), default_for=ProviderCategory.LLM)
    live.register(mocks.MockImageProvider(), default_for=ProviderCategory.IMAGE)
    live.register(mocks.MockVoiceProvider(), default_for=ProviderCategory.VOICE)
    live.register(mocks.MockVideoProvider(), default_for=ProviderCategory.VIDEO)
    live.register(mocks.MockPublishingProvider(), default_for=ProviderCategory.PUBLISHING)
    registry.set_registry_for_tests(live)

    yield live

    registry.set_registry_for_tests(None)
    storage_module.set_storage_for_tests(None)
    database._set_backend_for_tests(None)


def _content_pipeline(**overrides):
    """Trigger → script → storyboard → (voice ‖ images) → merge."""
    nodes = [
        {"id": "trigger", "type": "manual_trigger", "name": "Start"},
        {
            "id": "script",
            "type": "script",
            "config": {"agentId": "cf_script", "duration": 3, "language": "ru"},
            "inputMapping": {"topic": "{{ trigger.topic }}"},
        },
        {
            "id": "storyboard",
            "type": "storyboard",
            "config": {"agentId": "cf_storyboard", "duration": 3},
            "inputMapping": {"script": "{{ nodes.script.output }}"},
        },
        {
            "id": "voice",
            "type": "voice_generation",
            "inputMapping": {"scenes": "{{ nodes.storyboard.output.scenes }}"},
        },
        {
            "id": "images",
            "type": "image_generation",
            "inputMapping": {"scenes": "{{ nodes.storyboard.output.scenes }}"},
        },
        {"id": "collect", "type": "merge"},
    ]
    edges = [
        {"source": "trigger", "target": "script"},
        {"source": "script", "target": "storyboard"},
        {"source": "storyboard", "target": "voice"},
        {"source": "storyboard", "target": "images"},
        {"source": "voice", "target": "collect"},
        {"source": "images", "target": "collect"},
    ]
    for node in nodes:
        node.update(overrides.get(node["id"], {}))
    return nodes, edges


def _activate(nodes, edges, **kwargs):
    pipeline = pipelines.create_pipeline("Test pipeline", nodes=nodes, edges=edges, **kwargs)
    pipelines.set_status(pipeline["id"], "ACTIVE")
    return pipelines.get_pipeline(pipeline["id"])


def test_pipeline_runs_to_completion(factory):
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.COMPLETED
    statuses = {row["node_id"]: row["status"] for row in execution.get_node_runs(run["id"])}
    assert set(statuses.values()) == {NodeStatus.COMPLETED}
    assert finished["progress"] == 100


def test_parallel_branches_both_produce_assets(factory):
    """§19: voice and images fan out from the storyboard and both run."""
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})
    asyncio.run(execution.drive_run(run["id"]))

    assets = db.query("SELECT type FROM cf_assets WHERE pipeline_run_id = ?", (run["id"],))
    kinds = {row["type"] for row in assets}
    assert "VOICE" in kinds
    assert "IMAGE" in kinds


def test_every_provider_call_is_recorded(factory):
    """§59: usage rows exist for the free local calls too, so the trace is
    complete rather than only listing what was billed."""
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})
    asyncio.run(execution.drive_run(run["id"]))

    operations = {
        row["operation"]
        for row in db.query("SELECT operation FROM cf_usage_records WHERE pipeline_run_id = ?", (run["id"],))
    }
    assert {"voice", "image"} <= operations


def test_transient_failure_is_retried(factory):
    """§20: a provider that fails once then succeeds must not fail the run."""
    factory.unregister("mock-voice")
    factory.register(
        mocks.MockVoiceProvider("mock-voice", config={"failTimes": 1}),
        default_for=ProviderCategory.VOICE,
    )
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.COMPLETED
    voice_row = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "voice")
    assert voice_row["attempt"] >= 2


def test_failing_provider_falls_back_to_the_next_one(factory):
    """§21: the fallback rung that actually answered is recorded."""
    factory.unregister("mock-voice")
    factory.register(
        mocks.MockVoiceProvider("voice-primary", config={"alwaysFail": True}),
        default_for=ProviderCategory.VOICE,
    )
    factory.register(mocks.MockVoiceProvider("voice-backup"))

    nodes, edges = _content_pipeline(
        voice={
            "config": {"provider": "voice-primary", "retryPolicy": {"maxAttempts": 1}},
            "fallbackPolicy": ["voice-backup"],
        }
    )
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    finished = asyncio.run(execution.drive_run(run["id"]))

    voice_row = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "voice")
    assert finished["status"] == RunStatus.COMPLETED
    assert voice_row["provider"] == "voice-backup"
    assert voice_row["fallback_index"] == 1


def test_unrecoverable_failure_fails_the_run(factory):
    factory.unregister("mock-voice")
    factory.register(
        mocks.MockVoiceProvider("mock-voice", config={"alwaysFail": True, "failCode": "PROVIDER_AUTH"}),
        default_for=ProviderCategory.VOICE,
    )
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.FAILED
    assert finished["error_code"] == "PROVIDER_AUTH"


def test_approval_suspends_and_resumes(factory):
    """§36: the run stops without holding a worker, and the owner's decision
    puts it back in the queue."""
    nodes, edges = _content_pipeline()
    nodes.append({"id": "approve", "type": "human_approval"})
    edges.append({"source": "collect", "target": "approve"})
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    suspended = asyncio.run(execution.drive_run(run["id"]))
    assert suspended["status"] == RunStatus.WAITING_APPROVAL

    execution.approve_run(run["id"], decision="APPROVED", note="ok")
    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.COMPLETED
    approve_row = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "approve")
    assert db.loads(approve_row["output"], {})["approved"] is True


def test_full_auto_does_not_ask_for_approval(factory):
    nodes, edges = _content_pipeline()
    nodes.append({"id": "approve", "type": "human_approval"})
    edges.append({"source": "collect", "target": "approve"})
    pipeline = _activate(nodes, edges, publishing_mode="FULL_AUTO")
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.COMPLETED


def test_external_job_is_parked_and_polled(factory):
    """§25: a submitted video job goes through WAITING_EXTERNAL and its id is
    persisted before the first poll."""
    factory.unregister("mock-video")
    factory.register(
        mocks.MockVideoProvider("mock-video", config={"jobDuration": 0.2}),
        default_for=ProviderCategory.VIDEO,
    )
    nodes = [
        {"id": "trigger", "type": "manual_trigger"},
        {
            "id": "clips",
            "type": "video_generation",
            "config": {
                "prompts": [{"sceneId": "scene-1", "prompt": "a city at night", "duration": 4}],
                "pollInterval": 0.1,
            },
        },
    ]
    edges = [{"source": "trigger", "target": "clips"}]
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"])

    finished = asyncio.run(execution.drive_run(run["id"]))

    clips_row = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "clips")
    assert finished["status"] == RunStatus.COMPLETED
    assert clips_row["external_job_id"].startswith("mockjob-")
    assert db.query("SELECT id FROM cf_assets WHERE type = 'VIDEO'")


def test_branching_skips_the_path_not_taken(factory):
    """§18: QA FAIL wiring stays SKIPPED when QA passes."""
    nodes = [
        {"id": "trigger", "type": "manual_trigger"},
        {"id": "check", "type": "condition", "config": {"left": True, "operator": "truthy"}},
        {"id": "on_true", "type": "transform", "config": {"value": "yes"}},
        {"id": "on_false", "type": "transform", "config": {"value": "no"}},
    ]
    edges = [
        {"source": "trigger", "target": "check"},
        {"source": "check", "target": "on_true", "branch": "TRUE"},
        {"source": "check", "target": "on_false", "branch": "FALSE"},
    ]
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"])

    asyncio.run(execution.drive_run(run["id"]))

    statuses = {row["node_id"]: row["status"] for row in execution.get_node_runs(run["id"])}
    assert statuses["on_true"] == NodeStatus.COMPLETED
    assert statuses["on_false"] == NodeStatus.SKIPPED


def test_cancel_stops_the_run(factory):
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})

    execution.cancel_run(run["id"], reason="stop it")
    finished = execution.get_run(run["id"])

    assert finished["status"] == RunStatus.CANCELLED
    statuses = {row["status"] for row in execution.get_node_runs(run["id"])}
    assert NodeStatus.PENDING not in statuses


def test_retry_from_node_keeps_completed_upstream_work(factory):
    """§41: retrying the failed node does not re-run — or re-pay for — the
    nodes before it."""
    factory.unregister("mock-voice")
    factory.register(
        mocks.MockVoiceProvider("mock-voice", config={"alwaysFail": True, "failCode": "PROVIDER_AUTH"}),
        default_for=ProviderCategory.VOICE,
    )
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})
    asyncio.run(execution.drive_run(run["id"]))
    assert execution.get_run(run["id"])["status"] == RunStatus.FAILED

    script_before = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "script")

    factory.unregister("mock-voice")
    factory.register(mocks.MockVoiceProvider("mock-voice"), default_for=ProviderCategory.VOICE)
    execution.retry_node(run["id"], "voice")
    finished = asyncio.run(execution.drive_run(run["id"]))

    script_after = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "script")
    assert finished["status"] == RunStatus.COMPLETED
    assert script_after["started_at"] == script_before["started_at"]


def test_retry_refuses_a_completed_node(factory):
    """§42: re-running something that succeeded is a regenerate, and costs
    money — it may not happen by accident."""
    from backend.content_factory.domain import ContentFactoryError

    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})
    asyncio.run(execution.drive_run(run["id"]))

    with pytest.raises(ContentFactoryError):
        execution.retry_node(run["id"], "script")

    execution.retry_node(run["id"], "script", regenerate=True)
    assert execution.get_run(run["id"])["status"] == RunStatus.QUEUED


def test_recover_crashed_runs_requeues_interrupted_work(factory):
    """§17: a worker that died leaves a run claimable again."""
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"], trigger_data={"topic": "AI news"})
    db.execute(
        "UPDATE cf_pipeline_runs SET status = ?, worker_id = 'dead-worker', lease_expires_at = ? WHERE id = ?",
        (RunStatus.RUNNING, "2000-01-01T00:00:00+00:00", run["id"]),
    )
    db.execute(
        "UPDATE cf_node_runs SET status = ? WHERE pipeline_run_id = ? AND node_id = 'script'",
        (NodeStatus.RUNNING, run["id"]),
    )

    recovered = execution.recover_crashed_runs()

    assert run["id"] in recovered
    refreshed = execution.get_run(run["id"])
    assert refreshed["status"] == RunStatus.QUEUED
    assert refreshed["worker_id"] == ""
    script_row = next(row for row in execution.get_node_runs(run["id"]) if row["node_id"] == "script")
    assert script_row["status"] == NodeStatus.PENDING


def test_lease_prevents_two_workers_driving_one_run(factory):
    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"])

    assert execution.claim_run(run["id"]) is True
    db.execute(
        "UPDATE cf_pipeline_runs SET worker_id = 'another-worker' WHERE id = ?", (run["id"],)
    )
    assert execution.claim_run(run["id"]) is False


def test_publish_is_idempotent(factory):
    """§55/§117: a second publish attempt for the same content and channel does
    not reach the platform again."""
    channel = content_store.create_channel(
        platform="YOUTUBE", account_name="Test channel", credentials_ref="secret:test"
    )
    publisher = mocks.MockPublishingProvider("youtube")
    factory.register(publisher, default_for=ProviderCategory.PUBLISHING)

    item = content_store.create_content_item(pipeline_run_id="run-x", title="Video")
    nodes = [
        {"id": "trigger", "type": "manual_trigger"},
        {
            "id": "publish",
            "type": "publish",
            "config": {
                "channelId": channel["id"],
                "contentId": item["id"],
                "title": "Video",
                "privacy": "private",
            },
        },
    ]
    edges = [{"source": "trigger", "target": "publish"}]
    pipeline = _activate(nodes, edges)

    first = execution.create_run(pipeline["id"])
    asyncio.run(execution.drive_run(first["id"]))
    second = execution.create_run(pipeline["id"])
    asyncio.run(execution.drive_run(second["id"]))

    assert len(publisher.published) == 1
    publications = content_store.list_publications(content_id=item["id"])
    assert len(publications) == 1
    assert publications[0]["status"] == "PUBLISHED"


def test_test_run_never_publishes(factory):
    """§107/§108: dry run is decided by the run, not by node config."""
    channel = content_store.create_channel(
        platform="YOUTUBE", account_name="Test channel", credentials_ref="secret:test"
    )
    publisher = mocks.MockPublishingProvider("youtube")
    factory.register(publisher, default_for=ProviderCategory.PUBLISHING)

    item = content_store.create_content_item(pipeline_run_id="run-y", title="Video")
    nodes = [
        {"id": "trigger", "type": "manual_trigger"},
        {
            "id": "publish",
            "type": "publish",
            "config": {"channelId": channel["id"], "contentId": item["id"], "title": "Video"},
        },
    ]
    edges = [{"source": "trigger", "target": "publish"}]
    pipeline = _activate(nodes, edges)

    run = execution.create_run(pipeline["id"], dry_run=True)
    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.COMPLETED
    assert publisher.published == []
    assert content_store.list_publications(content_id=item["id"])[0]["external_id"] == ""


def test_budget_check_stops_an_overspending_run(factory):
    nodes = [
        {"id": "trigger", "type": "manual_trigger"},
        {"id": "budget", "type": "budget_check", "config": {"maxCost": 0.0001, "onExceeded": "stop"}},
    ]
    edges = [{"source": "trigger", "target": "budget"}]
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"])
    db.execute(
        """
        INSERT INTO cf_usage_records
            (id, workspace_id, pipeline_run_id, provider, model, operation, cost, currency, created_at)
        VALUES ('use-1', 'default', ?, 'mock', 'mock', 'llm', 5.0, 'USD', ?)
        """,
        (run["id"], db.now_iso()),
    )

    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.FAILED
    assert finished["error_code"] == "BUDGET_LIMIT_REACHED"


def test_concurrency_limit_blocks_a_second_run(factory):
    from backend.content_factory.domain import ContentFactoryError

    nodes, edges = _content_pipeline()
    pipeline = _activate(nodes, edges)
    execution.create_run(pipeline["id"])

    with pytest.raises(ContentFactoryError):
        execution.create_run(pipeline["id"])


def test_unresolved_mapping_falls_back_to_node_config(factory):
    """A run started without trigger data must use the node's configured value
    rather than failing outright — the real failure this prevents is a manual
    run of a scheduled pipeline dying at its first node."""
    nodes = [
        {"id": "trigger", "type": "manual_trigger"},
        {
            "id": "script",
            "type": "script",
            "config": {"agentId": "cf_script", "duration": 3, "topic": "запасная тема"},
            # Nothing supplies trigger.topic on a bare manual run.
            "inputMapping": {"topic": "{{ trigger.topic }}"},
        },
    ]
    edges = [{"source": "trigger", "target": "script"}]
    pipeline = _activate(nodes, edges)
    run = execution.create_run(pipeline["id"])

    finished = asyncio.run(execution.drive_run(run["id"]))

    assert finished["status"] == RunStatus.COMPLETED
