from datetime import datetime, timezone

from fastapi import APIRouter, status

from graph_service.dto import (
    GetMemoryRequest,
    GetMemoryResponse,
    GraphSearchQuery,
    Message,
    SearchQuery,
    SearchResults,
)
from graph_service.zep_graphiti import ZepGraphitiDep, get_fact_result_from_edge

router = APIRouter()


@router.post('/search', status_code=status.HTTP_200_OK)
async def search(query: SearchQuery, graphiti: ZepGraphitiDep):
    relevant_edges = await graphiti.search(
        group_ids=query.group_ids,
        query=query.query,
        num_results=query.max_facts,
    )
    facts = [get_fact_result_from_edge(edge) for edge in relevant_edges]
    return SearchResults(
        facts=facts,
    )


@router.get('/entity-edge/{uuid}', status_code=status.HTTP_200_OK)
async def get_entity_edge(uuid: str, graphiti: ZepGraphitiDep):
    entity_edge = await graphiti.get_entity_edge(uuid)
    return get_fact_result_from_edge(entity_edge)


@router.get('/episodes/{group_id}', status_code=status.HTTP_200_OK)
async def get_episodes(group_id: str, last_n: int, graphiti: ZepGraphitiDep):
    episodes = await graphiti.retrieve_episodes(
        group_ids=[group_id], last_n=last_n, reference_time=datetime.now(timezone.utc)
    )
    return episodes


@router.post('/get-memory', status_code=status.HTTP_200_OK)
async def get_memory(
    request: GetMemoryRequest,
    graphiti: ZepGraphitiDep,
):
    combined_query = compose_query_from_messages(request.messages)
    result = await graphiti.search(
        group_ids=[request.group_id],
        query=combined_query,
        num_results=request.max_facts,
    )
    facts = [get_fact_result_from_edge(edge) for edge in result]
    return GetMemoryResponse(facts=facts)


def compose_query_from_messages(messages: list[Message]):
    combined_query = ''
    for message in messages:
        combined_query += f'{message.role_type or ""}({message.role or ""}): {message.content}\n'
    return combined_query


@router.post('/search/graph', status_code=status.HTTP_200_OK)
async def graph_search(query: GraphSearchQuery, graphiti: ZepGraphitiDep):
    """Keyword entry -> relation graph search -> max_token packing (zero LLM)."""
    from graph_service.graph_retriever import GraphRetriever

    retriever = GraphRetriever(graphiti.driver, group_ids=query.group_ids, graphiti=graphiti)
    return await retriever.retrieve(
        query.query,
        max_tokens=query.max_tokens,
        max_entries=query.max_entries,
        max_hops=query.max_hops,
    )


@router.get('/content/{chunk_id}', status_code=status.HTTP_200_OK)
async def get_content(chunk_id: str, graphiti: ZepGraphitiDep):
    "Full text of a Content node (/search/graph excerpts are truncated)."
    records = await graphiti.driver.execute_query(
        'MATCH (c:Content {chunk_id: $chunk_id}) RETURN c.text AS text, c.title AS title, c.heading_path AS path',
        params={'chunk_id': chunk_id},
    )
    if not records.records:
        return {'success': False, 'text': None}
    row = records.records[0]
    return {'success': True, 'title': row['title'], 'path': row['path'], 'text': row['text']}
