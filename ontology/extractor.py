"""Ontology-constrained LLM extraction with deterministic evidence checks."""
import json
from pydantic import BaseModel, Field

from .legal_ontology import ENTITY_TYPES, RELATIONS, ONTOLOGY_ID, ONTOLOGY_VERSION
from .models import EntityRecord, RelationRecord
from .validator import OntologyValidator
from graphiti_core.prompts.models import Message


class ExtractedEntity(BaseModel):
    id: str
    name: str
    canonical_name: str
    type: str
    source_text: str
    attributes: dict = Field(default_factory=dict)


class ExtractedRelation(BaseModel):
    type: str
    source: str
    target: str
    evidence_text: str
    confidence: float = 1.0


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)


def build_prompt(text: str, chunk_id: str) -> str:
    relation_spec = {k: {'source': sorted(v[0]), 'target': sorted(v[1])} for k, v in RELATIONS.items()}
    return f'''你是法律知识图谱抽取器。只从原文抽取明确表达的事实，不要推断。
本体版本：{ONTOLOGY_ID} {ONTOLOGY_VERSION}
允许实体类型：{sorted(ENTITY_TYPES)}
允许关系及端点：{json.dumps(relation_spec, ensure_ascii=False)}
规则：实体必须提供原文 source_text；关系必须提供原文 evidence_text；关系只能使用白名单类型，且端点类型必须符合约束；逐句识别所有可独立指代的法律概念、对象、条件和数值，不得遗漏关系两端实体；“是……的补充/组成部分/替代”等明确语义使用对应关系（如 SUPPLEMENTS）；无法判断类型使用 UnknownEntity；只返回 JSON。
当前 chunk_id：{chunk_id}
原文：\n{text}
输出格式：{{"entities": [{{"id":"e1","name":"原文名","canonical_name":"规范名","type":"EntityType","source_text":"证据","attributes":{{}}}}], "relations": [{{"type":"关系","source":"e1","target":"e2","evidence_text":"原文证据","confidence":0.0}}]}}'''


class OntologyExtractor:
    def __init__(self, llm_client):
        self.llm_client = llm_client
        self.validator = OntologyValidator()

    async def extract(self, text: str, chunk_id: str) -> tuple[ExtractionResult, list[str]]:
        response = await self.llm_client.generate_response(
            [Message(role='user', content=build_prompt(text, chunk_id))],
            response_model=ExtractionResult,
            prompt_name='ohn_ontology_extraction',
        )
        result = ExtractionResult.model_validate(response)
        entities = {e.id: EntityRecord(**e.model_dump()) for e in result.entities}
        errors = [f'entity {e.id}: {x}' for e in entities.values() for x in self.validator.validate_entity(e, text)]
        for relation in result.relations:
            checked = RelationRecord(**relation.model_dump(), source_chunk_id=chunk_id)
            errors.extend(f'relation {relation.type}: {x}' for x in self.validator.validate_relation(checked, entities, text, chunk_id))
        return result, errors
