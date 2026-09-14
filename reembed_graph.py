"""One-shot re-embedding of Entity.name_embedding and RELATES_TO.fact_embedding."""
import asyncio, sys
sys.path.insert(0, '.')
from openai import AsyncOpenAI
from neo4j import AsyncGraphDatabase

client = AsyncOpenAI(api_key='sk-pdmkuamvdvsujwheezioxacslbqhfaromtoydhvfztgkokkm', base_url='https://api.siliconflow.cn/v1')
MODEL = 'BAAI/bge-m3'

async def embed(texts):
    resp = await client.embeddings.create(model=MODEL, input=texts)
    return [d.embedding for d in resp.data]

async def reembed_nodes(driver):
    result = await driver.execute_query('MATCH (e:Entity) RETURN e.uuid AS uuid, e.name AS name')
    items = [(r['uuid'], r['name']) for r in result.records]
    for i in range(0, len(items), 32):
        batch = items[i:i+32]
        vectors = await embed([t for _, t in batch])
        await driver.execute_query(
            'UNWIND $items AS item MATCH (e:Entity {uuid: item.uuid}) SET e.name_embedding = item.vec',
            parameters_={'items': [{'uuid': u, 'vec': v} for (u, _), v in zip(batch, vectors)]},
        )
    return len(items)

async def reembed_edges(driver):
    result = await driver.execute_query('MATCH ()-[r:RELATES_TO]->() RETURN r.uuid AS uuid, r.fact AS fact')
    items = [(r['uuid'], r['fact'] or '') for r in result.records]
    for i in range(0, len(items), 32):
        batch = items[i:i+32]
        vectors = await embed([t if t else ' ' for _, t in batch])
        await driver.execute_query(
            'UNWIND $items AS item MATCH ()-[r:RELATES_TO {uuid: item.uuid}]->() SET r.fact_embedding = item.vec',
            parameters_={'items': [{'uuid': u, 'vec': v} for (u, _), v in zip(batch, vectors)]},
        )
    return len(items)

async def main():
    driver = AsyncGraphDatabase.driver('bolt://localhost:7687', auth=('neo4j', 'Graphiti123'))
    try:
        n = await reembed_nodes(driver)
        e = await reembed_edges(driver)
        print(f'reembedded {n} entities, {e} edges')
    finally:
        await driver.close()

asyncio.run(main())
