"""Hierarchy builder: Document / Section / Content + navigation relations.

Implements the deterministic rule layer of the development spec (sections
5, 9, 16, 17): every field comes from the JSONL metadata verbatim, written by
rules only — the LLM never sees or rewrites structure. Construction order:

    Document (by document_id)
    -> Section chain (parent_node_id > parent_path > heading_path_parts)
    -> Content (by chunk_id)
    -> CONTAINS / NEXT / PREVIOUS / ATTACHMENT_OF

Also links each Content to the Graphiti episode that extracts from it so the
Content-[:MENTIONS]->Entity bridge can be created after extraction.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fields owned by the Document node (spec section 17 field table).
DOCUMENT_METADATA_FIELDS = [
    'document_id', 'document_title', 'source_file', 'source_path', 'file_type',
    'file_version', 'document_number', 'publication_date', 'content_hash',
    'file_role', 'parent_document_id', 'attachment_chain_document_ids',
    'attachment_document_ids', 'attachment_bindings', 'attachment_number',
    'attachment_label', 'attachment_content_type', 'attachment_presentation',
    'is_ocr', 'ocr_engine', 'visual_type',
]

SECTION_FIELDS = [
    'node_id', 'title', 'heading_path', 'heading_path_parts', 'parent_path',
    'parent_node_id', 'level', 'document_id', 'chunk_summary',
]


def _section_id(document_id: str, heading_path: str) -> str:
    return f'{document_id}#section#{heading_path}'


class HierarchyBuilder:
    """Writes the Document/Section/Content skeleton into Neo4j."""

    def __init__(self, driver, ontology_id: str = 'legal_ontology', ontology_version: str = '1.0.0'):
        self.driver = driver
        self.ontology_id = ontology_id
        self.ontology_version = ontology_version

    async def _run(self, query: str, **params) -> None:
        """execute_query with keyword params for the raw neo4j async driver."""
        await self.driver.execute_query(query, parameters_=params)

    async def ensure_indexes(self) -> None:
        queries = [
            'CREATE INDEX document_id IF NOT EXISTS FOR (n:Document) ON (n.document_id)',
            'CREATE INDEX section_node_id IF NOT EXISTS FOR (n:Section) ON (n.node_id)',
            'CREATE INDEX content_chunk_id IF NOT EXISTS FOR (n:Content) ON (n.chunk_id)',
            'CREATE INDEX content_doc_id IF NOT EXISTS FOR (n:Content) ON (n.document_id)',
        ]
        for query in queries:
            try:
                await self._run(query)
            except Exception:
                logger.debug('Index creation skipped (may already exist): %s', query[:60])

    async def build_from_records(self, records: list[dict[str, Any]]) -> dict[str, int]:
        """Build the full hierarchy for a batch of JSONL records."""
        stats = {'documents': 0, 'sections': 0, 'contents': 0, 'next_prev': 0}
        for record in records:
            meta = record.get('metadata', {})
            stats['documents'] += await self._merge_document(meta)
            stats['sections'] += await self._merge_section_chain(meta)
            stats['contents'] += await self._merge_content(record, meta)
        # Navigation edges need all contents present first.
        for record in records:
            stats['next_prev'] += await self._link_navigation(record.get('metadata', {}))
        await self._link_attachments()
        return stats

    async def _merge_document(self, meta: dict) -> int:
        if not meta.get('document_id'):
            return 0
        props = {
            field: meta.get(field)
            for field in DOCUMENT_METADATA_FIELDS
            if meta.get(field) is not None
        }
        # Nested maps (attachment_bindings entries) are not valid Neo4j
        # property values — serialize them, metadata stays lossless.
        for key, value in props.items():
            if isinstance(value, dict) or (
                isinstance(value, list) and any(isinstance(item, dict) for item in value)
            ):
                props[key] = json.dumps(value, ensure_ascii=False)
        props['ontology_id'] = self.ontology_id
        props['ontology_version'] = self.ontology_version
        await self._run(
            """
            MERGE (d:Document {document_id: $document_id})
            SET d += $props
            """,
            document_id=meta['document_id'],
            props=props,
        )
        return 1

    async def _merge_section_chain(self, meta: dict) -> int:
        parts = meta.get('heading_path_parts') or []
        if not parts:
            return 0
        document_id = meta['document_id']
        created = 0
        previous_section_id: str | None = None
        # Path prefixes: 中华人民共和国宪法修正案（1988年） > 第一条
        for level in range(1, len(parts) + 1):
            path_parts = parts[:level]
            heading_path = ' > '.join(path_parts)
            section_id = _section_id(document_id, heading_path)
            section_props = {
                'node_id': meta.get('parent_node_id') if level == len(parts) else section_id,
                'title': path_parts[-1],
                'heading_path': heading_path,
                'heading_path_parts': path_parts,
                'parent_path': parts[: level - 1] if level > 1 else [],
                'parent_node_id': previous_section_id,
                'level': level,
                'document_id': document_id,
                'ontology_id': self.ontology_id,
                'ontology_version': self.ontology_version,
            }
            await self._run(
                """
                MERGE (s:Section {node_id: $node_id})
                SET s += $props
                WITH s
                MATCH (d:Document {document_id: $document_id})
                MERGE (d)-[:CONTAINS]->(s)
                """,
                node_id=section_props['node_id'] or section_id,
                props=section_props,
                document_id=document_id,
            )
            if previous_section_id is not None:
                await self._run(
                    """
                    MATCH (parent:Section {node_id: $parent_id}), (child:Section {node_id: $child_id})
                    MERGE (parent)-[:CONTAINS]->(child)
                    """,
                    parent_id=previous_section_id,
                    child_id=section_props['node_id'] or section_id,
                )
            previous_section_id = section_props['node_id'] or section_id
            created += 1
        return created

    async def _merge_content(self, record: dict, meta: dict) -> int:
        chunk_id = meta.get('chunk_id')
        if not chunk_id:
            return 0
        # All 35 metadata fields preserved verbatim (spec 17): empty values kept.
        # Nested maps (e.g. attachment_bindings entries) are not valid Neo4j
        # properties — serialize them so no metadata is lost.
        def _flat(value: Any) -> Any:
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                return json.dumps(value, ensure_ascii=False)
            return value

        props: dict[str, Any] = {key: _flat(val) for key, val in meta.items()}
        props['text'] = record.get('page_content', '')
        props['chunk_id'] = chunk_id
        props['ontology_id'] = self.ontology_id
        props['ontology_version'] = self.ontology_version
        await self._run(
            """
            MERGE (c:Content {chunk_id: $chunk_id})
            SET c += $props
            """,
            chunk_id=chunk_id,
            props=props,
        )
        # Attach to its Section (by heading_path — stable even when the
        # deepest Section reuses parent_node_id as its id), plus a Document
        # safety edge so every Content is always traceable to its file.
        parts = meta.get('heading_path_parts') or []
        heading_path = ' > '.join(parts) if parts else None
        await self._run(
            """
            MATCH (c:Content {chunk_id: $chunk_id})
            MATCH (d:Document {document_id: $document_id})
            MERGE (d)-[:CONTAINS]->(c)
            WITH c, d
            OPTIONAL MATCH (s:Section {
                document_id: d.document_id,
                heading_path: $heading_path
            })
            WITH c, s WHERE s IS NOT NULL
            MERGE (s)-[:CONTAINS]->(c)
            """,
            chunk_id=chunk_id,
            document_id=meta.get('document_id'),
            heading_path=heading_path,
        )
        return 1

    async def _link_navigation(self, meta: dict) -> int:
        """NEXT/PREVIOUS from explicit pointers; chunk_index is only a fallback."""
        chunk_id = meta.get('chunk_id')
        if not chunk_id:
            return 0
        linked = 0
        if meta.get('next_leaf_id'):
            await self._create_nav(chunk_id, meta['next_leaf_id'], 'NEXT')
            linked += 1
        if meta.get('previous_leaf_id'):
            await self._create_nav(chunk_id, meta['previous_leaf_id'], 'PREVIOUS')
            linked += 1
        if linked == 0:
            # Fallback: consecutive chunk_index within the same document.
            await self._run(
                """
                MATCH (c:Content {chunk_id: $chunk_id})
                MATCH (n:Content {
                    document_id: c.document_id,
                    chunk_index: c.chunk_index + 1
                })
                MERGE (c)-[:NEXT]->(n)
                """,
                chunk_id=chunk_id,
            )
            linked += 1
        return linked

    async def _create_nav(self, source_id: str, target_id: str, rel: str) -> None:
        await self._run(
            f"""
            MATCH (a:Content {{chunk_id: $source_id}})
            MATCH (b:Content {{chunk_id: $target_id}})
            MERGE (a)-[:{rel}]->(b)
            """,
            source_id=source_id,
            target_id=target_id,
        )

    async def _link_attachments(self) -> None:
        """ATTACHMENT_OF from parent_document_id (rule-built, not LLM)."""
        await self._run(
            """
            MATCH (child:Document), (parent:Document)
            WHERE child.parent_document_id IS NOT NULL
              AND child.parent_document_id <> ''
              AND child.parent_document_id = parent.document_id
            MERGE (child)-[r:ATTACHMENT_OF]->(parent)
            SET r.attachment_number = child.attachment_number
            """
        )

    async def link_episode_to_content(self, chunk_id: str, episode_uuid: str) -> None:
        """Record the Content <-> episode mapping used for the MENTIONS bridge."""
        await self._run(
            """
            MATCH (c:Content {chunk_id: $chunk_id})
            SET c.episode_uuid = $episode_uuid
            """,
            chunk_id=chunk_id,
            episode_uuid=episode_uuid,
        )

    async def bridge_mentions_from_episodes(self) -> int:
        """Copy MENTIONS from episodes to Content nodes.

        Graphiti extracts with Content-[:MENTIONS]->Entity semantics (spec
        4.1); the Episodic MENTIONS stay for Graphiti internals, this bridge
        puts the same evidence on the Content level with chunk provenance.
        """
        result = await self.driver.execute_query(
            """
            MATCH (c:Content)
            WHERE c.episode_uuid IS NOT NULL
            MATCH (e:Episodic {uuid: c.episode_uuid})-[m:MENTIONS]->(ent:Entity)
            MERGE (c)-[cm:MENTIONS {uuid: 'content-mention-' + m.uuid}]->(ent)
            SET cm.mention_text = m.mention_text,
                cm.source_chunk_id = c.chunk_id,
                cm.resolution = m.resolution,
                cm.resolution_confidence = m.resolution_confidence
            RETURN count(cm) AS bridged
            """
        )
        try:
            return int(result.records[0]['bridged'])
        except (IndexError, KeyError, TypeError):
            return 0


    async def cleanup_episodic(self) -> int:
        """Remove Graphiti's internal Episodic nodes after extraction.

        The user-facing graph keeps only Document/Section/Content plus
        LLM-extracted Entity nodes and their relations (spec section 5.1).
        MENTIONS evidence already lives on Content-[:MENTIONS]->Entity after
        the bridge; Episodic and its edges are pipeline scaffolding only.
        """
        result = await self.driver.execute_query(
            'MATCH (e:Episodic) DETACH DELETE e RETURN count(e) AS deleted'
        )
        try:
            return int(result.records[0]['deleted'])
        except (IndexError, KeyError, TypeError):
            return 0


def read_jsonl(path: str, limit: int | None = None) -> list[dict]:
    records = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
            if limit is not None and len(records) >= limit:
                break
    return records


__all__ = ['DOCUMENT_METADATA_FIELDS', 'HierarchyBuilder', 'read_jsonl']
