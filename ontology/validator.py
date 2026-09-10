from .legal_ontology import ENTITY_TYPES, RELATIONS
from .models import EntityRecord, RelationRecord


class OntologyValidator:
    def validate_entity(self, entity: EntityRecord, source: str) -> list[str]:
        errors = []
        if entity.type not in ENTITY_TYPES:
            errors.append(f'unknown entity type: {entity.type}')
        if not entity.source_text or entity.source_text not in source:
            errors.append('entity source_text not found in source')
        return errors

    def validate_relation(self, relation: RelationRecord, entities: dict[str, EntityRecord], source: str, chunk_id: str) -> list[str]:
        errors = []
        spec = RELATIONS.get(relation.type)
        if not spec:
            return [f'unknown relation type: {relation.type}']
        if relation.source_chunk_id != chunk_id:
            errors.append('relation source_chunk_id is not current chunk')
        if relation.evidence_text not in source:
            errors.append('relation evidence_text not found in source')
        if relation.source in entities and entities[relation.source].type not in spec[0]:
            errors.append('source entity type violates relation constraint')
        if relation.target in entities and entities[relation.target].type not in spec[1]:
            errors.append('target entity type violates relation constraint')
        return errors
