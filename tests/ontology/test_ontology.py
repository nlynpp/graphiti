from ontology.build_sample_graph import entities_for
from ontology.models import EntityRecord, RelationRecord
from ontology.validator import OntologyValidator


def test_entity_evidence_and_type_are_enforced():
    validator = OntologyValidator()
    valid = EntityRecord(id='1', name='国家', canonical_name='国家', type='Organization', source_text='国家')
    assert validator.validate_entity(valid, '国家保护合法权益') == []
    invalid = valid.model_copy(update={'type': 'Invented', 'source_text': '不存在'})
    assert len(validator.validate_entity(invalid, '国家保护合法权益')) == 2


def test_relation_constraints_are_enforced():
    validator = OntologyValidator()
    entities = {
        'd': EntityRecord(id='d', name='法', canonical_name='法', type='LegalDocument', source_text='法'),
        'o': EntityRecord(id='o', name='机关', canonical_name='机关', type='Organization', source_text='机关'),
    }
    relation = RelationRecord(type='ISSUED_BY', source='d', target='o', evidence_text='法由机关发布', source_chunk_id='c1')
    assert validator.validate_relation(relation, entities, '法由机关发布', 'c1') == []
    assert validator.validate_relation(relation.model_copy(update={'source': 'o'}), entities, '法由机关发布', 'c1')


def test_metadata_title_is_not_used_as_content_evidence():
    chunk = {'page_content': '正文只有国家', 'metadata': {'document_id': 'd', 'document_title': '某法', 'title': '正文', 'chunk_id': 'c'}}
    assert all(entity['name'] != '某法' for entity in entities_for(chunk))
