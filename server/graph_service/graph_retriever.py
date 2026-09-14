"""Graph-first retriever: fact-first entry -> relation graph search -> slot-driven packing.

Combination of Graphiti primitives and LightRAG-style packing (zero LLM):

  1. Entry localization (fact-first, upstream Graphiti pattern): hybrid fact
     search (BM25 + vector + RRF, zero LLM) ranks facts; endpoint entities of
     the top facts become entry candidates. Literal name matches in the
     question add a bonus. Generic stopwords never become entries.
  2. Graph search: expand 1..max_hops along raw RELATES_TO facts (episodes
     provenance lives there), undirected, self-loops and stopword nodes skipped,
     ranked by question n-gram relevance then hub degree.
  3. Slot-driven packing: facts matching intent slots (ISSUED_BY for "which
     body passed", DEFINES for "status", ...) are force-packed within a 60%
     quota; remaining budget by relevance. Contents are read back with
     slot-priority ordering and 500-char excerpts.
  4. Enumeration mode: aggregation questions ("which articles respectively/all")
     switch to exhaustive listing per document, deduplicated by fact text and
     source chunk.

All queries are static parameterized templates — nothing is handwritten per
round.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _tokens(text: str | None) -> int:
    if not text:
        return 0
    return len(text)


def _is_cjk(ch: str) -> bool:
    return '\u4e00' <= ch <= '\u9fff'


def _year_fragments(name: str) -> list[str]:
    """Extract 4-digit years from a name."""
    return re.findall(r'\d{4}', name)


# Generic high-frequency names that match almost any question fragment —
# they must never become entry points or expansion seeds.
_ENTRY_STOPWORDS = {
    '全国', '国家', '我国', '地方', '社会', '人民', '政府', '中央', '地区',
    '单位', '组织', '机构', '部门', '个人', '有关', '其他', '相关',
}

# Enumeration intent signals: aggregation questions need exhaustive queries.
_ENUM_SIGNALS = [
    '分别', '所有', '全部', '每一', '一一', '列举', '历届', '历次', '各自',
    '哪些条款', '哪些修改', '哪些变化', '有哪些', '分别修改', '都修改',
]

# Intent slots: question signals -> relations whose facts MUST be packed.
_SLOT_SIGNALS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (('哪个机构', '哪届人大', '哪届会议', '由谁通过', '谁通过', '通过机构', '哪个机关'), ('ISSUED_BY',)),
    (('哪些条款', '每处', '哪些条文', '修改了宪法哪些', '涉及哪些'), ('CONTAINS_ARTICLE', 'AMENDS')),
    (('地位', '是什么', '定义', '概念', '含义'), ('DEFINES',)),
    (('条件', '前提'), ('REQUIRES',)),
    (('例外', '除外'), ('EXCEPTS',)),
    (('处罚', '罚则', '刑事责任'), ('HAS_PENALTY',)),
    (('数额', '金额'), ('HAS_AMOUNT',)),
    (('适用', '适用于'), ('APPLIES_TO',)),
]


def detect_enumeration(question: str) -> tuple[bool, str]:
    for signal in _ENUM_SIGNALS:
        if signal in question:
            return True, signal
    return False, ''


def extract_slots(question: str) -> list[str]:
    """Rule-based intent slots: relations the answer requires."""
    slots: list[str] = []
    for signals, relations in _SLOT_SIGNALS:
        if any(sig in question for sig in signals):
            for rel in relations:
                if rel not in slots:
                    slots.append(rel)
    return slots


class GraphRetriever:
    def __init__(self, driver, group_ids: list[str] | None = None, graphiti=None):
        self.driver = driver
        self.group_ids = group_ids
        # Graphiti client for fact-first entry localization (hybrid search).
        self.graphiti = graphiti

    async def _run(self, query: str, **params) -> list[dict]:
        result = await self.driver.execute_query(query, params=params)
        return [dict(r) for r in result.records]

    # ---------------- 1. entry localization (fact-first) ----------------

    async def find_entries(self, question: str, max_entries: int = 3) -> tuple[list[dict], list[str]]:
        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}

        # 1) fact-first: endpoints of top-ranked facts from hybrid search
        if self.graphiti is not None:
            try:
                from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF

                results = await self.graphiti.search_(
                    question,
                    config=EDGE_HYBRID_SEARCH_RRF,
                    group_ids=self.group_ids,
                )
                top_edges = (results.edges or [])[:12]
                endpoint_uuids: list[str] = []
                for edge in top_edges:
                    endpoint_uuids.extend([edge.source_node_uuid, edge.target_node_uuid])
                    for uuid in (edge.source_node_uuid, edge.target_node_uuid):
                        scores[uuid] = scores.get(uuid, 0) + 1.0
                if endpoint_uuids:
                    name_rows = await self._run(
                        """
                        UNWIND $uuids AS uuid
                        MATCH (e:Entity {uuid: uuid})
                        RETURN e.uuid AS uuid, e.name AS name, e.entity_key AS key,
                               [l IN labels(e) WHERE l <> 'Entity'][0] AS type
                        """,
                        uuids=list(dict.fromkeys(endpoint_uuids)),
                    )
                    for row in name_rows:
                        meta[row['uuid']] = row
            except Exception:  # noqa: BLE001 - hybrid search is best-effort
                logger.warning('fact-first entry search failed; falling back to literal', exc_info=True)

        # 2) literal bonus: entity name appears in the question
        name_rows = await self._run(
            """
            MATCH (e:Entity)
            WHERE size(e.name) >= 2 AND $question CONTAINS e.name
            RETURN e.uuid AS uuid, e.name AS name, e.entity_key AS key,
                   [l IN labels(e) WHERE l <> 'Entity'][0] AS type
            """,
            question=question,
        )
        for row in name_rows:
            meta[row['uuid']] = row
            bonus = 10.0
            # Years/document numbers are strong intent signals.
            if any(ch.isdigit() for ch in question) and any(
                y in question for y in _year_fragments(row['name'] or '')
            ):
                bonus = 12.0
            scores[row['uuid']] = scores.get(row['uuid'], 0) + bonus

        scored: list[tuple[float, dict]] = []
        for uuid, score in scores.items():
            row = meta.get(uuid)
            if row is None:
                continue
            name = row['name'] or ''
            if len(name) < 2 or name in _ENTRY_STOPWORDS:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        entries = [row for _score, row in scored[:max_entries]]

        keywords = [e['name'] for e in entries]
        for name in list(keywords):
            if len(name) >= 4 and name[:2] not in keywords:
                keywords.append(name[:2])
        return entries, keywords

    # ---------------- 2. graph expansion ----------------

    async def expand(self, entry_uuids: list[str], max_hops: int = 2, question: str = '') -> list[dict]:
        facts: dict[str, dict] = {}
        seen_pairs: set[tuple[str, str, str]] = set()
        frontier = entry_uuids
        grams = {
            question[i : i + g]
            for g in range(2, 5)
            for i in range(len(question) - g + 1)
            if _is_cjk(question[i])
        }

        def relevance(fact_text: str | None) -> int:
            if not fact_text:
                return 0
            return sum(1 for gram in grams if gram in fact_text)

        for hop in range(1, max_hops + 1):
            rows = await self._run(
                """
                MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity)
                WHERE a.uuid IN $frontier AND a.uuid <> b.uuid
                OPTIONAL MATCH (b)-[deg:RELATES_TO]-()
                WITH a, r, b, count(deg) AS b_degree
                RETURN a.uuid AS src_uuid, a.name AS src, r.uuid AS rel_uuid,
                       coalesce(r.name, type(r)) AS relation, r.fact AS fact,
                       r.episodes AS episodes,
                       b.uuid AS tgt_uuid, b.name AS tgt, b_degree,
                       type(r) AS rel_type
                """,
                frontier=frontier,
            )
            new_frontier: list[str] = []
            for row in rows:
                key = row['rel_uuid'] or f"{row['src_uuid']}->{row['tgt_uuid']}:{row['relation']}"
                if key in facts:
                    continue
                pair_key = (row['src_uuid'], row['relation'], row['tgt_uuid'])
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                row['hop'] = hop
                row['episodes'] = row.get('episodes') or []
                row['relevance'] = relevance(row.get('fact'))
                facts[key] = row
                if row['tgt_uuid'] not in entry_uuids and row['tgt'] not in _ENTRY_STOPWORDS:
                    new_frontier.append(row['tgt_uuid'])
            if not new_frontier:
                break
            frontier = new_frontier
        return sorted(
            facts.values(),
            key=lambda f: (f['hop'], -f.get('relevance', 0), -(f['b_degree'] or 0)),
        )

    # ---------------- 3. read-back + packing ----------------

    async def read_back_contents(
        self,
        entity_uuids: list[str],
        keywords: list[str],
        budget: int,
        episode_uuids: list[str] | None = None,
        priority_episode_uuids: set[str] | None = None,
    ) -> list[dict]:
        """Read back source contents. Priority: episodes of slot/packed facts
        (authoritative, intent-critical) first, then MENTIONS of entries."""
        rows: list[dict] = []
        if episode_uuids:
            rows.extend(await self._run(
                """
                MATCH (c:Content)
                WHERE c.episode_uuid IN $episode_uuids
                WITH c, size([kw IN $keywords WHERE c.text CONTAINS kw]) AS hits,
                     c.episode_uuid AS episode_uuid
                RETURN c.title AS title, c.heading_path AS path, c.text AS text,
                       c.chunk_id AS chunk_id, episode_uuid,
                       hits + 1 AS hits
                LIMIT 40
                """,
                episode_uuids=episode_uuids,
                keywords=keywords,
            ))
        rows.extend(await self._run(
            """
            MATCH (c:Content)-[:MENTIONS]->(e:Entity)
            WHERE e.uuid IN $uuids
            WITH c, size([kw IN $keywords WHERE c.text CONTAINS kw]) AS hits
            WHERE hits > 0
            RETURN c.title AS title, c.heading_path AS path, c.text AS text,
                   c.chunk_id AS chunk_id, NULL AS episode_uuid, hits
            ORDER BY hits DESC
            LIMIT 20
            """,
            uuids=entity_uuids,
            keywords=keywords,
        ))
        # Dedupe by (title, path); slot-priority contents rank first.
        priority = priority_episode_uuids or set()
        best: dict[tuple[str, str], dict] = {}
        for row in rows:
            k = (row['title'], row['path'])
            row['slot_hit'] = 1 if row.get('episode_uuid') in priority else 0
            prev = best.get(k)
            if prev is None or (row['slot_hit'], row['hits']) > (prev.get('slot_hit', 0), prev['hits']):
                best[k] = row
        rows = sorted(best.values(), key=lambda r: (-r.get('slot_hit', 0), -r['hits']))

        packed: list[dict] = []
        used = 0
        max_per_content = 500
        for row in rows:
            text = row.get('text') or ''
            excerpt_len = min(len(text), max_per_content)
            row['text'] = text[:excerpt_len] + ('…' if excerpt_len < len(text) else '')
            cost = min(_tokens(text), max_per_content)
            if used + cost > budget and packed:
                break
            packed.append(row)
            used += cost
        return packed

    def pack_facts(
        self, facts: list[dict], budget: int, slot_relations: list[str] | None = None
    ) -> tuple[list[dict], int]:
        """Slot-driven packing: slot facts force-packed within a 60% quota,
        remaining budget by relevance/degree order."""
        slot_relations = slot_relations or []
        slot_budget = int(budget * 0.6)
        forced = sorted(
            (f for f in facts if f.get('relation') in slot_relations),
            key=lambda f: -f.get('relevance', 0),
        )
        forced_keys: set = set()

        packed: list[dict] = []
        used = 0
        for fact in forced:
            cost = _tokens(fact.get('fact')) + _tokens(fact.get('relation'))
            if used + cost > slot_budget and packed:
                break
            packed.append(fact)
            forced_keys.add(fact.get('rel_uuid') or id(fact))
            used += cost
        rest = [f for f in facts if (f.get('rel_uuid') or id(f)) not in forced_keys]
        for fact in rest:
            cost = _tokens(fact.get('fact')) + _tokens(fact.get('relation'))
            if used + cost > budget and packed:
                break
            packed.append(fact)
            used += cost
        return packed, used

    # ---------------- enumeration mode ----------------

    async def enumerate_evidence(
        self,
        question: str,
        entries: list[dict],
        max_tokens: int,
    ) -> dict:
        """Exhaustive mode: all whitelist relations of each entry document,
        with per-clause Content evidence; deduplicated by fact text/chunk."""
        doc_uuids = [e['uuid'] for e in entries]
        rows = await self._run(
            """
            MATCH (d:Entity)-[r:RELATES_TO]-(clause:Entity)
            WHERE d.uuid IN $doc_uuids
              AND r.name IN ['AMENDS', 'CONTAINS_ARTICLE', 'DEFINES', 'ISSUED_BY',
                             'APPLIES_TO', 'REQUIRES', 'EXCEPTS', 'HAS_PENALTY',
                             'HAS_AMOUNT', 'EFFECTIVE_ON', 'REPEALS', 'CITES']
            RETURN d.name AS doc, d.uuid AS doc_uuid,
                   clause.name AS clause, r.name AS relation,
                   r.fact AS fact, r.episodes AS episodes
            ORDER BY doc, clause
            """,
            doc_uuids=doc_uuids,
        )
        seen_facts: set[str] = set()
        deduped_rows = []
        for row in rows:
            fkey = (row.get('fact') or '').strip()
            if fkey and fkey in seen_facts:
                continue
            if fkey:
                seen_facts.add(fkey)
            deduped_rows.append(row)
        rows = deduped_rows

        groups: dict[str, dict] = {}
        used = 0
        content_budget = max_tokens - int(max_tokens * 0.2)
        seen_chunks: set[str] = set()
        for row in rows:
            doc = row['doc']
            group = groups.setdefault(doc, {'doc': doc, 'clauses': []})
            clause_entry = {
                'clause': row['clause'],
                'relation': row['relation'],
                'fact': row.get('fact'),
                'content': None,
            }
            eps = row.get('episodes') or []
            chunk_key = None
            for ep in eps:
                if ep not in seen_chunks:
                    chunk_key = ep
                    break
            if chunk_key and used < content_budget:
                crows = await self._run(
                    """
                    MATCH (c:Content) WHERE c.episode_uuid IN $eps
                    RETURN c.title AS title, c.heading_path AS path,
                           c.text AS text, c.chunk_id AS chunk_id
                    LIMIT 2
                    """,
                    eps=[chunk_key],
                )
                if crows:
                    text = crows[0].get('text') or ''
                    chunk_id = crows[0].get('chunk_id') or chunk_key
                    if chunk_id in seen_chunks:
                        clause_entry['content'] = {
                            'title': crows[0]['title'], 'path': crows[0]['path'],
                            'duplicate': True, 'chunk_id': chunk_id,
                        }
                    else:
                        excerpt = text[:500]
                        cost = min(_tokens(text), 500)
                        if used + cost <= max_tokens:
                            clause_entry['content'] = {
                                'title': crows[0]['title'],
                                'path': crows[0]['path'],
                                'text': excerpt + ('…' if len(text) > 500 else ''),
                                'chunk_id': chunk_id,
                            }
                            used += cost
                        seen_chunks.add(chunk_id)
            group['clauses'].append(clause_entry)

        return {
            'mode': 'enumeration',
            'entries': entries,
            'groups': list(groups.values()),
            'facts': [
                {
                    'hop': 1,
                    'src': row['doc'],
                    'src_uuid': row['doc_uuid'],
                    'relation': row['relation'],
                    'tgt': row['clause'],
                    'fact': row.get('fact'),
                    'episodes': row.get('episodes') or [],
                }
                for row in rows
            ],
            'contents': [
                c['content']
                for g in groups.values()
                for c in g['clauses']
                if c['content'] and not c['content'].get('duplicate')
            ],
            'tokens_used': used,
            'token_budget': max_tokens,
            'degraded': not rows,
        }

    # ---------------- entry point ----------------

    async def retrieve(
        self,
        question: str,
        max_tokens: int = 4000,
        max_entries: int = 3,
        max_hops: int = 2,
    ) -> dict:
        fact_budget = int(max_tokens * 0.6)
        content_budget = max_tokens - fact_budget

        entries, keywords = await self.find_entries(question, max_entries)
        if not entries:
            return {
                'mode': 'packed',
                'entries': [],
                'keywords': keywords,
                'facts': [],
                'contents': [],
                'tokens_used': 0,
                'token_budget': max_tokens,
                'degraded': True,
                'note': '未找到入口实体，请改用关键词更接近实体名的问法，或使用 /search 混合检索',
            }

        is_enum, signal = detect_enumeration(question)
        if is_enum:
            result = await self.enumerate_evidence(question, entries, max_tokens)
            result['keywords'] = keywords
            result['enum_signal'] = signal
            return result

        facts = await self.expand([e['uuid'] for e in entries], max_hops, question)
        slot_relations = extract_slots(question)
        packed_facts, fact_tokens = self.pack_facts(facts, fact_budget, slot_relations)
        slot_eps: list[str] = []
        all_eps: list[str] = []
        for f in packed_facts:
            eps = f.get('episodes') or []
            if f.get('relation') in slot_relations:
                slot_eps.extend(eps)
            all_eps.extend(eps)
        contents = await self.read_back_contents(
            [e['uuid'] for e in entries], keywords, content_budget,
            episode_uuids=list(dict.fromkeys(all_eps))[:40],
            priority_episode_uuids=set(slot_eps),
        )

        return {
            'mode': 'packed',
            'entries': entries,
            'keywords': keywords,
            'facts': packed_facts,
            'contents': contents,
            'tokens_used': fact_tokens + sum(_tokens(c.get('text') or '') for c in contents),
            'token_budget': max_tokens,
            'degraded': len(packed_facts) == 0,
        }
