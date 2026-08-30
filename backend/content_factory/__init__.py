"""CONTENT FACTORY — autonomous content production for VEXA.

Orchestration layer, not a "generate video" button (§145): pipelines describe
the workflow, agents execute the steps, providers are swappable adapters, and
assets/traces record what actually happened.

The package deliberately imports nothing heavy at module level — the backend
imports it during app startup, and a missing optional dependency (boto3, an
unconfigured provider) must degrade that provider, never the whole app.

See `docs/content-factory-integration-plan.md` for how this maps onto the rest
of HERMES and which parts of the spec were adapted to this deployment.
"""

from __future__ import annotations

__all__ = ["schema", "domain", "db"]
