import unittest
import httpx
from evaluate_meeting_rag import evaluate_retrieval, evaluate_citations, validate_dataset, human_scores


class MetricsTest(unittest.TestCase):
    def test_authenticated_session_keeps_cookie_sends_csrf_and_logs_out_on_error(self):
        from evaluate_meeting_rag import authenticated_session
        paths = []
        def handle(request):
            paths.append(request.url.path)
            if request.url.path == '/api/auth/login':
                return httpx.Response(200, json={'csrf_token':'csrf'}, headers={'set-cookie':'meeting_session=opaque; Path=/api'})
            self.assertEqual(request.headers['x-csrf-token'], 'csrf')
            self.assertIn('meeting_session=opaque', request.headers['cookie'])
            return httpx.Response(204)
        with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with authenticated_session(client, 'eval@example.test', 'private-password'):
                    client.post('/api/chat/queries', json={})
                    raise RuntimeError('interrupted')
            self.assertNotIn('X-CSRF-Token', client.headers)
        self.assertEqual(paths, ['/api/auth/login','/api/chat/queries','/api/auth/logout'])

    def test_login_failure_does_not_echo_credentials_or_server_response(self):
        from evaluate_meeting_rag import authenticated_session
        with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text='private-password'))) as client:
            with self.assertRaisesRegex(ValueError, '^evaluation_login_failed$'):
                with authenticated_session(client, 'eval@example.test', 'private-password'):
                    self.fail('must not query')

    def test_ranked_metrics_and_comparison_reject_different_dataset(self):
        from evaluate_meeting_rag import ranked_metrics, compare_reports
        result = ranked_metrics({'a'}, ['b','a','a'], 3)
        self.assertEqual(result['recall_at_k'], 1.)
        self.assertEqual(result['mrr'], .5)
        self.assertAlmostEqual(result['ndcg_at_k'], 1 / __import__('math').log2(3))
        baseline = {'dataset_hash':'same','evaluation_kind':'retrieval', 'k':3,
                    'cases':[{'id':'q','retrieval':{'recall_at_k':.5,'mrr':.5,'ndcg_at_k':.5}}]}
        candidate = baseline | {'cases':[{'id':'q','retrieval':{'recall_at_k':1.,'mrr':1.,'ndcg_at_k':1.}}]}
        self.assertEqual(compare_reports(baseline, candidate)['delta']['recall_at_k'], .5)
        with self.assertRaisesRegex(ValueError, 'incompatible_reports'):
            compare_reports(baseline, candidate | {'dataset_hash':'different'})

    def test_comparison_scores_failed_cases_on_the_fixed_denominator(self):
        from evaluate_meeting_rag import compare_reports
        metrics = lambda value: {'recall_at_k':value,'mrr':value,'ndcg_at_k':value}
        baseline = {'dataset_hash':'same','k':3,'cases':[
            {'id':'easy','retrieval':metrics(1.)}, {'id':'hard','retrieval':metrics(.5)}]}
        candidate = baseline | {'cases':[{'id':'easy','retrieval':metrics(1.)}, {'id':'hard','http_status':504}]}
        result = compare_reports(baseline, candidate)
        self.assertEqual(result['delta']['recall_at_k'], -.25)
        self.assertEqual(result['candidate_errors'], 1)
        self.assertEqual(result['regressions'], ['hard'])

    def test_comparison_rejects_threshold_and_context_budget_drift(self):
        from evaluate_meeting_rag import compare_reports
        report = {'dataset_hash':'same','k':3,'cases':[{'id':'q','retrieval':{'recall_at_k':1.,'mrr':1.,'ndcg_at_k':1.}}],
                  'server_state':{'min_cosine_similarity':.35,'chat_model':{'id':'chat','context_tokens':8192},
                                  'retrieval_version':'hybrid-v1'}}
        for state in [report['server_state'] | {'min_cosine_similarity':.8},
                      report['server_state'] | {'chat_model':{'id':'chat','context_tokens':4096}}]:
            with self.assertRaisesRegex(ValueError, 'incompatible_reports'):
                compare_reports(report, report | {'server_state':state})
        compare_reports(report, report | {'server_state':report['server_state'] | {'retrieval_version':'lexical-v2'}})

    def test_known_counts_and_zero_denominators(self):
        self.assertEqual(evaluate_retrieval({'a','b'},['a','a','c','b'],2)['recall_at_k'], .5)
        self.assertEqual(evaluate_citations({'x','y'},['x','z','x']), {'precision':.5,'recall':.5})
        self.assertEqual(evaluate_retrieval(set(),[],1)['recall_at_k'], 0.)
        self.assertEqual(evaluate_citations(set(),[]), {'precision':0.,'recall':0.})

    def test_dataset_requires_consent_provenance_annotations(self):
        data={'schema_version':1,'consent':True,'provenance':'synthetic, author generated',
              'split':'held_out','meeting_revisions':{'a':'revision'},'cases':[{'id':'q','question':'Ai?',
              'category':'vietnamese','expected_meeting_ids':['a'],'expected_source_ids':['a:r:0']}]}
        validate_dataset(data)
        for key in ('consent','provenance','meeting_revisions'):
            invalid=dict(data);invalid.pop(key)
            with self.assertRaises(ValueError):validate_dataset(invalid)
        invalid=dict(data);invalid['cases']=[{'id':'q','question':'Ai?'}]
        with self.assertRaises(ValueError):validate_dataset(invalid)

    def test_valid_id_does_not_establish_semantic_support(self):
        self.assertEqual(evaluate_citations({'valid'},['valid'])['precision'],1.)
        self.assertIsNone(human_scores(None))
        self.assertEqual(human_scores([{'supported':False,'usefulness':1}])['factual_support'],0.)

    def test_server_revision_and_configuration_drift_is_rejected(self):
        import evaluate_meeting_rag as evaluation
        dataset={'meeting_revisions':{'a':'r1'}}
        state={'meeting_revisions':{'a':'r2'},'embedding_profile':{'id':'p'},'prompt_hash':'hash'}
        with self.assertRaisesRegex(ValueError,'evaluation_state_drift'):
            evaluation.verify_server_state(dataset,state,'p')
        state['meeting_revisions']={'a':'r1','extra':'r'}
        with self.assertRaisesRegex(ValueError,'evaluation_state_drift'):
            evaluation.verify_server_state(dataset,state,'p')

    def test_human_ratings_require_matching_output_hash(self):
        import evaluate_meeting_rag as evaluation
        with self.assertRaisesRegex(ValueError,'rating_output_mismatch'):
            evaluation.bound_human_scores({'output_hash':'old','claims':[{'supported':True,'usefulness':5}]},'new')
