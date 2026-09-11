"""Relation normalization layer (development spec sections 7.3 / 18.5).

Runs over all RELATES_TO edges (the raw LLM fact layer) and produces the
normalized layer, additively — the raw layer is never modified:

  1. Relation name not in the ontology whitelist  -> PendingReview(kind='relation').
  2. Whitelisted name, endpoints valid            -> materialize a typed
     relation (e.g. (:Article)-[:DEFINES {evidence_text, source_chunk_ids}]->
     (:LegalConcept)) so retrieval can traverse real relationship types.
  3. Whitelisted name but direction reversed      -> materialize the typed
     relation in the CORRECT direction (the raw edge keeps the LLM's
     original direction as provenance).
  4. Whitelisted name, endpoint types invalid in both directions ->
     PendingReview(kind='relation', reason='type mismatch ...').

Usage:
    uv run python ingestion/relation_normalizer.py            # run
    uv run python ingestion/relation_normalizer.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from neo4j import AsyncGraphDatabase

from graphiti_core.utils.ontology_loader import load_ontology

# PendingReview records produced here carry kind='relation' so the entity
# merge resolver (resolve_pending.py) skips them.


async def normalize(driver, ontology, dry_run: bool = False) -> dict:
    stats = {'total': 0, 'ok': 0, 'flipped': 0, 'pending': 0, 'unknown_name': 0}
    result = await driver.execute_query(
        """
        MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
        WHERE r.normalized IS NULL OR r.normalized = false
        RETURN a.uuid AS a_uuid, b.uuid AS b_uuid, r.uuid AS uuid,
               [l IN labels(a) WHERE l <> 'Entity'] AS a_labels,
               [l IN labels(b) WHERE l <> 'Entity'] AS b_labels,
               r.name AS name, r.fact AS fact, r.episodes AS episodes
        """
    )
    edges = [dict(rec) for rec in result.records]
    stats['total'] = len(edges)
    print(f'{len(edges)} raw relations to normalize')

    for edge in edges:
        src_types = edge['a_labels'] or ['UnknownEntity']
        tgt_types = edge['b_labels'] or ['UnknownEntity']
        # Try each specific type combination (entities may carry multiple labels).
        name = edge['name'] or ''
        if not ontology.relation_type_names:
            break

        if name not in ontology.relation_type_names:
            stats['unknown_name'] += 1
            stats['pending'] += 1
            if not dry_run:
                await _add_pending(driver, edge, f'unknown relation name: {name}')
            continue

        def allowed(a_labels, b_labels):
            return any(
                ontology.relation_allowed(name, sa, tb)
                for sa in (a_labels or ['UnknownEntity'])
                for tb in (b_labels or ['UnknownEntity'])
            )

        def allowed(a_labels, b_labels) -> bool | None:
            """True/False when types are known, None when either side is untyped."""
            src_known = [l for l in (a_labels or []) if l != 'UnknownEntity']
            tgt_known = [l for l in (b_labels or []) if l != 'UnknownEntity']
            if not src_known or not tgt_known:
                return None  # untyped endpoint: cannot validate either way
            return any(
                ontology.relation_allowed(name, sa, tb) for sa in src_known for tb in tgt_known
            )

        forward = allowed(src_types, tgt_types)
        backward = allowed(tgt_types, src_types)
        untyped = forward is None and backward is None

        if not untyped and forward is False and backward is False:
            stats['pending'] += 1
            if not dry_run:
                await _add_pending(
                    driver,
                    edge,
                    f'type mismatch for {name}: '
                    f'{src_types} -> {tgt_types} invalid in both directions',
                )
            continue

        flipped = backward is True and forward is not True
        stats['flipped' if flipped else 'ok'] += 1
        if not dry_run:
            await _materialize(driver, edge, name, flipped)

        if not dry_run:
            await driver.execute_query(
                'MATCH ()-[r:RELATES_TO {uuid: $uuid}]->() SET r.normalized = true',
                uuid=edge.get('uuid'),
            )
        continue
    return stats


async def _materialize(driver, edge: dict, name: str, flipped: bool) -> None:
    """Create/merge the typed relation in the correct direction."""
    src, tgt = ('b_uuid', 'a_uuid') if flipped else ('a_uuid', 'b_uuid')
    episodes = edge.get('episodes') or []
    await driver.execute_query(
        f"""
        MATCH (a:Entity {{uuid: ${src}}}), (b:Entity {{uuid: ${tgt}}})
        MERGE (a)-[t:{name}]->(b)
        ON CREATE SET t.evidence_text = $fact,
                      t.source_chunk_ids = $episodes,
                      t.normalized = true
        ON MATCH SET t.source_chunk_ids =
                      CASE WHEN $episodes[0] IN coalesce(t.source_chunk_ids, [])
                           THEN t.source_chunk_ids
                           ELSE coalesce(t.source_chunk_ids, []) + $episodes END
        """,
        **{src: edge['a_uuid'], tgt: edge['b_uuid']},
        fact=edge.get('fact') or '',
        episodes=episodes,
    )


async def _add_pending(driver, edge: dict, reason: str) -> None:
    entity_key = f'relation:{edge["a_uuid"]}:{edge["b_uuid"]}:{edge["name"]}'
    await driver.execute_query(
        """
        MERGE (pr:PendingReview {entity_key: $key})
        SET pr.kind = 'relation',
            pr.status = 'pending',
            pr.reason = $reason,
            pr.context = $fact,
            pr.created_at = datetime()
        """,
        key=entity_key,
        reason=reason,
        fact=edge.get('fact') or '',
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--neo4j-password', default='Graphiti123')
    args = parser.parse_args()

    ontology = load_ontology()
    if ontology is None:
        print('ontology unavailable; aborting')
        return 1

    driver = AsyncGraphDatabase.driver(
        'bolt://localhost:7687', auth=('neo4j', args.neo4j_password)
    )
    try:
        stats = await normalize(driver, ontology, dry_run=args.dry_run)
        label = '(dry-run) ' if args.dry_run else ''
        print(
            f'{label}normalized: total={stats["total"]} ok={stats["ok"]} '
            f'flipped={stats["flipped"]} unknown_name={stats["unknown_name"]} '
            f'pending={stats["pending"]}'
        )
        return 0
    finally:
        await driver.close()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
