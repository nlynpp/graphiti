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

# Pending-review queue and merge audit records for entity normalization.
#
# Both record types are stored as graph nodes (spec section 10):
#
# - ``(:PendingReview)`` — candidate merges whose explainable score fell into
#   the review band; nothing was merged, a human must decide later.
# - ``(:MergeAudit)`` — one record per automatic merge with the trigger rule,
#   score and provenance so mistakes can be traced and split.
import logging
from typing import Any

from graphiti_core.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


async def save_pending_review(
    driver,
    entity_key: str,
    candidate_keys: list[str],
    score: float,
    reason: str,
    source_chunk_id: str | None = None,
    group_id: str | None = None,
    context: str | None = None,
) -> None:
    """Persist one pending-review record keyed by the candidate entity key."""
    query = """
    MERGE (pr:PendingReview {entity_key: $entity_key})
    SET pr.candidate_keys = $candidate_keys,
        pr.score = $score,
        pr.reason = $reason,
        pr.context = $context,
        pr.source_chunk_id = $source_chunk_id,
        pr.group_id = $group_id,
        pr.status = 'pending',
        pr.created_at = $created_at
    """
    params: dict[str, Any] = {
        'entity_key': entity_key,
        'candidate_keys': candidate_keys,
        'score': score,
        'reason': reason,
        'context': context,
        'source_chunk_id': source_chunk_id,
        'group_id': group_id,
        'created_at': utc_now().isoformat(),
    }
    try:
        await driver.execute_query(query, **params)
    except Exception:
        logger.exception('Failed to save PendingReview for %s', entity_key)


async def save_merge_audit(
    driver,
    old_entity_key: str,
    new_entity_key: str,
    trigger_rule: str,
    score: float | None,
    source_chunk_id: str | None = None,
    group_id: str | None = None,
    operator: str = 'auto:normalizer',
) -> None:
    """Append an audit record for one automatic merge (spec section 10)."""
    query = """
    CREATE (ma:MergeAudit {
        old_entity_key: $old_entity_key,
        new_entity_key: $new_entity_key,
        trigger_rule: $trigger_rule,
        score: $score,
        source_chunk_id: $source_chunk_id,
        group_id: $group_id,
        operator: $operator,
        created_at: $created_at
    })
    """
    params: dict[str, Any] = {
        'old_entity_key': old_entity_key,
        'new_entity_key': new_entity_key,
        'trigger_rule': trigger_rule,
        'score': score,
        'source_chunk_id': source_chunk_id,
        'group_id': group_id,
        'operator': operator,
        'created_at': utc_now().isoformat(),
    }
    try:
        await driver.execute_query(query, **params)
    except Exception:
        logger.exception('Failed to save MergeAudit for %s -> %s', old_entity_key, new_entity_key)


__all__ = ['save_merge_audit', 'save_pending_review']
