"""Full-corpus ingestion with resume support.

Stages:
  1. Rule layer: Document/Section/Content hierarchy for the whole JSONL.
  2. Ontology-constrained extraction via the graph service, in batches.
     Resume-safe: a Content counts as done when its episode_uuid is set.
  3. Bridge Content-[:MENTIONS]->Entity, remove internal Episodic nodes.
  4. Resolve PendingReview (auto-accept >=0.85, reject+log below).

Usage:
    uv run python ingestion/full_ingest.py                 # full run / resume
    uv run python ingestion/full_ingest.py --batch-size 10 # default 10
    uv run python ingestion/full_ingest.py --max N         # cap chunks (test)
Progress is appended to logs/full_ingest.log; failures are retried once at end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from neo4j import AsyncGraphDatabase

from ingestion.hierarchy_builder import HierarchyBuilder, read_jsonl

CHUNKS_DEFAULT = r'G:\实习\律所RAG\OHN-GraphRAG_chunks_test\chunks.jsonl'
LOG = Path(__file__).parent.parent / 'logs' / 'full_ingest.log'


def log(msg: str) -> None:
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as handle:
        handle.write(line + '\n')


def post(path: str, payload: dict, timeout: int = 3600):
    import urllib.request

    req = urllib.request.Request(
        f'http://localhost:8000{path}',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


async def pending_chunk_ids(driver, records: list[dict]) -> list[dict]:
    """Records whose Content has no episode_uuid yet (not extracted)."""
    done = set()
    result = await driver.execute_query(
        'MATCH (c:Content) WHERE c.episode_uuid IS NOT NULL RETURN c.chunk_id AS id'
    )
    for r in result.records:
        done.add(r['id'])
    todo = []
    for rec in records:
        chunk_id = rec['metadata'].get('chunk_id')
        if chunk_id and chunk_id not in done:
            todo.append(rec)
    return todo


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--chunks', default=CHUNKS_DEFAULT)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--workers', type=int, default=4, help='concurrent extraction batches')
    parser.add_argument('--max', type=int, default=None, help='cap number of chunks')
    parser.add_argument('--neo4j-password', default='Graphiti123')
    parser.add_argument('--group-id', default='ohn_legal_full')
    parser.add_argument('--skip-resolver', action='store_true')
    args = parser.parse_args()

    records = read_jsonl(args.chunks, limit=args.max)
    log(f'Loaded {len(records)} chunks total')

    driver = AsyncGraphDatabase.driver(
        'bolt://localhost:7687', auth=('neo4j', args.neo4j_password)
    )
    try:
        # Stage 1: hierarchy (idempotent, MERGE-based).
        builder = HierarchyBuilder(driver)
        await builder.ensure_indexes()
        t0 = time.time()
        stats = await builder.build_from_records(records)
        log(f'Hierarchy built: {stats} in {time.time() - t0:.0f}s')

        # Stage 2: extraction, resumable.
        todo = await pending_chunk_ids(driver, records)
        log(f'{len(todo)} chunks remaining to extract')
        failed: list[dict] = []
        t0 = time.time()
        done_count = 0
        import urllib.error

        semaphore = asyncio.Semaphore(args.workers)

        def to_message(rec: dict) -> dict:
            return {
                'name': f"{rec['metadata'].get('document_title', 'chunk')}#{rec['metadata'].get('chunk_index', '')}",
                'content': rec['page_content'],
                'role_type': 'user',
                'role': rec['metadata'].get('document_title', 'document'),
                'source_description': f"chunk_id={rec['metadata'].get('chunk_id', '')}",
            }

        async def run_batch(batch: list[dict], label: str) -> None:
            nonlocal done_count
            async with semaphore:
                try:
                    await asyncio.to_thread(
                        post, '/messages', {'group_id': args.group_id, 'messages': [to_message(r) for r in batch]}
                    )
                    done_count += len(batch)
                    rate = done_count / max(time.time() - t0, 1)
                    eta = (len(todo) - done_count) / max(rate, 0.01) / 60
                    log(f'{label}: {done_count}/{len(todo)} done, {rate:.2f} chunks/s, ETA {eta:.0f} min')
                except urllib.error.HTTPError as exc:
                    if len(batch) > 1:
                        # Split the batch so one bad chunk cannot sink the rest.
                        mid = len(batch) // 2
                        await run_batch(batch[:mid], f'{label}a')
                        await run_batch(batch[mid:], f'{label}b')
                    else:
                        log(f'{label} FAILED: {exc}')
                        failed.extend(batch)
                except Exception as exc:  # noqa: BLE001
                    log(f'{label} FAILED: {exc}')
                    failed.extend(batch)

        batches = [todo[i : i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
        await asyncio.gather(*(run_batch(batch, f'batch{i + 1}') for i, batch in enumerate(batches)))

        # One retry pass for failed batches.
        for rec in failed:
            try:
                post(
                    '/messages',
                    {
                        'group_id': args.group_id,
                        'messages': [
                            {
                                'name': f"{rec['metadata'].get('document_title', 'chunk')}#{rec['metadata'].get('chunk_index', '')}",
                                'content': rec['page_content'],
                                'role_type': 'user',
                                'role': rec['metadata'].get('document_title', 'document'),
                                'source_description': f"chunk_id={rec['metadata'].get('chunk_id', '')}",
                            }
                        ],
                    },
                )
                log(f"retry ok: {rec['metadata'].get('chunk_id')}")
            except Exception as exc:  # noqa: BLE001
                log(f"retry FAILED: {rec['metadata'].get('chunk_id')}: {exc}")

        # Stage 3: bridge mentions + cleanup episodic.
        bridged = await builder.bridge_mentions_from_episodes()
        deleted = await builder.cleanup_episodic()
        log(f'Bridged {bridged} Content MENTIONS; removed {deleted} Episodic nodes')

        # Stage 3.5: relation normalization (validate + materialize typed relations).
        from ingestion.relation_normalizer import normalize as normalize_relations
        from graphiti_core.utils.ontology_loader import load_ontology

        ontology = load_ontology()
        if ontology is not None:
            rstats = await normalize(driver, ontology)
            log(f'Relation normalization: {rstats}')
        else:
            log('Relation normalization skipped: ontology unavailable')

        done = await pending_chunk_ids(driver, records)
        log(f'DONE. extracted={len(records) - len(done)}/{len(records)}, failed={len(done)}')
    finally:
        await driver.close()

    # Stage 4: pending review resolution.
    if not args.skip_resolver:
        import subprocess

        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'resolve_pending.py')],
            capture_output=True,
            text=True,
        )
        log(f'Resolver: {result.stdout.strip()[-300:]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
