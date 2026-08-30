"""Node registry (§13).

Importing this package registers every built-in node type. The registry is the
only way the engine and the validator find a handler, so a new node type is one
module plus one `register()` call — no switch statement to update in three
places.
"""

from __future__ import annotations

from backend.content_factory.nodes.base import (  # noqa: F401
    NodeContext,
    NodeHandler,
    get_node_handler,
    is_known_type,
    register,
    registered_types,
)

# Import for the side effect of registering handlers. Order is irrelevant; the
# registry is a flat dict keyed by node type.
from backend.content_factory.nodes import ai as _ai  # noqa: F401,E402
from backend.content_factory.nodes import control as _control  # noqa: F401,E402
from backend.content_factory.nodes import media as _media  # noqa: F401,E402
from backend.content_factory.nodes import publishing as _publishing  # noqa: F401,E402
from backend.content_factory.nodes import rendering as _rendering  # noqa: F401,E402

__all__ = [
    "NodeContext",
    "NodeHandler",
    "get_node_handler",
    "is_known_type",
    "register",
    "registered_types",
]
