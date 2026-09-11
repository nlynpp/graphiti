"""OHN-GraphRAG ingestion orchestrator.

Pipeline (development spec section 3):

    chunks.jsonl
    -> rule layer: Document / Section / Content + CONTAINS / NEXT / PREVIOUS
    -> LLM ontology-constrained extraction via the graph service (entity_types whitelist)
    -> bridge Content-[:MENTIONS]->Entity from episode MENTIONS
    -> validation statistics (spec section 13)

Usage:
    python ingestion/orchestrate_ingest.py [--limit 10] [--keep]
    --keep skips the /clear call (incremental ingestion)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from neo4j import AsyncGraphDatabase

from ingestion.hierarchy_builder import HierarchyBuilder, read_jsonl

CHUNKS_DEFAULT = r'G:\实习\律所RAG\OHN-GraphRAG_chunks_test\chunks.jsonl'
NEO4J_URI = 'bolt://localhost:7687'
NEO4J_AUTH = None  # filled from env in main()

SERVICE = 'http://localhost:8000'
GROUP_ID = 'ohn_legal_test'


async def build_hierarchy(records: list[dict], uri: str, auth) -> dict[str, int]:
    driver = AsyncGraphDatabase.driver(uri, auth=auth)
    try:
        builder = HierarchyBuilder(driver)
        await builder.ensure_indexes()
        return await builder.build_from_records(records)
    finally:
        await driver.close()


async def bridge_mentions(uri: str, auth, cleanup: bool = True) -> int:
    driver = AsyncGraphDatabase.driver(uri, auth=auth)
    try:
        builder = HierarchyBuilder(driver)
        bridged = await builder.bridge_mentions_from_episodes()
        if cleanup:
            deleted = await builder.cleanup_episodic()
            print(f'Removed {deleted} internal Episodic nodes')
        return bridged
    finally:
        await driver.close()


async def validate_graph(uri: str, auth) -> dict:
    driver = AsyncGraphDatabase.driver(uri, auth=auth)
    checks = {}
    try:
        async def rows(query: str) -> list[dict]:
            result = await driver.execute_query(query)
            return [dict(r) for r in result.records]

        checks['labels'] = await rows(
            'MATCH (n) RETURN labels(n) AS labels, count(n) AS count ORDER BY labels'
        )
        checks['orphan_entities'] = await rows(
            'MATCH (e:Entity) WHERE NOT ()-[:MENTIONS]->(e) RETURN count(e) AS c'
        )
        checks['contents_without_doc'] = await rows(
            'MATCH (c:Content) WHERE NOT (c)<-[:CONTAINS*1..3]-(:Document) RETURN count(c) AS c'
        )
        checks['mentions_without_source'] = await rows(
            'MATCH ()-[m:MENTIONS]->() WHERE m.source_chunk_id IS NULL RETURN count(m) AS c'
        )
        checks['entity_types'] = await rows(
            'MATCH (e:Entity) RETURN [l IN labels(e) WHERE l <> "Entity"][0] AS t, count(*) AS c ORDER BY c DESC'
        )
    finally:
        await driver.close()
    return checks


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--keep', action='store_true', help='skip clearing the graph')
    parser.add_argument('--chunks', default=CHUNKS_DEFAULT)
    parser.add_argument('--neo4j-uri', default=NEO4J_URI)
    parser.add_argument('--neo4j-user', default='neo4j')
    parser.add_argument('--neo4j-password', default='password')
    parser.add_argument('--group-id', default=GROUP_ID)
    args = parser.parse_args()

    import os

    auth = (
        os.environ.get('NEO4J_USER', args.neo4j_user),
        os.environ.get('NEO4J_PASSWORD', args.neo4j_password),
    )

    records = read_jsonl(args.chunks, limit=args.limit)
    print(f'Loaded {len(records)} chunks')

    if not args.keep:
        _post('/clear', {})
        print('Graph cleared')

    # 1. Rule layer: hierarchy skeleton.
    stats = await build_hierarchy(records, args.neo4j_uri, auth)
    print(f'Hierarchy: {stats}')

    # 2. LLM ontology-constrained extraction via the graph service.
    messages = []
    for rec in records:
        meta = rec['metadata']
        messages.append(
            {
                'name': f"{meta.get('document_title', 'chunk')}#{meta.get('chunk_index', '')}",
                'content': rec['page_content'],
                'role_type': 'user',
                'role': meta.get('document_title', 'document'),
                'source_description': f"chunk_id={meta.get('chunk_id', '')}",
            }
        )
    result = _post(
        '/messages', {'group_id': args.group_id, 'messages': messages}
    )
    print(f'Extraction: {result}')

    # 3. Bridge Content-[:MENTIONS]->Entity.
    bridged = await bridge_mentions(args.neo4j_uri, auth)
    print(f'Bridged {bridged} Content MENTIONS')

    # 4. Validation statistics (spec section 8/13).
    checks = await validate_graph(args.neo4j_uri, auth)
    print(json.dumps(checks, ensure_ascii=False, indent=2, default=str))
    return 0


def _post(path: str, payload: dict):
    import urllib.request

    req = urllib.request.Request(
        SERVICE + path,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode('utf-8'))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
