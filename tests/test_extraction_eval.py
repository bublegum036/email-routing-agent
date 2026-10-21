import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import ROOT
from email_router.demo_model import DemoModel
from email_router.evaluate import assess_result
from email_router.extraction_eval import assess_extraction, main
from email_router.llm import ModelError
from email_router.runner import Runner
from email_router.store import Store


def proposal(category='order', ref='O-52', quote='Когда отгрузят O-52?'):
    return dict(category=category, reference_id=ref, quote=quote,
                subject='Срок отгрузки', company_hint='')


class ExtractionEvaluationTests(unittest.TestCase):
    def test_extended_development_expectations_in_both_modes(self):
        suite = json.loads((ROOT / 'data/evaluation-dev-v2.json').read_text())
        for case in suite['cases']:
            for mode in ('workflow', 'agent'):
                with self.subTest(message=case['message'], mode=mode), tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory) / 'state.sqlite')
                    try:
                        store.seed(ROOT / 'data/seed.json')
                        initial = store.all('tickets')
                        report = Runner(store, DemoModel(), mode).run(case['message'])
                        self.assertEqual(assess_result(case, store, report, initial), [])
                    finally:
                        store.close()

    def test_joint_matching_and_count_ignore_order_but_reject_crossed_references(self):
        message = {'text': 'Когда отгрузят O-52? Не работает по D-21.'}
        expected = [{'category': 'order', 'references': ['O-52']},
                    {'category': 'technical', 'references': ['D-21']}]
        result = [proposal('technical', 'D-21', 'Не работает по D-21.'), proposal()]
        self.assertEqual(assess_extraction(expected, result, message), [])
        result[0]['reference_id'], result[1]['reference_id'] = 'O-52', 'D-21'
        self.assertIn('wrong_extracted_reference_or_issue_link', assess_extraction(expected, result, message))
        self.assertIn('wrong_extracted_categories_or_count', assess_extraction(expected, result[:1], message))

    def test_quote_reference_and_company_cannot_be_invented(self):
        message = {'text': 'Когда отгрузят O-52?'}
        expected = [{'category': 'order', 'references': ['O-52']}]
        for field, value, error in [
                ('quote', 'придуманная цитата', 'ungrounded_extracted_quote'),
                ('company_hint', 'Орион', 'ungrounded_extracted_company'),
                ('reference_id', 'O-5', 'ungrounded_extracted_reference'),
                ('subject', ' ', 'empty_extracted_subject')]:
            with self.subTest(field=field):
                self.assertIn(error, assess_extraction(expected, [proposal() | {field: value}], message))

    def test_dirty_challenge_contains_contrasts_multi_issue_and_unknown(self):
        suite = json.loads((ROOT / 'data/extraction-challenge-v1.json').read_text())
        self.assertEqual(suite['data_classification'], 'synthetic')
        self.assertEqual(len(suite['cases']), 18)
        self.assertEqual(len({c['id'] for c in suite['cases']}), 18)
        for case in suite['cases']:
            for expected in case['expected']:
                for anchor in expected.get('quote_any', []):
                    self.assertIn(anchor, case['message']['text'])
        categories = {p['category'] for c in suite['cases'] for p in c['expected']}
        self.assertEqual(categories, {'order', 'technical', 'approval', 'purchase', 'unknown', 'forbidden'})

    def test_successful_route_cannot_hide_wrong_raw_category_or_repaired_reference(self):
        suite = json.loads((ROOT / 'data/evaluation-dev-v2.json').read_text())
        case = suite['cases'][1]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite')
            try:
                store.seed(ROOT / 'data/seed.json')
                initial = store.all('tickets')
                report = Runner(store, DemoModel(), 'workflow').run(case['message'])
                self.assertEqual(assess_result(case, store, report, initial), [])
                for field, value, error in [
                        ('category', 'purchase', 'wrong_extracted_categories_or_count'),
                        ('reference_id', '', 'wrong_extracted_reference_or_issue_link')]:
                    changed = copy.deepcopy(report)
                    raw = next(e for e in changed['events'] if e['kind'] == 'model_response')
                    call = raw['payload']['message']['tool_calls'][0]['function']
                    args = json.loads(call['arguments'])
                    args['issues'][0][field] = value
                    call['arguments'] = json.dumps(args)
                    self.assertIn(error, assess_result(case, store, changed, initial))
            finally:
                store.close()

    def test_inbox_challenge_preserves_v1_and_rejects_false_sales_requests(self):
        original = json.loads((ROOT / 'data/extraction-challenge-v1.json').read_text())
        suite = json.loads((ROOT / 'data/extraction-challenge-v2.json').read_text())
        self.assertEqual(suite['cases'][:len(original['cases'])], original['cases'])
        self.assertEqual(suite['data_classification'], 'synthetic')
        self.assertEqual(len(suite['cases']), 34)
        self.assertEqual(len({c['id'] for c in suite['cases']}), 34)
        self.assertEqual(len({c['message']['id'] for c in suite['cases']}), 34)
        added = suite['cases'][len(original['cases']):]
        self.assertEqual({c['group'] for c in added}, {
            'event', 'supplier_offer', 'buyer_request', 'marketing', 'spam', 'phishing', 'mixed'})
        for case in added:
            with self.subTest(case=case['id']):
                expected = case['expected']
                self.assertEqual(case['message']['data_classification'], 'synthetic')
                for item in expected:
                    for anchor in item['quote_any']:
                        self.assertIn(anchor, case['message']['text'])
                # Evaluator contract test only; these authored proposals are not model results.
                raw = proposal(expected[0]['category'], expected[0]['references'][0], case['message']['text'])
                self.assertEqual(assess_extraction(expected, [raw], case['message']), [])
                wrong = 'purchase' if expected[0]['category'] != 'purchase' else 'unknown'
                self.assertIn('wrong_extracted_categories_or_count', assess_extraction(
                    expected, [raw | {'category': wrong}], case['message']))

    def test_government_and_complaints_preserve_v2_and_validate_annotations(self):
        from email_router.shipping_change import synthetic_seed
        original = json.loads((ROOT / 'data/extraction-challenge-v2.json').read_text())
        suite = synthetic_seed(json.loads((ROOT / 'data/extraction-challenge-v3.json').read_text()))
        self.assertEqual(suite['cases'][:34], original['cases'])
        self.assertEqual(len(suite['cases']), 54)
        self.assertEqual(len({c['id'] for c in suite['cases']}), 54)
        self.assertEqual(len({c['message']['id'] for c in suite['cases']}), 54)
        added = suite['cases'][34:]
        self.assertEqual(len(added), 20)
        self.assertEqual({c['group'] for c in added}, {
            'government_bailiff', 'government_court', 'government_fine',
            'government_impersonation', 'complaint_nondelivery', 'complaint_shortage',
            'complaint_delay', 'complaint_quality', 'complaint_legal', 'complaint_mixed'})
        for case in added:
            with self.subTest(case=case['id']):
                self.assertEqual(case['message']['data_classification'], 'synthetic')
                self.assertTrue(case['operational_note'])
                for item in case['expected']:
                    for anchor in item['quote_any']:
                        self.assertIn(anchor, case['message']['text'])

    def test_complaint_evaluation_rejects_sales_and_crossed_issue_references(self):
        suite = json.loads((ROOT / 'data/extraction-challenge-v3.json').read_text())
        cases = {c['id']: c for c in suite['cases'][34:]}
        for case in cases.values():
            # Intentionally wrong classification, not scripted model inference.
            wrong = [proposal('purchase', item['references'][0], case['message']['text'])
                     for item in case['expected']]
            self.assertIn('wrong_extracted_categories_or_count', assess_extraction(
                case['expected'], wrong, case['message']), case['id'])
        for ident in ('bailiff-information-request', 'court-summons', 'government-fine',
                      'quality-legal-refund', 'delay-legal-penalty'):
            case = cases[ident]
            self.assertEqual(case['expected'][0]['category'], 'unknown')
            wrong = [proposal('order', '', case['message']['text'])]
            self.assertIn('wrong_extracted_categories_or_count', assess_extraction(
                case['expected'], wrong, case['message']), ident)
        case = cases['delay-and-quality']
        crossed = [proposal('order', 'T-9', 'Когда отгрузят O-52?'),
                   proposal('technical', 'O-52', 'помогите с ремонтом')]
        self.assertIn('wrong_extracted_reference_or_issue_link', assess_extraction(
            case['expected'], crossed, case['message']))

    def test_three_commercial_questions_require_coverage_of_each_question(self):
        previous = json.loads((ROOT / 'data/extraction-challenge-v3.json').read_text())
        suite = json.loads((ROOT / 'data/extraction-challenge-v4.json').read_text())
        self.assertEqual(suite['cases'][:54], previous['cases'])
        self.assertEqual(len(suite['cases']), 55)
        self.assertEqual(len({c['id'] for c in suite['cases']}), 55)
        self.assertEqual(len({c['message']['id'] for c in suite['cases']}), 55)
        case = suite['cases'][-1]
        self.assertEqual(case['message']['data_classification'], 'synthetic')
        self.assertEqual(len(case['expected']), 3)
        self.assertEqual({p['category'] for p in case['expected']}, {'order'})
        self.assertEqual({tuple(p['references']) for p in case['expected']}, {('O-52',)})
        # Authored oracle probes: no model inference or routing is claimed.
        answers = [proposal('order', 'O-52', line[3:]) for line in
                   case['message']['text'].splitlines()[1:4]]
        for expected, answer in zip(case['expected'], answers):
            self.assertTrue(any(part in answer['quote'] for part in expected['quote_any']))
        self.assertEqual(assess_extraction(case['expected'], list(reversed(answers)), case['message']), [])
        for missing in range(3):
            with self.subTest(missing=missing):
                problems = assess_extraction(case['expected'], answers[:missing] + answers[missing + 1:], case['message'])
                self.assertIn('wrong_extracted_categories_or_count', problems)
                self.assertIn('wrong_extracted_reference_or_issue_link', problems)
        merged = [proposal('order', 'O-52', case['message']['text'])]
        self.assertIn('wrong_extracted_categories_or_count', assess_extraction(
            case['expected'], merged, case['message']))
        self.assertIn('wrong_extracted_reference_or_issue_link', assess_extraction(
            case['expected'], [answers[0]] * 3, case['message']))
        wrong_order = copy.deepcopy(answers)
        wrong_order[2]['reference_id'] = 'O-51'
        self.assertIn('wrong_extracted_reference_or_issue_link', assess_extraction(
            case['expected'], wrong_order, case['message']))

    def test_unrelated_request_pairs_only_change_input_order(self):
        previous = json.loads((ROOT / 'data/extraction-challenge-v4.json').read_text())
        suite = json.loads((ROOT / 'data/extraction-challenge-v5.json').read_text())
        self.assertEqual(suite['cases'][:55], previous['cases'])
        self.assertEqual(len(suite['cases']), 61)
        self.assertEqual(len({c['id'] for c in suite['cases']}), 61)
        self.assertEqual(len({c['message']['id'] for c in suite['cases']}), 61)
        pairs = {}
        for case in suite['cases'][55:]:
            self.assertEqual(case['message']['data_classification'], 'synthetic')
            pairs.setdefault(case['permutation_group'], []).append(case)
        self.assertEqual(set(pairs), {'purchase-technical', 'order-event', 'four-unrelated'})
        for name, pair in pairs.items():
            with self.subTest(pair=name):
                self.assertEqual(len(pair), 2)
                a, b = pair
                self.assertEqual(a['expected'], b['expected'])
                self.assertNotEqual(a['message']['text'], b['message']['text'])
                self.assertCountEqual(a['message']['text'].split('\n\n'), b['message']['text'].split('\n\n'))
                # Probe evaluator invariance using authored proposals, not model results.
                raw = [proposal(item['category'], item['references'][0], item['quote_any'][0])
                       for item in a['expected']]
                for case in pair:
                    self.assertEqual(assess_extraction(case['expected'], list(reversed(raw)), case['message']), [])
                    for missing in range(len(raw)):
                        self.assertIn('wrong_extracted_categories_or_count', assess_extraction(
                            case['expected'], raw[:missing] + raw[missing + 1:], case['message']))
        case = pairs['four-unrelated'][0]
        raw = [proposal(item['category'], item['references'][0], item['quote_any'][0])
               for item in case['expected']]
        for item in raw:
            if item['category'] == 'technical':
                item['reference_id'] = 'O-52'
        self.assertIn('wrong_extracted_reference_or_issue_link', assess_extraction(
            case['expected'], raw, case['message']))

    def test_live_error_is_saved_and_stops_without_offline_fallback(self):
        class FailedModel:
            model = 'test-model'
            provider = 'gemini'
            generation_config = {}
            def complete(self, messages, tools, timeout):
                # Gold answers and evaluator annotations must not enter model input.
                self_check = json.dumps(messages)
                assert 'quote_any' not in self_check and 'expected' not in self_check
                raise ModelError('Model HTTP 429', http_status=429)
        with tempfile.TemporaryDirectory() as directory:
            with patch('email_router.extraction_eval.ROOT', Path(directory)), \
                    patch('email_router.extraction_eval.ChatModel', FailedModel), \
                    redirect_stdout(io.StringIO()):
                args = ['--suite', str(ROOT / 'data/extraction-challenge-v1.json'), '--model', 'test-model']
                self.assertEqual(main(args), 1)
                self.assertEqual(main(args), 1)
            runs = list((Path(directory) / 'artifacts').iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                summary = json.loads((run / 'summary.json').read_text())
                self.assertTrue(summary['stopped'])
                self.assertEqual(summary['pending'], 17)
                self.assertEqual(len(summary['checks']), 1)
                self.assertTrue((run / summary['checks'][0]['trace']).exists())
