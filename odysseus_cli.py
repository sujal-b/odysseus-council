"""odysseus_cli — installable entry point for the `odysseus` dispatcher.

Thin shim: loads `scripts/odysseus` by path (same SourceFileLoader contract
as tests/helpers/cli_loader.py) and re-exports its main(). Single version
source: scripts/odysseus VERSION is canonical; this module reads it from the
loaded dispatcher and never hardcodes its own copy.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


def _load_dispatcher():
    loader = importlib.machinery.SourceFileLoader(
        "odysseus_cli", str(_SCRIPTS_DIR / "odysseus")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_dispatcher = _load_dispatcher()

VERSION = _dispatcher.VERSION
__all__ = ["VERSION", "main"]


def main(argv=None) -> int:
    """Entry point for [project.scripts] odysseus. Returns exit code."""
    return _dispatcher.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
