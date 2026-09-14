"""OHN-GraphRAG 检索测试集（基于前 130 条 chunk 语料）

五类用例：
  A. 事实问答（1 跳可达）
  B. 多跳推理（答案需要 2-3 跳路径）
  C. 多证据聚合（答案需要跨文档 >=2 份证据）
  D. 多 Content 枚举（需要返回多篇原文）
  E. 鲁棒性（噪声拒绝 / 入口停用词 / 预算截断）

运行：uv run python tests/retrieval_test_suite.py
前置：graph 服务 :8000 健康，图谱含宪法修正案语料（group ohn_legal_full）
"""

from __future__ import annotations

import json
import urllib.request

API = 'http://127.0.0.1:8000'
GROUP = 'ohn_legal_full'

CASES = [
    # ---------------- A. 事实问答 ----------------
    {
        'id': 'A1',
        'type': '事实问答',
        'question': '私营经济由谁引导、监督和管理？',
        'hops': 2,
        'must_contain_facts': ['引导'],
        'must_contain_entities': ['国家'],
    },
    {
        'id': 'A2',
        'type': '事实问答',
        'question': '宪法第十一条定义了什么？',
        'hops': 2,
        'must_contain_facts': ['私营经济'],
        'must_contain_entities': ['私营经济'],
    },
    # ---------------- B. 多跳推理 ----------------
    {
        'id': 'B1',
        'type': '多跳推理',
        'question': '私营经济在宪法中的地位是哪一年、由哪个机构通过的修正案确立的？',
        'hops': 3,
        'must_contain_facts': ['私营经济'],
        'must_contain_entities': ['第七届全国人民代表大会'],
        'expect_years': ['1988'],
    },
    {
        'id': 'B2',
        'type': '多跳推理',
        'question': '农村集体经济组织的经营形式在宪法中是如何演变的？相关修正案是由哪届全国人大通过的？',
        'hops': 3,
        'must_contain_facts': ['家庭联产', '集体经济'],
        'must_contain_entities': ['全国人民代表大会'],
    },
    # ---------------- C. 多证据聚合 ----------------
    {
        'id': 'C1',
        'type': '多证据聚合',
        'question': '宪法关于土地制度做过哪些修改？',
        'hops': 2,
        'must_contain_facts': ['土地'],
        'min_evidence_docs': 2,
        'expect_years': ['1988', '2004'],
    },
    {
        'id': 'C2',
        'type': '多证据聚合',
        'question': '历部宪法修正案分别由哪届全国人民代表大会通过？',
        'hops': 2,
        'must_contain_entities': ['全国人民代表大会'],
        'min_evidence_docs': 3,
    },
    {
        'id': 'C3',
        'type': '多证据聚合',
        'question': '宪法修正案的指导思想有哪些变化？',
        'hops': 2,
        'must_contain_facts': ['思想'],
        'expect_years': ['1999', '2004', '2018'],
    },
    # ---------------- D. 多 Content 枚举 ----------------
    {
        'id': 'D1',
        'type': '多Content枚举',
        'question': '1988年和1993年的宪法修正案分别修改了宪法哪些条款？每处修改的原文内容是什么？',
        'hops': 2,
        'must_be_enumeration': True,
        'min_contents': 3,
        'min_evidence_docs': 2,
    },
    {
        'id': 'D2',
        'type': '多Content枚举',
        'question': '2004年和2018年的宪法修正案分别涉及宪法的哪些条款？',
        'hops': 2,
        'must_be_enumeration': False,  # 信号词「分别」命中即枚举；不强制
        'min_contents': 2,
        'min_evidence_docs': 2,
    },
    # ---------------- E. 鲁棒性 ----------------
    {
        'id': 'E1',
        'type': '鲁棒性-噪声',
        'question': '小型非营运二手车异地交易登记是怎么规定的？',
        'hops': 2,
        'should_not_be_all_noise': True,  # 图里没有该法规实体，不应返回大量低相关事实
    },
    {
        'id': 'E2',
        'type': '鲁棒性-入口停用词',
        'question': '全国人民代表大会的作用是什么？',
        'hops': 2,
        'must_contain_entities': ['全国人民代表大会'],
        'forbid_entries': ['全国'],
    },
    {
        'id': 'E3',
        'type': '鲁棒性-预算截断',
        'question': '私营经济的宪法地位是哪一年、由哪个机构通过的修正案确立的？',
        'hops': 3,
        'max_tokens': 600,
        'budget_must_hold': True,
    },
]


def run_query(question: str, hops: int, max_tokens: int = 4000) -> dict:
    req = urllib.request.Request(
        f'{API}/search/graph',
        data=json.dumps(
            {'group_ids': [GROUP], 'question' and 'query': question,
             'max_tokens': max_tokens, 'max_hops': hops}
        ).encode(),
        headers={'Content-Type': 'application/json'},
    )
    return json.load(urllib.request.urlopen(req, timeout=180))


def check(case: dict, r: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    facts_text = ' '.join((f.get('fact') or '') + (f.get('src') or '') + (f.get('tgt') or '')
                          for f in r.get('facts', []))
    entry_names = [e['name'] for e in r.get('entries', [])]

    for frag in case.get('must_contain_facts', []):
        if frag not in facts_text:
            problems.append(f'事实未命中关键词「{frag}」')
    for ent in case.get('must_contain_entities', []):
        in_entries = any(ent in n for n in entry_names)
        in_facts = any(ent in (f.get('src') or '') + (f.get('tgt') or '') for f in r.get('facts', []))
        if not (in_entries or in_facts):
            problems.append(f'未命中实体「{ent}」')
    for forbidden in case.get('forbid_entries', []):
        if forbidden in entry_names:
            problems.append(f'停用词实体「{forbidden}」混入入口')

    years = case.get('expect_years', [])
    if years:
        all_text = facts_text + json.dumps(r.get('contents', []), ensure_ascii=False)
        missing = [y for y in years if y not in all_text]
        if missing:
            problems.append(f'缺少年份证据 {missing}（多证据不完整）')

    if case.get('must_be_enumeration') and r.get('mode') != 'enumeration':
        problems.append('未进入枚举模式')
    if r.get('mode') == 'enumeration':
        n = sum(len(g['clauses']) for g in r.get('groups', []))
        if n < 2:
            problems.append(f'枚举项过少: {n}')

    if case.get('min_contents') and len(r.get('contents', [])) < case['min_contents']:
        problems.append(f"原文篇数 {len(r.get('contents', []))} < 要求 {case['min_contents']}")

    if case.get('min_evidence_docs'):
        docs = {(c.get('path') or '?').split(' > ')[0] for c in r.get('contents', [])}
        # facts 也可作为证据来源计数
        fact_docs = {f.get('src', '') for f in r.get('facts', [])} | {f.get('tgt', '') for f in r.get('facts', [])}
        total_docs = docs | {d[:20] for d in fact_docs if d}
        if len(docs) < case['min_evidence_docs']:
            problems.append(f"证据文档 {len(docs)} < 要求 {case['min_evidence_docs']}")

    if case.get('budget_must_hold'):
        if r.get('tokens_used', 0) > case.get('max_tokens', 10 ** 9):
            problems.append(f"预算超支: {r['tokens_used']}")

    if case.get('should_not_be_all_noise'):
        total = len(r.get('facts', []))
        relevant = sum(1 for f in r.get('facts', [])
                       if any(kw in (f.get('fact') or '') + (f.get('src') or '') + (f.get('tgt') or '')
                              for kw in ('二手车', '机动车')))
        if total > 0 and relevant / total < 0.5:
            problems.append(f'相关率过低: {relevant}/{total}')

    noise = sum(1 for f in r.get('facts', [])
                if '二手车' in (f.get('fact') or '') + (f.get('src') or '') + (f.get('tgt') or ''))
    if case['id'] not in ('E1',) and noise > 0:
        problems.append(f'混入 {noise} 条二手车噪声')

    return len(problems) == 0, problems


def main():
    passed = 0
    results = []
    for case in CASES:
        try:
            r = run_query(case['question'], case['hops'], case.get('max_tokens', 4000))
            ok, problems = check(case, r)
        except Exception as exc:  # noqa: BLE001
            ok, problems = False, [f'{type(exc).__name__}: {exc}']
        passed += ok
        status = 'PASS' if ok else 'FAIL'
        results.append((status, case, problems))
        print(f'[{status}] {case["id"]} {case["type"]} | {case["question"][:30]}')
        for p in problems:
            print(f'       ✗ {p}')
    print('=' * 60)
    print(f'总计: {passed}/{len(CASES)} 通过')
    return 0 if passed == len(CASES) else 1


if __name__ == '__main__':
    raise SystemExit(main())
