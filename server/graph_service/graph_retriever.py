"""Graph-first retriever: keyword entry -> relation graph search -> token budget.

Combination of Graphiti primitives and LightRAG-style packing (zero LLM):

  1. Entry localization: exact/alias/containment matching of question keywords
     against entity names (normalize_name + alias dictionary), ranked.
  2. Graph search: expand 1..max_hops along relations from the entries,
     collecting (source, relation, fact, target) with neighbor degrees.
  3. max_token packing (LightRAG): fact budget first (1-hop before 2-hop,
     degree-descending within a hop), then Content full-text budget via
     MENTIONS read-back. Packing stops when the budget is exhausted.
  4. Fallback: when no entries are found, degrade to Graphiti's native hybrid
     fact search so the caller never gets an empty answer without a reason.

All queries are static parameterized templates — nothing here is handwritten
per round.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Rough token estimate: CJK ~= 1 token per char (conservative). Good enough
# for budget packing without shipping a tokenizer.
def _tokens(text: str | None) -> int:
    if not text:
        return 0
    return len(text)


_STOPWORDS = {
    '的', '了', '是', '在', '和', '与', '及', '对', '把', '被', '由', '从', '向',
    '什么', '哪些', '哪个', '怎么', '怎样', '如何', '为什么', '请问', '哪些个',
    '规定', '进行', '可以', '应当', '必须', '以及', '或者', '还有', '关于',
}
_WORD_RE = re.compile(r'[\u4e00-\u9fffA-Za-z0-9]+')


def extract_keywords(question: str, max_keywords: int = 6) -> list[str]:
    """Deterministic keyword extraction: split and drop stopwords/short noise."""
    keywords: list[str] = []
    for chunk in _WORD_RE.findall(question):
        if len(chunk) < 2 or chunk.lower() in _STOPWORDS:
            continue
        if chunk not in keywords:
            keywords.append(chunk)
    # Also keep 2-char heads of long compounds (e.g. 私营经济 -> 私营) so
    # containment matching can still hit short entity names.
    extra: list[str] = []
    for chunk in list(keywords):
        if len(chunk) >= 4 and len(keywords) + len(extra) < max_keywords * 2:
            if chunk[:2] not in keywords and chunk[:2] not in extra:
                extra.append(chunk[:2])
    return (keywords + extra)[:max_keywords * 2]


def _year_fragments(name: str) -> list[str]:
    """Extract 4-digit years (and parenthesized year spans) from a name."""
    import re as _re

    return _re.findall(r'\d{4}', name)


def _is_cjk(ch: str) -> bool:
    return '一' <= ch <= '鿿'


# Enumeration intent signals (spec: aggregation questions need exhaustive
# queries, not budget-packed retrieval).
_ENUM_SIGNALS = [
    '分别', '所有', '全部', '每一', '一一', '列举', '历届', '历次', '各自',
    '哪些条款', '哪些修改', '哪些变化', '有哪些', '分别修改', '都修改',
]


# Generic high-frequency names that match almost any question fragment —
# they must never become entry points or expansion seeds (e.g. 全国 matching
# "全国人大" pulls in unrelated corpora).
_ENTRY_STOPWORDS = {
    '全国', '国家', '我国', '地方', '社会', '人民', '政府', '中央', '地区',
    '单位', '组织', '机构', '部门', '个人', '有关', '其他', '相关',
}


def detect_enumeration(question: str) -> tuple[bool, str]:
    for signal in _ENUM_SIGNALS:
        if signal in question:
            return True, signal
    return False, ''


# Intent slots: question signals -> relation names whose facts MUST be packed.
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

    # ---------------- 1. entry localization ----------------

    async def find_entries(self, question: str, max_entries: int = 3) -> tuple[list[dict], list[str]]:
        """Fact-first entry localization (upstream Graphiti pattern).

        1. Hybrid fact search (BM25 + vector + RRF, zero LLM) ranks facts.
        2. The endpoint entities of the top facts become entry candidates —
           entry count is naturally bound to relevance.
        3. Literal name matches (question contains entity name / alias) add a
           bonus so exact mentions win over merely co-occurring endpoints.
        """
        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}
        keywords: list[str] = []

        # Exact article mentions take precedence and prevent high-degree
        # constitution nodes from becoming broad expansion seeds.
        exact_articles = await self._run(
            """
            MATCH (e:Entity)
            WHERE e.name CONTAINS '第' AND e.name CONTAINS '条'
              AND $question CONTAINS e.name
            RETURN e.uuid AS uuid, e.name AS name, e.entity_key AS key,
                   [l IN labels(e) WHERE l <> 'Entity'][0] AS type
            ORDER BY size(e.name) DESC
            LIMIT $limit
            """,
            question=question, limit=max_entries,
        )
        if exact_articles:
            entries = [dict(row) for row in exact_articles]
            return entries, [e['name'] for e in entries]

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
            scores[row['uuid']] = scores.get(row['uuid'], 0) + 10.0

        # stopword filter + ranking
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

        # keywords for content ranking: entry names + short heads
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
        # Question n-grams for relevance scoring of fact text.
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
                # Raw RELATES_TO and its materialized typed edge describe the
                # same fact — keep only one per (src, relation, tgt).
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
        # Rank: closer hops first, then question relevance, then hub degree.
        return sorted(
            facts.values(),
            key=lambda f: (f['hop'], -f.get('relevance', 0), -(f['b_degree'] or 0)),
        )

    # ---------------- 3. packing + read-back ----------------

    async def read_back_contents(
        self,
        entity_uuids: list[str],
        keywords: list[str],
        budget: int,
        episode_uuids: list[str] | None = None,
        priority_episode_uuids: set[str] | None = None,
    ) -> list[dict]:
        """Read back source contents. Priority: episodes of packed facts
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
                   c.chunk_id AS chunk_id, hits
            ORDER BY hits DESC
            LIMIT 20
            """,
            uuids=entity_uuids,
            keywords=keywords,
        ))
        # Dedupe by (title, path), keeping the higher hits.
        best: dict[tuple[str, str], dict] = {}
        for row in rows:
            k = (row['title'], row['path'])
            if k not in best or row['hits'] > best[k]['hits']:
                best[k] = row
        priority = priority_episode_uuids or set()
        for row in rows:
            row['slot_hit'] = 1 if row.get('episode_uuid') in priority else 0
        rows = sorted(best.values(), key=lambda r: (-r.get('slot_hit', 0), -r['hits']))
        packed: list[dict] = []
        used = 0
        max_per_content = 500
        for row in rows:
            text = row.get('text') or ''
            # Excerpt per content so multiple documents fit the budget —
            # multi-evidence questions need breadth over full texts.
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
        """Slot-driven packing: facts matching intent slots are force-packed
        first (they answer the question even when ranked low by relevance);
        the remaining budget is filled by relevance/degree order."""
        slot_relations = slot_relations or []
        # Slot facts are force-packed, but within a quota (60% of budget) and
        # by relevance — a broad slot (e.g. DEFINES) must not eat the budget.
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
        target_clause_names: list[str] | None = None,
    ) -> dict:
        """Exhaustive mode: return all clause relations and source evidence.

        Unlike packed retrieval, exhaustive retrieval never truncates a source
        chunk.  The token budget only controls which full chunks can be
        returned; completeness is reported explicitly when the budget is too
        small.
        """
        doc_uuids = [e['uuid'] for e in entries]
        rows = await self._run(
            """
            MATCH (d:Entity)-[r:RELATES_TO]-(clause:Entity)
            WHERE d.uuid IN $doc_uuids
              AND ($target_clause_names IS NULL OR clause.name IN $target_clause_names)
              AND r.name IN ['AMENDS', 'CONTAINS_ARTICLE', 'DEFINES', 'ISSUED_BY',
                             'APPLIES_TO', 'REQUIRES', 'EXCEPTS', 'HAS_PENALTY',
                             'HAS_AMOUNT', 'EFFECTIVE_ON', 'REPEALS', 'CITES']
            OPTIONAL MATCH (d)-[t:AMENDS|CONTAINS_ARTICLE|DEFINES|ISSUED_BY]->(clause)
            WITH d, clause, collect(DISTINCT r)[0] AS r0,
                 collect(DISTINCT r.fact)[0] AS fact,
                 collect(DISTINCT r.episodes)[0] AS episodes
            RETURN d.name AS doc, d.uuid AS doc_uuid,
                   clause.name AS clause, r0.name AS relation,
                   fact, episodes
            ORDER BY doc, clause
            """,
            doc_uuids=doc_uuids,
            target_clause_names=target_clause_names,
        )
        # Content evidence per clause via its episode uuids.
        # Same fact text can appear under several entity granularities
        # (document vs clause entities) — it is one piece of evidence.
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

        clause_episodes: dict[int, list[str]] = {}
        for i, row in enumerate(rows):
            eps = row.get('episodes') or []
            if eps:
                clause_episodes[i] = eps

        # One source chunk is evidence once, not once per relation.
        seen_chunks: set[str] = set()
        omitted_chunks: set[str] = set()

        groups: dict[str, dict] = {}
        used = 0
        content_budget = max_tokens - int(max_tokens * 0.2)
        for i, row in enumerate(rows):
            doc = row['doc']
            group = groups.setdefault(doc, {'doc': doc, 'clauses': []})
            clause_entry = {
                'clause': row['clause'],
                'relation': row['relation'],
                'fact': row.get('fact'),
                'content': None,
            }
            # Read back the complete source content for this clause.
            eps = clause_episodes.get(i) or []
            chunk_key = None
            if eps:
                for ep in eps:
                    if ep not in seen_chunks:
                        chunk_key = ep
                        break
            if chunk_key:
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
                        cost = _tokens(text)
                        if used + cost <= max_tokens:
                            clause_entry['content'] = {
                                'title': crows[0]['title'],
                                'path': crows[0]['path'],
                                'text': text,
                                'chunk_id': chunk_id,
                            }
                            used += cost
                        else:
                            omitted_chunks.add(chunk_id)
                        seen_chunks.add(chunk_id)
            group['clauses'].append(clause_entry)

        return {
            'mode': 'enumeration',
            'entries': entries,
            'groups': list(groups.values()),
            # Flat view kept for the path graph.
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
            # Only independently packed contents go into the flat list;
            # duplicates stay as chunk_id markers on their clause entries.
            'contents': [
                c['content']
                for g in groups.values()
                for c in g['clauses']
                if c['content'] and not c['content'].get('duplicate')
            ],
            'tokens_used': used,
            'token_budget': max_tokens,
            'completeness': {
                'is_complete': not omitted_chunks,
                'omitted_chunks': sorted(omitted_chunks),
                'returned_chunk_count': len(seen_chunks) - len(omitted_chunks),
                'omitted_chunk_count': len(omitted_chunks),
            },
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
                'entries': [],
                'keywords': keywords,
                'facts': [],
                'contents': [],
                'tokens_used': 0,
                'degraded': True,
                'note': '未找到入口实体，请改用关键词更接近实体名的问法，或使用 /search 混合检索',
            }

        is_enum, signal = detect_enumeration(question)
        article_names = [e['name'] for e in entries if e.get('type') == 'Article']
        if is_enum and entries:
            result = await self.enumerate_evidence(
                question, entries, max_tokens,
                target_clause_names=article_names or None,
            )
            result['keywords'] = keywords
            result['enum_signal'] = signal
            return result
        facts = await self.expand([e['uuid'] for e in entries], max_hops, question)
        slot_relations = extract_slots(question)
        packed_facts, fact_tokens = self.pack_facts(facts, fact_budget, slot_relations)
        # Slot facts first: their episodes are the authoritative sources.
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
        content_tokens = sum(_tokens(c['text']) for c in contents)

        return {
            'mode': 'packed',
            'entries': entries,
            'keywords': keywords,
            'facts': packed_facts,
            'contents': contents,
            'tokens_used': fact_tokens + content_tokens,
            'token_budget': max_tokens,
            'degraded': len(packed_facts) == 0,
        }
