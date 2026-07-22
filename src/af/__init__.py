"""AF Agent Framework public package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from af.bootstrap import AFApplication, create_application

__all__ = ["AFApplication", "create_application"]

__version__ = "0.1.0a0"


def __getattr__(name: str) -> object:
    """Load composition-root exports only when callers explicitly request them."""

    if name in __all__:
        from af.bootstrap import AFApplication, create_application

        return {
            "AFApplication": AFApplication,
            "create_application": create_application,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
