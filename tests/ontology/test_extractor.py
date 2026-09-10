from ontology.extractor import build_prompt


def test_prompt_contains_ontology_constraints_and_chunk():
    prompt = build_prompt('国家保护私营经济。', 'chunk_1')
    assert 'UnknownEntity' in prompt
    assert 'ISSUED_BY' in prompt
    assert 'chunk_1' in prompt
