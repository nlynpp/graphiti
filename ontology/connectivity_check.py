import asyncio
import json
import os

from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.llm_client.config import LLMConfig
from ontology.extractor import OntologyExtractor


async def main():
    with open('/tmp/chunks.jsonl', encoding='utf-8') as fh:
        chunk = json.loads(next(fh))
    config = LLMConfig(
        api_key=os.environ['OPENAI_API_KEY'],
        base_url=os.environ['OPENAI_BASE_URL'],
        model=os.environ.get('MODEL_NAME', 'qwen-turbo'),
    )
    extractor = OntologyExtractor(OpenAIClient(config=config))
    result, errors = await extractor.extract(chunk['page_content'], chunk['metadata']['chunk_id'])
    print(f'connected entities={len(result.entities)} relations={len(result.relations)} errors={errors[:5]}')


asyncio.run(main())
