"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import annotations

# Loader for the tiered alias dictionary used in entity disambiguation.
#
# The dictionary file (YAML, see graphiti_core/resources/aliases.yaml) maps
# surface forms to canonical entities with a level:
#
# - ``strong``: unconditionally mergeable aliases confirmed by humans.
# - ``conditional``: aliases that only resolve in a matching context and must
#   never be merged globally without further checks.
#
# Override the built-in dictionary by pointing ``GRAPHITI_ALIAS_DICT_PATH`` at
# another YAML file.
import logging
import os
from pathlib import Path

from graphiti_core.utils.maintenance.normalizer import AliasHit, normalize_name

logger = logging.getLogger(__name__)

DEFAULT_ALIAS_DICT_PATH = Path(__file__).parent.parent.parent / 'resources' / 'aliases.yaml'
ALIAS_DICT_PATH_ENV = 'GRAPHITI_ALIAS_DICT_PATH'


class AliasDictionary:
    """In-memory index of the alias dictionary keyed by normalized surface form."""

    def __init__(self, entries: list[dict]):
        self._by_surface: dict[tuple[str, str], list[dict]] = {}
        for entry in entries:
            canonical_name = str(entry.get('canonical_name', '')).strip()
            if not canonical_name:
                continue
            entity_type = str(entry.get('entity_type', 'Entity')).strip()
            for alias in [canonical_name, *entry.get('aliases', [])]:
                surface = normalize_name(str(alias))
                if not surface:
                    continue
                self._by_surface.setdefault((surface, entity_type), []).append(entry)

    def lookup(self, mention: str, entity_type: str | None = None) -> AliasHit | None:
        """Return the alias hit for a mention.

        An exact ``(surface, entity_type)`` match wins; a surface-only match
        (registered without a type, or when no typed entry exists) is returned
        as a fallback so cross-type candidates can still be detected and
        escalated to context-aware resolution.
        """
        surface = normalize_name(mention)
        if not surface:
            return None

        if entity_type is not None:
            typed = self._by_surface.get((surface, entity_type))
            if typed:
                return self._to_hit(typed[0])
            # A strong alias registered under another type must not silently
            # match this type; only fall through if no typed entry exists.
            typed_entries_exist = any(
                key[0] == surface and key[1] != entity_type for key in self._by_surface
            )
            if typed_entries_exist:
                return None

        untyped = self._by_surface.get((surface, 'Entity'))
        if untyped:
            return self._to_hit(untyped[0])
        return None

    @staticmethod
    def _to_hit(entry: dict) -> AliasHit:
        return AliasHit(
            canonical_name=str(entry.get('canonical_name', '')),
            canonical_id=str(entry.get('canonical_id', '')),
            entity_type=str(entry.get('entity_type', 'Entity')),
            alias_level=str(entry.get('alias_level', 'conditional')),
            confidence=float(entry.get('confidence', 0.0)),
            conditions=list(entry.get('conditions', [])),
        )


def load_alias_dictionary(path: str | Path | None = None) -> AliasDictionary | None:
    """Load the alias dictionary; returns None when unavailable so callers can proceed."""
    dict_path = Path(path or os.environ.get(ALIAS_DICT_PATH_ENV) or DEFAULT_ALIAS_DICT_PATH)
    try:
        import yaml

        with open(dict_path, encoding='utf-8') as handle:
            data = yaml.safe_load(handle) or {}
        entries = list(data.get('aliases', []))
    except FileNotFoundError:
        logger.warning('Alias dictionary not found at %s; skipping alias matching', dict_path)
        return None
    except ImportError:
        logger.warning('pyyaml is not installed; alias dictionary matching is disabled')
        return None
    except Exception:
        logger.exception('Failed to load alias dictionary from %s', dict_path)
        return None

    return AliasDictionary(entries)


__all__ = [
    'AliasDictionary',
    'ALIAS_DICT_PATH_ENV',
    'DEFAULT_ALIAS_DICT_PATH',
    'load_alias_dictionary',
]
