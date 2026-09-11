"""Ontology loader: parses the versioned YAML ontology (types + relations).

Per the development spec (section 11), the ontology is defined before any
extraction happens; entity/relation validation always runs against this
whitelist, and every written node carries ``ontology_id``/``ontology_version``.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_ONTOLOGY_PATH = Path(__file__).parent.parent / 'resources' / 'legal_ontology.yaml'
ONTOLOGY_PATH_ENV = 'GRAPHITI_ONTOLOGY_PATH'


class Ontology:
    """Parsed ontology whitelist: entity types, attributes and relations."""

    def __init__(self, ontology_id: str, version: str, domain: str, data: dict):
        self.ontology_id = ontology_id
        self.version = version
        self.domain = domain
        self._entity_types: dict[str, dict] = data.get('entity_types', {})
        self._relation_types: dict[str, dict] = data.get('relation_types', {})
        # parent -> children for hierarchical types (e.g. Law -> LegalDocument)
        self._descendants: dict[str, set[str]] = {}
        for type_name, spec in self._entity_types.items():
            parent = spec.get('parent')
            if parent:
                self._descendants.setdefault(parent, set()).add(type_name)

    @property
    def entity_type_names(self) -> set[str]:
        return set(self._entity_types)

    @property
    def relation_type_names(self) -> set[str]:
        return set(self._relation_types)

    @property
    def relation_types(self) -> dict[str, dict]:
        return dict(self._relation_types)

    def is_valid_entity_type(self, type_name: str) -> bool:
        return type_name in self._entity_types

    def is_subtype_of(self, type_name: str, ancestor: str) -> bool:
        """True when type_name is ancestor or any descendant of it."""
        if type_name == ancestor:
            return True
        frontier = {ancestor}
        while frontier:
            children = self._descendants.get(frontier.pop(), set())
            if type_name in children:
                return True
            frontier |= children
        return False

    def relation_allowed(self, relation: str, source_type: str, target_type: str) -> bool:
        spec = self._relation_types.get(relation)
        if spec is None:
            return False
        return any(
            self.is_subtype_of(source_type, s) and self.is_subtype_of(target_type, t)
            for s in spec.get('source', [])
            for t in spec.get('target', [])
        )

    def entity_type_description(self, type_name: str) -> str:
        return self._entity_types.get(type_name, {}).get('description', '')


def load_ontology(path: str | Path | None = None) -> Ontology | None:
    """Load the ontology; returns None when the file is unavailable."""
    from os import environ

    ontology_path = Path(path or environ.get(ONTOLOGY_PATH_ENV) or DEFAULT_ONTOLOGY_PATH)
    try:
        with open(ontology_path, encoding='utf-8') as handle:
            data = yaml.safe_load(handle) or {}
        return Ontology(
            ontology_id=str(data.get('ontology_id', 'unknown')),
            version=str(data.get('version', '0.0.0')),
            domain=str(data.get('domain', '')),
            data=data,
        )
    except FileNotFoundError:
        logger.warning('Ontology file not found at %s', ontology_path)
        return None
    except Exception:
        logger.exception('Failed to load ontology from %s', ontology_path)
        return None


__all__ = ['Ontology', 'DEFAULT_ONTOLOGY_PATH', 'ONTOLOGY_PATH_ENV', 'load_ontology']
