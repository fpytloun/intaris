"""intaris — Guardrails service for AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("intaris")
except PackageNotFoundError:
    # Package not installed (running from source without pip install)
    __version__ = "0.0.0+dev"
