"""PendingReview resolution tool (normalization spec sections 5.3 / 10).

Policy (user-specified):
  - score >= 0.85  -> auto-accept: merge the candidate entity into the
    review-key entity, migrate its MENTIONS edges, write a MergeAudit record.
  - score <  0.85  -> reject: mark the review record 'rejected' and append a
    JSONL log line for human audit. Nothing is merged.

Accepted records are marked 'accepted'; both outcomes leave a full audit
trail (MergeAudit node in the graph, rejected_review log on disk).

Usage:
    uv run python ingestion/resolve_pending.py                # apply policy
    uv run python ingestion/resolve_pending.py --dry-run      # report only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from neo4j import AsyncGraphDatabase

AUTO_ACCEPT_THRESHOLD = 0.85
DEFAULT_LOG = Path(__file__).parent.parent / 'logs' / 'rejected_review.jsonl'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def fetch_pending(driver) -> list[dict]:
    result = await driver.execute_query(
        """
        MATCH (pr:PendingReview)
        WHERE pr.status = 'pending' AND coalesce(pr.kind, 'entity') = 'entity'
        RETURN pr.entity_key AS entity_key,
               pr.candidate_keys AS candidate_keys,
               pr.score AS score,
               pr.reason AS reason,
               pr.source_chunk_id AS source_chunk_id,
               pr.group_id AS group_id
        ORDER BY pr.score DESC
        """
    )
    return [dict(r) for r in result.records]


async def accept_record(driver, record: dict) -> bool:
    """Merge the first existing candidate into the review-key entity."""
    entity_key = record['entity_key']
    candidates = record.get('candidate_keys') or []
    merged = False
    for candidate_key in candidates:
        if candidate_key == entity_key:
            continue
        # Both endpoints carry derived uuid5s from their entity_key + group.
        result = await driver.execute_query(
            """
            MATCH (keep:Entity {entity_key: $entity_key}), (drop:Entity {entity_key: $candidate_key})
            WHERE keep.uuid <> drop.uuid
            OPTIONAL MATCH (drop)-[m:MENTIONS]->(c:Episodic)
            MERGE (keep)-[km:MENTIONS {uuid: m.uuid}]->(c)
            SET km.mention_text = coalesce(m.mention_text, km.mention_text),
                km.source_chunk_id = coalesce(m.source_chunk_id, km.source_chunk_id),
                km.resolution = 'merged',
                km.resolution_confidence = $score
            WITH keep, drop, count(km) AS moved
            DETACH DELETE drop
            CREATE (ma:MergeAudit {
                old_entity_key: $candidate_key,
                new_entity_key: $entity_key,
                trigger_rule: 'pending_review accepted (score >= threshold)',
                score: $score,
                source_chunk_id: $source_chunk_id,
                group_id: $group_id,
                operator: 'resolve_pending:auto',
                created_at: $now
            })
            RETURN moved
            """,
            entity_key=entity_key,
            candidate_key=candidate_key,
            score=record.get('score'),
            source_chunk_id=record.get('source_chunk_id'),
            group_id=record.get('group_id'),
            now=_now(),
        )
        if result.records and result.records[0].get('moved', 0) >= 0:
            merged = True
    return merged


async def main() -> int:
    parser = argparse.ArgumentParser(description='Resolve PendingReview records by score policy')
    parser.add_argument('--dry-run', action='store_true', help='only report, change nothing')
    parser.add_argument('--threshold', type=float, default=AUTO_ACCEPT_THRESHOLD)
    parser.add_argument('--neo4j-uri', default='bolt://localhost:7687')
    parser.add_argument('--neo4j-user', default='neo4j')
    parser.add_argument('--neo4j-password', default='Graphiti123')
    parser.add_argument('--log', default=str(DEFAULT_LOG))
    args = parser.parse_args()

    driver = AsyncGraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        pending = await fetch_pending(driver)
        print(f'{len(pending)} pending review record(s)')
        accepted = rejected = missing_entity = 0

        for record in pending:
            score = record.get('score') or 0.0
            if score >= args.threshold:
                if args.dry_run:
                    print(f'ACCEPT {score:.2f} {record["entity_key"]} <- {record["candidate_keys"]}')
                    accepted += 1
                    continue
                ok = await accept_record(driver, record)
                if ok:
                    await driver.execute_query(
                        "MATCH (pr:PendingReview {entity_key: $k}) SET pr.status='accepted', pr.resolved_at=$now",
                        k=record['entity_key'],
                        now=_now(),
                    )
                    accepted += 1
                    print(f'ACCEPTED {score:.2f} {record["entity_key"]}')
                else:
                    missing_entity += 1
                    print(f'SKIP (entity not found) {record["entity_key"]}')
            else:
                entry = {
                    'resolved_at': _now(),
                    'action': 'rejected',
                    'entity_key': record['entity_key'],
                    'candidate_keys': record.get('candidate_keys'),
                    'score': score,
                    'reason': record.get('reason'),
                    'source_chunk_id': record.get('source_chunk_id'),
                    'group_id': record.get('group_id'),
                }
                print(f'REJECTED {score:.2f} {record["entity_key"]} -> {log_path.name}')
                if not args.dry_run:
                    with open(log_path, 'a', encoding='utf-8') as handle:
                        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
                    await driver.execute_query(
                        "MATCH (pr:PendingReview {entity_key: $k}) SET pr.status='rejected', pr.resolved_at=$now",
                        k=record['entity_key'],
                        now=_now(),
                    )
                rejected += 1

        print(f'Summary: accepted={accepted} rejected={rejected} skipped={missing_entity}')
        return 0
    finally:
        await driver.close()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
