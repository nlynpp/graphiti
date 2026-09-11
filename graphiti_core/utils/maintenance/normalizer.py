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

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graphiti_core.utils.maintenance.alias_dict import AliasDictionary

# Scoring weights from the normalization spec (section 8).
SCORE_EXTERNAL_ID_MATCH = 1.00
SCORE_STRONG_ALIAS = 0.95
SCORE_EXACT_TYPE_AND_NAME = 0.90
SCORE_SAME_DOCUMENT_CONTEXT = 0.20
SCORE_RELATION_ATTRIBUTE_MATCH = 0.20
SCORE_VECTOR_SIMILARITY_WEIGHT = 0.20
SCORE_TYPE_CONFLICT = -1.00
SCORE_ATTRIBUTE_CONFLICT = -0.60

# Decision thresholds (section 8).
MERGE_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.70

# Mentions of review decisions on MENTIONS edges / audit records.
RESOLUTION_MERGED = 'merged'
RESOLUTION_NEW = 'new'
RESOLUTION_PENDING_REVIEW = 'pending_review'
RESOLUTION_KEPT_SEPARATE = 'kept_separate'

# Full-width -> half-width translation table for common characters.
_FULLWIDTH_OFFSET = 0xFEE0

# Quote characters that carry no disambiguating meaning.
_STRIP_QUOTES = "“”„‟''\"«»「」『』《》〈〉"

# Brackets unified to their ASCII form.
_BRACKET_MAP = {
    '（': '(',
    '）': ')',
    '【': '[',
    '】': ']',
    '［': '[',
    '］': ']',
    '｛': '{',
    '｝': '}',
    '〔': '[',
    '〕': ']',
}


@lru_cache(maxsize=4096)
def normalize_name(name: str) -> str:
    """Build the matching key for an entity mention.

    Applies: strip, full-width to half-width, whitespace collapsing, bracket
    unification and removal of decorative quotes. The result is lowercased so
    that it can serve as a deterministic dedup key. Never use it as display
    name; the original mention text is preserved by callers.
    """
    if not name:
        return ''

    text = name.strip()
    if not text:
        return ''

    # Remove decorative quotes first (they may wrap the entire name).
    text = text.strip(_STRIP_QUOTES).strip()

    # Full-width ASCII and common punctuation to half-width.
    text = ''.join(
        chr(ord(char) - _FULLWIDTH_OFFSET)
        if 0xFF01 <= ord(char) <= 0xFF5E
        else ('\u3000' if char == '\u3000' else char)
        for char in text
    )

    # Unify brackets.
    text = ''.join(_BRACKET_MAP.get(char, char) for char in text)

    # NFKC folds remaining compatibility characters (e.g. ①, ㈠).
    text = unicodedata.normalize('NFKC', text)

    # Collapse whitespace, lowercase.
    text = re.sub(r'\s+', ' ', text).strip().lower()

    # Trim quotes that survived interior repositioning after collapsing.
    return text.strip(_STRIP_QUOTES).strip()


@lru_cache(maxsize=4096)
def slugify(canonical_name: str) -> str:
    """Derive a stable ``canonical_id`` from a canonical name.

    Keeps CJK characters (they are the identity for Chinese legal entities),
    lowercases latin characters, and replaces any other non-alphanumeric run
    with a single underscore.
    """
    text = normalize_name(canonical_name)
    slug = re.sub(r'[^0-9a-z\u4e00-\u9fff\u3400-\u4dbf]+', '_', text)
    return slug.strip('_') or 'entity'


def generate_entity_key(entity_type: str, canonical_name: str) -> str:
    """Return the global unique key ``entity_type:canonical_id``.

    Names alone must never be the key: e.g. Apple Inc. (Enterprise) and
    苹果 (Material) share a surface form but are different entities, while
    申请 (procedure) and 申请 (business matter) are same-named but distinct.
    """
    normalized_type = slugify(entity_type)
    return f'{normalized_type}:{slugify(canonical_name)}'


@dataclass
class AliasHit:
    """A successful alias-dictionary lookup."""

    canonical_name: str
    canonical_id: str
    entity_type: str
    alias_level: str  # 'strong' | 'conditional'
    confidence: float
    conditions: list[str] = field(default_factory=list)


@dataclass
class CandidateScore:
    """Explainable merge score for one (extracted, existing) candidate pair."""

    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def decision(self) -> str:
        if self.score >= MERGE_THRESHOLD:
            return RESOLUTION_MERGED
        if self.score >= REVIEW_THRESHOLD:
            return RESOLUTION_PENDING_REVIEW
        return RESOLUTION_KEPT_SEPARATE


def score_candidate(
    extracted_type: str,
    existing_type: str,
    normalized_extracted_name: str,
    normalized_existing_name: str,
    alias_hit: AliasHit | None = None,
    same_document: bool = False,
    relations_or_attributes_match: bool = False,
    vector_similarity: float | None = None,
) -> CandidateScore:
    """Compute the explainable weighted score between two entity candidates.

    Vector similarity is only ever a small additive signal and must never be
    the sole basis for a merge (spec section 8).
    """
    score = 0.0
    reasons: list[str] = []

    types_equal = normalize_name(extracted_type) == normalize_name(existing_type)
    if not types_equal:
        score += SCORE_TYPE_CONFLICT
        reasons.append(f'type conflict: {extracted_type} vs {existing_type}')
        return CandidateScore(score, reasons)

    if alias_hit is not None and alias_hit.alias_level == 'strong':
        score += SCORE_STRONG_ALIAS
        reasons.append(f'strong alias hit: {alias_hit.canonical_name}')
    elif normalized_extracted_name == normalized_existing_name:
        score += SCORE_EXACT_TYPE_AND_NAME
        reasons.append('type and canonical name exact match')

    if alias_hit is not None and alias_hit.alias_level == 'conditional':
        # Conditional aliases alone cannot merge; they only add context signal.
        score += SCORE_SAME_DOCUMENT_CONTEXT
        reasons.append(f'conditional alias hit: {alias_hit.canonical_name}')

    if same_document:
        score += SCORE_SAME_DOCUMENT_CONTEXT
        reasons.append('same document context')

    if relations_or_attributes_match:
        score += SCORE_RELATION_ATTRIBUTE_MATCH
        reasons.append('relations and attributes consistent')

    if vector_similarity is not None:
        score += SCORE_VECTOR_SIMILARITY_WEIGHT * max(0.0, min(1.0, vector_similarity))
        reasons.append(f'vector similarity {vector_similarity:.3f}')

    return CandidateScore(score=round(score, 4), reasons=reasons)


class EntityNormalizer:
    """Facade combining name cleaning, entity-key generation and alias lookup."""

    def __init__(self, alias_dictionary: AliasDictionary | None = None):
        self.alias_dictionary = alias_dictionary

    def normalize(self, name: str) -> str:
        return normalize_name(name)

    def entity_key(self, entity_type: str, canonical_name: str) -> str:
        return generate_entity_key(entity_type, canonical_name)

    def lookup_alias(self, mention: str, entity_type: str | None = None) -> AliasHit | None:
        """Resolve a mention through the alias dictionary, if one is loaded."""
        if self.alias_dictionary is None:
            return None
        return self.alias_dictionary.lookup(mention, entity_type)


__all__ = [
    'AliasHit',
    'CandidateScore',
    'EntityNormalizer',
    'MERGE_THRESHOLD',
    'REVIEW_THRESHOLD',
    'RESOLUTION_KEPT_SEPARATE',
    'RESOLUTION_MERGED',
    'RESOLUTION_NEW',
    'RESOLUTION_PENDING_REVIEW',
    'SCORE_ATTRIBUTE_CONFLICT',
    'SCORE_EXTERNAL_ID_MATCH',
    'SCORE_RELATION_ATTRIBUTE_MATCH',
    'SCORE_SAME_DOCUMENT_CONTEXT',
    'SCORE_STRONG_ALIAS',
    'SCORE_TYPE_CONFLICT',
    'SCORE_VECTOR_SIMILARITY_WEIGHT',
    'generate_entity_key',
    'normalize_name',
    'score_candidate',
    'slugify',
]
