"""Build the OHN hierarchy and conservative ontology graph for the first chunks."""
import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from .legal_ontology import ONTOLOGY_ID, ONTOLOGY_VERSION


def read_chunks(path: Path, limit: int):
    with path.open(encoding='utf-8') as fh:
        return [json.loads(line) for _, line in zip(range(limit), fh, strict=False)]


def entities_for(chunk):
    meta, text = chunk['metadata'], chunk['page_content']
    doc_id, title = meta['document_id'], meta['document_title']
    result = []
    if title in text:
        result.append({'id': doc_id, 'name': title, 'canonical_name': title, 'type': 'LegalDocument', 'source_text': title})
    article = meta.get('title', '')
    if re.fullmatch(r'(第[^\s]{1,8}条)', article):
        result.append({'id': meta['chunk_id'] + ':article', 'name': article, 'canonical_name': article,
                       'type': 'Article', 'source_text': article})
    for name, typ in [('国家', 'Organization'), ('中国共产党', 'Organization'), ('私营经济', 'LegalConcept'),
                      ('土地的使用权', 'Right'), ('社会主义民主', 'LegalConcept')]:
        if name in text:
            result.append({'id': f"{meta['chunk_id']}:{typ}:{name}", 'name': name, 'canonical_name': name,
                           'type': typ, 'source_text': name})
    return result


async def build(path: Path, limit: int, dry_run: bool):
    chunks = read_chunks(path, limit)
    driver = None
    if not dry_run:
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        driver = Neo4jDriver(
            os.environ['NEO4J_URI'],
            os.environ.get('NEO4J_USER'),
            os.environ.get('NEO4J_PASSWORD'),
        )
    queries = []
    extractor = None
    if not dry_run:
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from .extractor import OntologyExtractor

        extractor = OntologyExtractor(OpenAIClient(config=LLMConfig(
            api_key=os.environ['OPENAI_API_KEY'],
            base_url=os.environ['OPENAI_BASE_URL'],
            model=os.environ.get('MODEL_NAME', 'qwen-turbo'),
        )))
    for chunk in chunks:
        m, text = chunk['metadata'], chunk['page_content']
        doc_props = {k: v for k, v in m.items() if k in {
            'document_id', 'document_title', 'source_file', 'source_path', 'file_type',
            'file_version', 'document_number', 'publication_date', 'content_hash',
            'file_role', 'parent_document_id', 'attachment_chain_document_ids',
            'attachment_document_ids', 'attachment_bindings', 'attachment_number',
            'attachment_label', 'attachment_content_type', 'attachment_presentation',
            'is_ocr', 'ocr_engine', 'visual_type',
        }}
        doc_props.update({'ontology_id': ONTOLOGY_ID, 'ontology_version': ONTOLOGY_VERSION})
        queries.append(('MERGE (d:Document {document_id:$document_id}) SET d += $props', {'document_id': m['document_id'], 'props': doc_props}))
        if m.get('parent_document_id'):
            queries.append(('MATCH (a:Document {document_id:$child}),(p:Document {document_id:$parent}) MERGE (a)-[r:ATTACHMENT_OF]->(p) SET r.source_chunk_id=$chunk_id, r.attachment_number=$number', {'child': m['document_id'], 'parent': m['parent_document_id'], 'chunk_id': m['chunk_id'], 'number': m.get('attachment_number', 0)}))
        parent = m['parent_node_id'] or m['document_id']
        section_props = {
            'title': m['heading_path_parts'][-1],
            'heading_path': m['heading_path'],
            'heading_path_parts': m['heading_path_parts'],
            'parent_path': m['parent_path'],
            'parent_node_id': m.get('parent_node_id'),
            'document_id': m['document_id'],
            'chunk_id': m['chunk_id'],
            'level': len(m['heading_path_parts']),
        }
        queries.append(('MERGE (s:Section {node_id:$sid}) SET s += $props MERGE (d:Document {document_id:$did}) MERGE (d)-[:CONTAINS]->(s)', {'sid': parent, 'props': section_props, 'did': m['document_id']}))
        # Keep the complete upstream metadata on Content as the evidence/navigation record.
        content_props = dict(m)
        content_props.update({'text': text, 'document_id': m['document_id']})
        queries.append(('MERGE (c:Content {chunk_id:$chunk_id}) SET c += $props MERGE (s:Section {node_id:$sid}) MERGE (s)-[:CONTAINS]->(c)', {'chunk_id': m['chunk_id'], 'props': content_props, 'sid': parent}))
        if m.get('previous_leaf_id'):
            nav = {'source_chunk_id': m['chunk_id'], 'source_document_id': m['document_id'], 'chunk_index': m['chunk_index']}
            queries.append(('MATCH (a:Content {chunk_id:$a}),(b:Content {chunk_id:$b}) MERGE (a)-[r:NEXT]->(b) SET r += $props MERGE (b)-[p:PREVIOUS]->(a) SET p += $props', {'a': m['previous_leaf_id'], 'b': m['chunk_id'], 'props': nav}))
        for e in entities_for(chunk):
            queries.append(('MERGE (e:Entity {entity_id:$eid}) SET e.name=$name, e.canonical_name=$canonical_name, e.entity_type=$type, e.source_text=$source_text, e.source_chunk_id=$source_chunk_id, e.source_document_id=$source_document_id, e.ontology_id=$ontology_id, e.ontology_version=$ontology_version MERGE (c:Content {chunk_id:$cid}) MERGE (c)-[:MENTIONS]->(e)', {**e, 'eid': e['id'], 'cid': m['chunk_id'], 'source_chunk_id': m['chunk_id'], 'source_document_id': m['document_id'], 'ontology_id': ONTOLOGY_ID, 'ontology_version': ONTOLOGY_VERSION}))
        if extractor:
            extracted, errors = await extractor.extract(text, m['chunk_id'])
            print(f"{m['chunk_id']}: LLM entities={len(extracted.entities)} relations={len(extracted.relations)} rejected={len(errors)}")
            ids = {e.id: e for e in extracted.entities}
            for e in extracted.entities:
                params = {'eid': f"llm:{m['chunk_id']}:{e.id}", 'name': e.name, 'canonical': e.canonical_name, 'type': e.type, 'source_text': e.source_text, 'cid': m['chunk_id'], 'did': m['document_id'], 'oid': ONTOLOGY_ID, 'version': ONTOLOGY_VERSION}
                queries.append(('MERGE (e:Entity {entity_id:$eid}) SET e.name=$name, e.canonical_name=$canonical, e.entity_type=$type, e.source_text=$source_text, e.source_chunk_id=$cid, e.source_document_id=$did, e.ontology_id=$oid, e.ontology_version=$version MERGE (c:Content {chunk_id:$cid}) MERGE (c)-[:MENTIONS]->(e)', params))
            for r in extracted.relations:
                if r.source in ids and r.target in ids:
                    queries.append(('MATCH (a:Entity {entity_id:$a}),(b:Entity {entity_id:$b}) MERGE (a)-[r:`'+r.type+'`]->(b) SET r.source_chunk_id=$cid, r.source_document_id=$did, r.evidence_text=$evidence, r.confidence=$confidence, r.ontology_id=$oid, r.ontology_version=$version', {'a': f"llm:{m['chunk_id']}:{r.source}", 'b': f"llm:{m['chunk_id']}:{r.target}", 'cid': m['chunk_id'], 'did': m['document_id'], 'evidence': r.evidence_text, 'confidence': r.confidence, 'oid': ONTOLOGY_ID, 'version': ONTOLOGY_VERSION}))
    if dry_run:
        print(f'prepared {len(queries)} cypher statements for {len(chunks)} chunks')
    else:
        for query, params in queries:
            await driver.execute_query(query, **params)
        nodes, _, _ = await driver.execute_query(
            'MATCH (n) WHERE n:Document OR n:Section OR n:Content OR n:Entity '
            'RETURN labels(n) AS labels, count(*) AS count ORDER BY labels'
        )
        relationships, _, _ = await driver.execute_query(
            "MATCH ()-[r]->() WHERE type(r) IN ['CONTAINS','NEXT','PREVIOUS','MENTIONS'] "
            'RETURN type(r) AS relation, count(*) AS count ORDER BY relation'
        )
        await driver.close()
        print(f'loaded first {len(chunks)} chunks into Neo4j')
        print('nodes:', [dict(row) for row in nodes])
        print('relationships:', [dict(row) for row in relationships])


if __name__ == '__main__':
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='../OHN-GraphRAG_chunks_test/chunks.jsonl')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    asyncio.run(build(Path(args.input), args.limit, args.dry_run))
