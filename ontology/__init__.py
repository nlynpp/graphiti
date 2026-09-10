"""OHN-GraphRAG ontology constrained extraction primitives."""

from .models import EntityRecord, RelationRecord
from .extractor import OntologyExtractor
from .validator import OntologyValidator

__all__ = ['EntityRecord', 'RelationRecord', 'OntologyExtractor', 'OntologyValidator']
