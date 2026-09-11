# Copyright 2024, Zep Software, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from graphiti_core.utils.maintenance.alias_dict import AliasDictionary, load_alias_dictionary
from graphiti_core.utils.maintenance.normalizer import (
    MERGE_THRESHOLD,
    REVIEW_THRESHOLD,
    generate_entity_key,
    normalize_name,
    score_candidate,
    slugify,
)


class TestNormalizeName:
    def test_strip_whitespace(self):
        assert normalize_name('  苹果公司  ') == '苹果公司'

    def test_full_width_to_half_width(self):
        assert normalize_name('Ａｐｐｌｅ　Ｉｎｃ．') == 'apple inc.'
        assert normalize_name('苹果（中国）') == '苹果(中国)'

    def test_collapse_whitespace(self):
        assert normalize_name('foo   bar') == 'foo bar'

    def test_remove_decorative_quotes(self):
        assert normalize_name('《中华人民共和国劳动合同法》') == '中华人民共和国劳动合同法'
        assert normalize_name('"Apple Inc."') == 'apple inc.'

    def test_case_folding(self):
        assert normalize_name('Apple Inc.') == normalize_name('apple inc.')

    def test_matching_key_differs_from_display_name(self):
        assert normalize_name('  Apple  Inc. ') != '  Apple  Inc. '


class TestEntityKey:
    def test_type_and_name(self):
        assert generate_entity_key('Enterprise', 'Apple Inc.') == 'enterprise:apple_inc'

    def test_chinese_kept(self):
        assert generate_entity_key('Court', '最高人民法院') == 'court:最高人民法院'

    def test_same_name_different_types_differ(self):
        assert generate_entity_key('Enterprise', '苹果') != generate_entity_key('Material', '苹果')

    def test_normalization_converges(self):
        assert generate_entity_key('Law', '《劳动合同法》') == generate_entity_key('law', '劳动合同法')

    def test_slugify_punctuation(self):
        assert slugify('Apple, Inc.') == 'apple_inc'


class TestScoring:
    def test_exact_type_and_name_merges(self):
        result = score_candidate('Court', 'Court', '最高人民法院', '最高人民法院')
        assert result.score >= MERGE_THRESHOLD
        assert result.decision == 'merged'

    def test_strong_alias_merges(self):
        from graphiti_core.utils.maintenance.normalizer import AliasHit

        hit = AliasHit(
            canonical_name='最高人民法院',
            canonical_id='supreme_peoples_court',
            entity_type='Court',
            alias_level='strong',
            confidence=1.0,
        )
        result = score_candidate('Court', 'Court', '最高法', '最高人民法院', alias_hit=hit)
        assert result.score >= MERGE_THRESHOLD

    def test_type_conflict_never_merges(self):
        result = score_candidate('Enterprise', 'Material', '苹果', '苹果')
        assert result.score < 0
        assert result.decision != 'merged'

    def test_review_band(self):
        from graphiti_core.utils.maintenance.normalizer import AliasHit

        hit = AliasHit(
            canonical_name='Apple Inc.',
            canonical_id='apple_inc',
            entity_type='Enterprise',
            alias_level='conditional',
            confidence=0.8,
        )
        result = score_candidate(
            'Enterprise',
            'Enterprise',
            '苹果',
            'apple_inc',
            alias_hit=hit,
            same_document=True,
            relations_or_attributes_match=True,
            vector_similarity=0.9,
        )
        assert REVIEW_THRESHOLD <= result.score < MERGE_THRESHOLD
        assert result.decision == 'pending_review'

    def test_conditional_alias_alone_never_merges(self):
        from graphiti_core.utils.maintenance.normalizer import AliasHit

        hit = AliasHit(
            canonical_name='Apple Inc.',
            canonical_id='apple_inc',
            entity_type='Enterprise',
            alias_level='conditional',
            confidence=0.8,
        )
        result = score_candidate('Enterprise', 'Enterprise', '苹果', 'apple_inc', alias_hit=hit)
        assert result.score < MERGE_THRESHOLD

    def test_low_score_kept_separate(self):
        result = score_candidate('Court', 'Court', '某某法院', '另一法院')
        assert result.score < REVIEW_THRESHOLD
        assert result.decision == 'kept_separate'

    def test_vector_similarity_is_small_signal_only(self):
        base = score_candidate('Court', 'Court', '某某法院', '另一法院')
        boosted = score_candidate(
            'Court', 'Court', '某某法院', '另一法院', vector_similarity=0.95
        )
        assert boosted.score - base.score <= 0.20 * 0.95 + 1e-9
        assert boosted.score < MERGE_THRESHOLD


class TestAliasDictionary:
    def test_load_builtin_dictionary(self):
        dictionary = load_alias_dictionary()
        assert dictionary is not None
        hit = dictionary.lookup('最高法', 'Court')
        assert hit is not None
        assert hit.canonical_name == '最高人民法院'
        assert hit.alias_level == 'strong'

    def test_strong_alias_cross_lookup_blocked_for_other_types(self):
        dictionary = AliasDictionary(
            [
                {
                    'canonical_name': '最高人民法院',
                    'canonical_id': 'spc',
                    'entity_type': 'Court',
                    'aliases': ['最高法'],
                    'alias_level': 'strong',
                    'confidence': 1.0,
                }
            ]
        )
        # Registered only as Court: a Person mention "最高法" must not resolve.
        assert dictionary.lookup('最高法', 'Person') is None

    def test_conditional_alias_flagged(self):
        dictionary = AliasDictionary(
            [
                {
                    'canonical_name': 'Apple Inc.',
                    'canonical_id': 'apple_inc',
                    'entity_type': 'Enterprise',
                    'aliases': ['苹果'],
                    'alias_level': 'conditional',
                    'conditions': ['企业上下文'],
                    'confidence': 0.8,
                }
            ]
        )
        hit = dictionary.lookup('苹果', 'Enterprise')
        assert hit is not None
        assert hit.alias_level == 'conditional'

    def test_missing_file_returns_none(self, tmp_path):
        assert load_alias_dictionary(tmp_path / 'missing.yaml') is None
