from pydantic import BaseModel, Field


class EntityRecord(BaseModel):
    id: str
    name: str
    canonical_name: str
    type: str
    source_text: str
    attributes: dict = Field(default_factory=dict)


class RelationRecord(BaseModel):
    type: str
    source: str
    target: str
    evidence_text: str
    source_chunk_id: str
    confidence: float = 1.0
