"""Live retrieval tests against the running graph service and Neo4j.

Covers the retrieval navigation chain from 落地设计 section 7:
  1. Graphiti hybrid search (fact layer recall via /retrieve/search)
  2. Entity entry-point lookup (entity linking)
  3. Relation traversal on the graph (hop between entities)
  4. Fall-back to Content full text (evidence read-back)

Prerequisites: graph service on :8000, Neo4j on :7687, the 10-chunk test
graph ingested with group_id 'ohn_legal_test'.

Run: uv run python tests/live_retrieval_test.py
"""

from __future__ import annotations

import json
import urllib.request

import pytest

SERVICE = 'http://localhost:8000'
GROUP_ID = 'ohn_legal_test'
NEO4J_URI = 'bolt://localhost:7687'
NEO4J_AUTH = ('neo4j', 'Graphiti123')

pytestmark = pytest.mark.integration


def _search(query: str, max_facts: int = 10) -> list[dict]:
    payload = json.dumps({'group_ids': [GROUP_ID], 'query': query, 'max_facts': max_facts})
    req = urllib.request.Request(
        f'{SERVICE}/search',
        data=payload.encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))['facts']


# ---------------------------------------------------------------- fact layer


@pytest.mark.asyncio
async def test_hybrid_search_finds_private_economy_facts():
    """混合检索：问私营经济应召回包含该词的事实边。"""
    facts = _search('私营经济的法律地位是什么')
    assert facts, 'expected non-empty fact results'
    hits = [f for f in facts if '私营经济' in f['fact']]
    assert hits, f'no fact mentions 私营经济: {[f["fact"][:40] for f in facts[:3]]}'
    # 每条事实必须能追溯到来源 episode（可回读原文的前提）
    for fact in facts:
        assert fact['episodes'], f'fact without episode provenance: {fact["fact"][:40]}'


@pytest.mark.asyncio
async def test_hybrid_search_finds_amendment_relation():
    """问修正案应召回 AMENDS/修改类事实。"""
    facts = _search('宪法修正案对宪法条款的修改')
    assert facts
    # The specific article cited varies run to run (LLM extraction variance);
    # require an amendment fact about some constitutional article.
    assert any(
        ('修改' in f['fact'] or 'AMENDS' in (f['name'] or '')) and '宪法第' in f['fact']
        for f in facts
    ), f'expected amendment fact: {[f["fact"][:40] for f in facts[:3]]}'


@pytest.mark.asyncio
async def test_hybrid_search_relevance_ordering():
    """无关查询不应污染：排名首位的事实应与查询语义相关。"""
    facts = _search('国家对私营经济实行什么管理')
    assert facts
    assert any('私营经济' in f['fact'] for f in facts[:5]), 'top-5 should be on-topic'


# ------------------------------------------------------------ graph traversal


async def _run_cypher(query: str, **params) -> list[dict]:
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    try:
        result = await driver.execute_query(query, parameters_=params)
        return [dict(record) for record in result.records]
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_entity_entry_point_lookup():
    """入口定位：实体链接能命中起始实体。"""
    rows = await _run_cypher(
        "MATCH (e:Entity) WHERE e.name CONTAINS $kw RETURN e.name AS name, e.entity_key AS key LIMIT 5",
        kw='私营经济',
    )
    assert rows, 'entity 私营经济 not found'
    assert all(r['key'] for r in rows), 'entity_key missing'


@pytest.mark.asyncio
async def test_relation_traversal_from_entity():
    """关系跳转：从实体一跳扩展出带语义的业务关系。"""
    rows = await _run_cypher(
        """
        MATCH (e:Entity)-[r:RELATES_TO]-(other:Entity)
        WHERE e.name CONTAINS '私营经济'
        RETURN other.name AS other, r.name AS relation, left(r.fact, 40) AS fact
        LIMIT 10
        """
    )
    assert rows, 'no business relations from 私营经济'
    assert any(r['relation'] in ('DEFINES', 'APPLIES_TO', 'EXECUTES', 'PROTECTS', 'AMENDS') for r in rows)


@pytest.mark.asyncio
async def test_defines_via_relation_property():
    """定向查询：按关系名属性找 DEFINES 关系。"""
    rows = await _run_cypher(
        """
        MATCH (a:Entity)-[r:RELATES_TO {name: 'DEFINES'}]->(b:Entity)
        RETURN a.name AS src, b.name AS tgt
        """
    )
    assert rows, 'no DEFINES relation found'
    # Source naming varies by run (宪法第十一条 / 宪法修正案...); accept any
    # DEFINES edge about the private-economy concept.
    facts = await _run_cypher(
        "MATCH ()-[r:RELATES_TO {name: 'DEFINES'}]->() RETURN r.fact AS f"
    )
    assert any('私营经济' in r['f'] for r in facts), 'expected DEFINES fact about 私营经济'


@pytest.mark.asyncio
async def test_fall_back_to_content_full_text():
    """回读原文：关系边的 episode 必须能落到 Content 全文。"""
    rows = await _run_cypher(
        """
        MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity)
        WHERE a.name CONTAINS '私营经济' AND size(r.episodes) > 0
        WITH r LIMIT 1
        UNWIND r.episodes AS ep
        MATCH (c:Content) WHERE c.episode_uuid = ep
        RETURN c.title AS title, c.text AS text, c.heading_path AS path
        """
    )
    assert rows, 'relation episode could not be resolved to Content'
    row = rows[0]
    assert row['text'], 'Content text is empty'
    assert '私营' in row['text'] or '宪法' in row['text']


@pytest.mark.asyncio
async def test_content_context_expansion_via_next():
    """上下文扩展：命中条款可沿 NEXT 读取后续内容。"""
    rows = await _run_cypher(
        """
        MATCH (c:Content)-[:NEXT]->(n:Content)
        RETURN c.title AS cur, n.title AS nxt LIMIT 3
        """
    )
    assert rows, 'no NEXT navigation edges'


@pytest.mark.asyncio
async def test_hierarchy_trace_to_document():
    """层级回溯：每个 Content 沿 CONTAINS 到 Document。"""
    rows = await _run_cypher(
        """
        MATCH (c:Content)
        WHERE NOT (c)<-[:CONTAINS*1..3]-(:Document)
        RETURN count(c) AS orphans
        """
    )
    assert rows[0]['orphans'] == 0


if __name__ == '__main__':
    import asyncio
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and asyncio.iscoroutinefunction(fn):
            try:
                asyncio.run(fn())
                print(f'PASS {name}')
            except AssertionError as exc:
                failed += 1
                print(f'FAIL {name}: {exc}')
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f'ERROR {name}: {type(exc).__name__}: {exc}')
    sys.exit(1 if failed else 0)
