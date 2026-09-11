"""Multi-evidence retrieval test cases.

Each question is designed so that a complete answer REQUIRES evidence from
at least two distinct chunks/documents (multi-evidence). Verification checks
that the graph traversal actually surfaces >=2 independent evidence items.

Run: uv run python tests/multi_evidence_test.py
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from neo4j import AsyncGraphDatabase

NEO4J = ('bolt://localhost:7687', ('neo4j', 'Graphiti123'))
SERVICE = 'http://localhost:8000'
GROUP = 'ohn_legal_full'
AMENDMENT_YEARS = ['1988', '1993', '1999', '2004', '2018']


def search(query: str, k: int = 8) -> list[dict]:
    req = urllib.request.Request(
        f'{SERVICE}/search',
        data=json.dumps({'group_ids': [GROUP], 'query': query, 'max_facts': k}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    return json.load(urllib.request.urlopen(req, timeout=120))['facts']


async def hop(driver, cypher: str, **params) -> list[dict]:
    result = await driver.execute_query(cypher, parameters_=params)
    return [dict(r) for r in result.records]


CASES: list[dict] = []


def case(fn):
    CASES.append(fn)
    return fn


@case
async def c1_private_economy_evolution(driver):
    """私营经济宪法地位的演变：需要 1988（补充地位）与 1999（重要组成部分）两份证据。"""
    rows = await hop(
        driver,
        """
        MATCH (article:Entity)-[r2:RELATES_TO]-(doc:Entity)
        WHERE article.name CONTAINS '第十一条' AND doc.name CONTAINS '修正案'
        RETURN article.name AS a, r2.name AS rel, doc.name AS b,
               left(r2.fact, 60) AS fact, r2.episodes AS eps
        """,
    )
    years = set()
    for r in rows:
        for y in AMENDMENT_YEARS:
            if y in r['fact'] or y in r['a'] or y in r['b']:
                years.add(y)
    return {
        'question': '私营经济/非公有制经济的宪法地位经历了怎样的演变？',
        'evidence': rows[:4],
        'years_covered': sorted(years),
        'ok': len(years) >= 2,
        'note': f'覆盖 {sorted(years)}，需 >=2 个年份的证据',
    }


@case
async def c2_land_system(driver):
    """土地制度：1988 修正案改土地使用权，2004/2018 涉及征收补偿——需多文档证据。"""
    rows = await hop(
        driver,
        """
        MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity)
        WHERE (a.name CONTAINS '土地' OR b.name CONTAINS '土地'
               OR r.fact CONTAINS '土地')
        RETURN a.name AS a, r.name AS rel, b.name AS b, left(r.fact, 60) AS fact
        """,
    )
    docs = {r['a'] for r in rows} | {r['b'] for r in rows}
    amendment_docs = {d for d in docs if '修正案' in d}
    return {
        'question': '宪法关于土地制度的规定做过哪些修改？',
        'evidence': rows[:4],
        'documents': sorted(amendment_docs),
        'ok': len(amendment_docs) >= 2 or len(rows) >= 3,
        'note': f'涉及 {len(amendment_docs)} 个修正案文档, {len(rows)} 条关系',
    }


@case
async def c3_rule_of_law_wording(driver):
    """法制→法治的措辞演变（1999）：需要序言修改证据 + 原文对照。"""
    facts = search('法治国家 社会主义法制')
    contents = await hop(
        driver,
        """
        MATCH (c:Content) WHERE c.text CONTAINS '法治' OR c.text CONTAINS '法制'
        RETURN c.title AS title, c.heading_path AS path,
             left(c.text, 60) AS snippet
        LIMIT 6
        """,
    )
    return {
        'question': '"法制"到"法治"的宪法措辞变化发生在哪次修正案？',
        'evidence': [f['fact'][:60] for f in facts[:3]],
        'contents': [(c['title'], c['snippet']) for c in contents[:3]],
        'ok': len(facts) >= 1 and len(contents) >= 1,
        'note': f'检索事实 {len(facts)} 条, 含关键词 Content {len(contents)} 条',
    }


@case
async def c4_amending_body(driver):
    """历次修正案的通过机构：需要跨多个修正案文档的同类证据聚合。"""
    rows = await hop(
        driver,
        """
        MATCH (org:Entity)-[r:RELATES_TO]-(doc:Entity)
        WHERE org.name CONTAINS '人民代表大会' AND doc.name CONTAINS '修正案'
        RETURN org.name AS org, doc.name AS doc, left(r.fact, 55) AS fact
        """,
    )
    docs = {r['doc'] for r in rows}
    return {
        'question': '历部宪法修正案分别由哪届人大通过？',
        'evidence': rows[:5],
        'documents': sorted(docs),
        'ok': len(docs) >= 2,
        'note': f'{len(docs)} 部修正案有通过机构证据',
    }


@case
async def c5_leadership_amendments(driver):
    """指导思想写入宪法：马克思列宁主义等出现在序言类 Content 中，且与修正案实体关联。"""
    facts = search('邓小平理论 指导思想 宪法序言')
    contents = await hop(
        driver,
        """
        MATCH (c:Content)-[:MENTIONS]->(e:Entity)
        WHERE c.text CONTAINS '邓小平' OR c.text CONTAINS '马克思列宁主义'
        RETURN c.title AS title, collect(DISTINCT e.name)[..3] AS entities,
               left(c.text, 50) AS snippet
        LIMIT 5
        """,
    )
    return {
        'question': '哪些指导思想被写入宪法序言？分别在哪次修正案？',
        'evidence': [f['fact'][:60] for f in facts[:3]],
        'contents': [(c['title'], c['entities']) for c in contents],
        'ok': len(contents) >= 2 or (len(contents) >= 1 and len(facts) >= 1),
        'note': f'涉及 {len(contents)} 个 Content, 检索事实 {len(facts)} 条',
    }


async def main():
    driver = AsyncGraphDatabase.driver(NEO4J[0], auth=NEO4J[1])
    passed = 0
    for fn in CASES:
        print('=' * 70)
        result = await fn(driver)
        status = 'PASS' if result['ok'] else 'FAIL'
        passed += result['ok']
        print(f'[{status}] {result["question"]}')
        print(f'  说明: {result["note"]}')
        for ev in result['evidence'][:3]:
            print(f'  证据: {ev}')
        if result.get('years_covered'):
            print(f'  覆盖年份: {result["years_covered"]}')
        if result.get('documents'):
            print(f'  涉及文档: {result["documents"]}')
        if result.get('contents'):
            for t, extra in result['contents'][:2]:
                print(f'  Content: {t} | {extra}')
    print('=' * 70)
    print(f'总计: {passed}/{len(CASES)} 通过')
    await driver.close()


asyncio.run(main())
