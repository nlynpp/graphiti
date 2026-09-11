"""Server-side ontology setup: entity type models for constrained extraction.

Loaded once per worker from graphiti_core/utils/ontology_entity_types so the
ontology YAML stays the single source of truth; the Dockerfile ships the
editable local graphiti_core, and ontology/legal_ontology.yaml is COPYed into
the image alongside it.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_cache: dict[str, object] = {}


def get_ontology_entity_types():
    """Return pydantic entity-type models, or None when ontology is disabled."""
    if os.environ.get('GRAPHITI_ONTOLOGY_ENABLED', 'true').lower() not in ('1', 'true', 'yes'):
        return None
    if 'types' not in _cache:
        from graphiti_core.utils.ontology_entity_types import get_legal_entity_types

        types = get_legal_entity_types()
        if types is None:
            logger.warning('Ontology unavailable; extraction runs unconstrained')
        _cache['types'] = types
    return _cache['types']


def ontology_instructions() -> str | None:
    """Relation whitelist injected into the extraction prompt (spec section 6)."""
    if 'instructions' in _cache:
        return _cache['instructions']  # type: ignore[return-value]

    from graphiti_core.utils.ontology_loader import load_ontology

    ontology = load_ontology()
    if ontology is None:
        _cache['instructions'] = None
        return None

    relations = []
    for name, spec in ontology.relation_types.items():
        sources = '|'.join(spec.get('source', []))
        targets = '|'.join(spec.get('target', []))
        relations.append(f'- {name}: ({sources}) -> ({targets})')

    instructions = (
        '本体约束（必须遵守）：\n'
        '1. 不得创造白名单之外的实体类型或关系类型。\n'
        '2. 只有原文明确表达的事实才能抽取；每个实体和关系必须来自当前文本。\n'
        '3. 无法判断类型时使用 UnknownEntity。\n'
        '4. 关系名称必须使用以下白名单（源类型 -> 目标类型）：\n'
        + '\n'.join(relations)
    )
    _cache['instructions'] = instructions
    return instructions


__all__ = ['get_ontology_entity_types', 'ontology_instructions']
