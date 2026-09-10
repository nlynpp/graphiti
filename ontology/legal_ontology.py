ONTOLOGY_ID = 'legal_ontology'
ONTOLOGY_VERSION = '1.0.1'

ENTITY_TYPES = {
    'Organization', 'GovernmentAgency', 'Court', 'Enterprise', 'LegalDocument',
    'Constitution', 'Law', 'Regulation', 'JudicialInterpretation',
    'AdministrativeNotice', 'LegalConcept', 'Right', 'Obligation', 'Liability',
    'Procedure', 'LegalAct', 'Remedy', 'Person', 'Case', 'Party', 'Article',
    'Condition', 'Exception', 'Penalty', 'Amount', 'Date', 'Region', 'Evidence',
    'Material', 'UnknownEntity',
}

RELATIONS = {
    'ISSUED_BY': ({'LegalDocument'}, {'Organization', 'GovernmentAgency', 'Court'}),
    'AMENDS': ({'LegalDocument'}, {'LegalDocument'}),
    'REPEALS': ({'LegalDocument'}, {'LegalDocument'}),
    'CONTAINS_ARTICLE': ({'LegalDocument'}, {'Article'}),
    'DEFINES': ({'LegalDocument'}, {'LegalConcept'}),
    'SUPPLEMENTS': ({'LegalConcept'}, {'LegalConcept'}),
    'APPLIES_TO': ({'LegalConcept'}, {'Person', 'Organization', 'Party'}),
    'REQUIRES': ({'LegalConcept'}, {'Condition'}),
    'EXCEPTS': ({'LegalConcept'}, {'Exception'}),
    'HAS_PENALTY': ({'LegalConcept'}, {'Penalty'}),
    'HAS_AMOUNT': ({'LegalConcept'}, {'Amount'}),
    'EFFECTIVE_ON': ({'LegalDocument'}, {'Date'}),
    'APPLIES_IN': ({'LegalDocument'}, {'Region'}),
    'CITES': ({'LegalDocument', 'Article', 'Content'}, {'LegalDocument', 'Article', 'Content'}),
}
