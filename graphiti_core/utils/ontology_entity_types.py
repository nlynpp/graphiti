"""Build Graphiti entity_types (pydantic models) from the legal ontology.

Graphiti constrains LLM extraction to the keys of the ``entity_types`` dict
passed to ``add_episode``; generating those models from the ontology YAML
keeps a single source of truth (spec section 10: ontology precedes
extraction, LLM cannot invent types).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, create_model

from graphiti_core.utils.ontology_loader import Ontology, load_ontology

logger = logging.getLogger(__name__)


def _attribute_field(spec: dict[str, Any]):
    from typing import Optional as _Optional

    field_type: type = str
    values = spec.get('values')
    if spec.get('type') == 'number':
        field_type = float
    elif spec.get('type') == 'enum' and values:
        from typing import Literal

        field_type = Literal[tuple(values)]
    # Attributes are Optional: LLMs legitimately omit them, and a None must
    # not invalidate the whole extraction. Requiredness is a validator
    # concern (spec 7.2), not an extraction-blocking constraint.
    return (
        _Optional[field_type],
        Field(default=None, description=str(spec.get('description', '')) or None),
    )


def build_entity_type_models(ontology: Ontology) -> dict[str, type[BaseModel]]:
    """One pydantic model per ontology entity type, with described fields."""
    models: dict[str, type[BaseModel]] = {}
    # Insert root types first so parents precede children in the prompt.
    for type_name, spec in _ordered_types(ontology):
        fields: dict[str, Any] = {}
        for attr_name, attr_spec in (spec.get('attributes') or {}).items():
            fields[attr_name] = _attribute_field(attr_spec)
        description = spec.get('description', type_name)
        models[type_name] = create_model(type_name, __doc__=description, **fields)
    return models


def _ordered_types(ontology: Ontology) -> list[tuple[str, dict]]:
    """Entity types ordered parents-first for stable prompt construction."""
    raw = _raw_entity_types(ontology)
    ordered: list[tuple[str, dict]] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        parent = (raw.get(name) or {}).get('parent')
        if parent and parent in raw:
            visit(parent)
        ordered.append((name, raw[name]))

    for name in raw:
        visit(name)
    return ordered


def _raw_entity_types(ontology: Ontology) -> dict[str, dict]:
    return ontology._entity_types  # noqa: SLF001 - sibling module within core utils


def get_legal_entity_types() -> dict[str, type[BaseModel]] | None:
    ontology = load_ontology()
    if ontology is None:
        return None
    return build_entity_type_models(ontology)


__all__ = ['build_entity_type_models', 'get_legal_entity_types']
