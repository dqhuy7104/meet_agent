import unittest
from evaluate_meeting_rag import evaluate_retrieval, evaluate_citations, validate_dataset, human_scores


class MetricsTest(unittest.TestCase):
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
