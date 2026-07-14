"""Prompt Composer for the Council of Agents.

Builds static system prompts from composable fragments. No dynamic content —
that lives in the user message via context_envelope.build_context_envelope().
"""

import json
import logging
import os
import pathlib

logger = logging.getLogger(__name__)

PROMPTS_DIR = pathlib.Path(__file__).parent.parent / "prompts"
USE_FRAGMENTS = os.getenv("ODYSSEUS_USE_FRAGMENTS", "true").lower() == "true"


class PromptComposer:
    def __init__(self, prompts_dir=None):
        self._dir = prompts_dir or PROMPTS_DIR
        self._fragments_dir = self._dir / "fragments"
        self._registry_path = self._dir / "fragments.json"
        self._registry = None
        self._fragment_cache = {}
        self._prefix_cache = {}
        self._monolithic_cache = {}

    def _load_registry(self):
        if self._registry is None:
            if not self._registry_path.exists():
                logger.warning(
                    "fragments.json not found at %s, falling back to monolithic prompts",
                    self._registry_path,
                )
                return {}
            with open(self._registry_path, "r", encoding="utf-8") as f:
                try:
                    self._registry = json.load(f)
                except json.JSONDecodeError as e:
                    logger.error("Malformed fragments.json at %s: %s", self._registry_path, e)
                    return {}
        return self._registry

    def _load_fragment(self, path):
        if path not in self._fragment_cache:
            full = (self._fragments_dir / path).resolve()
            if not str(full).startswith(str(self._fragments_dir.resolve())):
                logger.error("Path traversal blocked: %s", path)
                return ""
            if not full.exists():
                logger.warning("Fragment file not found: %s", full)
                return ""
            self._fragment_cache[path] = full.read_text(encoding="utf-8").strip()
        return self._fragment_cache[path]

    def compose(self, role):
        """Compose a static system prompt for the given role.

        Falls back to monolithic {role}.md if fragments are unavailable.
        Returns empty string if neither fragments nor monolithic file exist.
        """
        if not USE_FRAGMENTS:
            return self._load_monolithic(role)
        reg = self._load_registry()
        keys = reg.get("compositions", {}).get(role)
        if keys is None:
            return self._load_monolithic(role)
        result = self._load_static_prefix(role, keys)
        if not result:
            return self._load_monolithic(role)
        return result

    def _load_static_prefix(self, role, keys):
        if role in self._prefix_cache:
            return self._prefix_cache[role]
        reg = self._load_registry()
        fragment_paths = reg.get("fragments", {})
        parts = []
        for key in keys:
            path = fragment_paths.get(key, key if key.endswith(".md") else f"{key}.md")
            content = self._load_fragment(path)
            if content:
                parts.append(content)
        result = "\n\n".join(parts)
        if not result:
            logger.warning("All fragments empty for role '%s'", role)
        self._prefix_cache[role] = result
        return result

    def _load_monolithic(self, role):
        if role in self._monolithic_cache:
            return self._monolithic_cache[role]
        p = self._dir / f"{role}.md"
        if not p.exists():
            logger.warning("Monolithic prompt not found: %s", p)
            return ""
        content = p.read_text(encoding="utf-8")
        content = content.replace("{{workspace}}", "")
        self._monolithic_cache[role] = content
        return content

    def invalidate_cache(self):
        """Clear all internal caches. Call if prompt files change at runtime."""
        self._fragment_cache.clear()
        self._prefix_cache.clear()
        self._monolithic_cache.clear()
        self._registry = None
