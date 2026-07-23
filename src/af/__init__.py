"""AF Agent Framework public package."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from af.bootstrap import AFApplication, create_application, create_application_async

__all__ = ["AFApplication", "create_application", "create_application_async"]

__version__ = "0.1.0a0"


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from af.bootstrap import AFApplication, create_application, create_application_async

    exports = {
        "AFApplication": AFApplication,
        "create_application": create_application,
        "create_application_async": create_application_async,
    }
    value = exports[name]
    globals()[name] = value
    return value
