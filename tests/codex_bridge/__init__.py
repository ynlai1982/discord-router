"""Codex bridge test package.

unittest discovery imports this package as ``codex_bridge`` when the start
directory is ``tests``. Include the real package path so test modules can still
import ``codex_bridge.bot`` and sibling implementation modules.
"""

from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[2] / "codex_bridge"))
