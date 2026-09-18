"""qq - ask a quick LLM question from your terminal."""

#: The single source of truth for the version. ``pyproject.toml`` declares the
#: version dynamic and points hatch at this line, so the package metadata is
#: derived from it rather than kept in step with it by hand. Read it from here
#: (``from . import __version__``) rather than from importlib.metadata, which
#: reports whatever wheel is installed - not the source tree being run.
__version__ = "0.2.1"

__all__ = ["__version__"]
