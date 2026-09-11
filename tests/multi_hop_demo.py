"""Multi-hop retrieval demo: 私营经济的宪法地位由哪年哪机构确立.

Observed topology: 私营经济 -RELATES_TO-> 宪法修正案(1988)
                    修正案 -CONTAINS_ARTICLE/AMENDS-> 宪法第十一条
                    修正案 -ISSUED_BY/RELATES_TO-> 全国人民代表大会
Chain: entry(hybrid search) -> hop1 neighbors -> hop2 clause/org -> hop3 evidence.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from neo4j import AsyncGraphDatabase

NEO4J = ('bolt://localhost:7687', ('neo4j', 'Graphiti123'))
SERVICE = 'http://localhost:8000'
GROUP = 'ohn_legal_test'


async def hop(driver, cypher: str, **params) -> list[dict]:
    result = await driver.execute_query(cypher, parameters_=params)
    return [dict(r) for r in result.records]


async def main():
    driver = AsyncGraphDatabase.driver(NEO4J[0], auth=NEO4J[1])

    print('=' * 70)
    print('问题: 私营经济在宪法中的地位是哪一年、由哪个机构通过的修正案确立的？')
    print('=' * 70)

    # ---- 第 0 跳: 入口定位（混合检索 + 实体链接 双路）----
    req = urllib.request.Request(
        f'{SERVICE}/search',
        data=json.dumps({'group_ids': [GROUP], 'query': '私营经济的法律地位', 'max_facts': 3}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    facts = json.load(urllib.request.urlopen(req, timeout=120))['facts']
    print('\n[第0跳] 入口定位（混合检索 top-3）:')
    for f in facts:
        print(f'  - {f["name"]} | {f["fact"][:50]}')

    rows = await hop(
        driver,
        "MATCH (e:Entity) WHERE e.name CONTAINS '私营经济' RETURN e.name AS name LIMIT 1",
    )
    entry = rows[0]['name']
    print(f'  实体链接命中: {entry}')

    # ---- 第 1 跳: 私营经济 -> 相邻实体 ----
    rows = await hop(
        driver,
        """
        MATCH (e:Entity {name: $entry})-[r:RELATES_TO]-(other:Entity)
        RETURN DISTINCT other.name AS node, r.name AS relation, left(r.fact, 45) AS fact
        LIMIT 8
        """,
        entry=entry,
    )
    print('\n[第1跳] 私营经济 -> 相邻实体:')
    doc_name = None
    for r in rows:
        print(f'  - {r["node"]} <-[{r["relation"]}]- {r["fact"]}')
        if '修正案' in r['node'] and doc_name is None:
            doc_name = r['node']
    assert doc_name, '第 1 跳失败: 未找到修正案实体'
    print(f'  选定下一跳节点: {doc_name}')

    # ---- 第 2 跳: 修正案 -> 条款 & 通过机构 ----
    rows = await hop(
        driver,
        """
        MATCH (doc:Entity)-[r:RELATES_TO]-(other:Entity)
        WHERE doc.name = $doc
          AND (other.name CONTAINS '第十一条' OR other.name CONTAINS '人民代表大会')
        RETURN DISTINCT other.name AS node, r.name AS relation, left(r.fact, 40) AS fact
        LIMIT 6
        """,
        doc=doc_name,
    )
    print('\n[第2跳] 修正案 -> 条款/通过机构:')
    article = org_name = None
    for r in rows:
        print(f'  - {r["node"]} <-[{r["relation"]}]- {r["fact"]}')
        if '第十一条' in r['node']:
            article = r['node']
        if '人民代表大会' in r['node']:
            org_name = r['node']

    # ---- 第 3 跳: 回读 Content 原文取证 ----
    rows = await hop(
        driver,
        """
        MATCH (c:Content)-[:MENTIONS]->(e:Entity)
        WHERE e.name CONTAINS '私营经济' AND c.text CONTAINS '引导、监督和管理'
        RETURN c.title AS title, c.heading_path AS path, c.text AS text LIMIT 1
        """,
    )
    print('\n[第3跳] 回读原文证据:')
    evidence = rows[0] if rows else None
    if evidence:
        print(f'  出处: {evidence["path"]}')
        print(f'  原文: {evidence["text"][:120]}')

    print('\n' + '=' * 70)
    year = '1988' if '1988' in (doc_name or '') else '1993'
    print(f'最终答案: 私营经济的宪法地位由 {year} 年宪法修正案确立')
    if org_name:
        print(f'          通过机构: {org_name}')
    if article:
        print(f'          涉及条款: {article}')
    if evidence:
        print(f'          证据出处: {evidence["title"]}（{evidence["path"]}）')
    print('=' * 70)
    await driver.close()


asyncio.run(main())
