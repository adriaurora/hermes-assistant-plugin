try:  # Plugin loader imports this as a package; pytest may import the file directly.
    from .hermes_assistant.adapter import register
except ImportError:  # pragma: no cover - direct-module test collection only
    from hermes_assistant.adapter import register

__all__ = ["register"]
