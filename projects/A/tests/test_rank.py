import copy
from server.common import AppError
from server.rank_facts import RankFacts
from test_sync_fixture import SyncFixture,known,unknown

class RankTests(SyncFixture):
    def setUp(self):super().setUp();self.ranks=RankFacts(self.a)

    def snapshot(self,**changes):
        result={'revision_id':'fictional_rank1','account_key':self.account,'rank_kind':'contribution','period_key':'fictional_30day','period_start_utc':'2026-09-10T16:00:00Z','period_end_utc':'2026-10-10T16:00:00Z','period_label':'fictional actual 30 days','observed_period_key':'fictional_30day','source_id':'fictional','source_version':'1','source_revision':1,'captured_at_utc':'2026-10-10T20:00:00Z','pages':[{'page_number':1,'rows':[{'person_key':self.person,'position':55,'metric':known('12000',precision='display',raw='1.2万')}]}],'pagination_terminal':True}
        result.update(changes);return result

    def test_display_precision_original_position_and_zero_unknown_retained(self):
        self.ranks.stage(self.snapshot());row=self.ranks.for_person(self.account,'contribution','fictional_30day',self.person)['rank']
        self.assertEqual(row['position'],55);self.assertEqual(row['metric']['raw_display'],'1.2万');self.assertEqual(row['metric']['precision'],'display')
        zero=self.snapshot(revision_id='fictional_zero',source_revision=2,pages=[{'page_number':1,'rows':[{'person_key':self.person,'position':80,'metric':known('0')}]}]);self.ranks.stage(zero)
        self.assertEqual(self.ranks.for_person(self.account,'contribution','fictional_30day',self.person)['rank']['metric']['value'],'0')
        self.assertEqual(self.ranks.for_person(self.account,'contribution','fictional_30day','not_in_rank')['metric_state'],'unknown')

    def test_partial_and_wrong_selected_period_retain_previous_cutoff(self):
        self.ranks.stage(self.snapshot());old=self.ranks.get_active(self.account,'contribution','fictional_30day')
        for index,changes in enumerate([{'pagination_terminal':False},{'observed_period_key':'another_tab'},{'pages':[{'page_number':n,'rows':[]} for n in range(1,12)]}]):
            snapshot=self.snapshot(revision_id='partial_'+str(index),source_revision=index+2,**changes);result=self.ranks.stage(snapshot)
            self.assertEqual(result['state'],'partial');self.assertEqual(self.ranks.get_active(self.account,'contribution','fictional_30day')['revision_id'],old['revision_id']);self.assertEqual(self.ranks.get_active(self.account,'contribution','fictional_30day')['effective_cutoff'],old['effective_cutoff'])

    def test_idempotency_reuse_period_independence_and_delayed_snapshot(self):
        snapshot=self.snapshot(source_revision=4);self.ranks.stage(snapshot)
        self.assertEqual(self.ranks.stage(snapshot)['status'],'duplicate')
        with self.assertRaises(AppError):self.ranks.stage(dict(snapshot,captured_at_utc='2026-10-10T21:00:00Z'))
        result=self.ranks.stage(self.snapshot(revision_id='old_revision',source_revision=2));self.assertEqual(result['state'],'superseded')
        other=self.snapshot(revision_id='independent_period',period_key='different_period',observed_period_key='different_period',source_revision=1);self.ranks.stage(other)
        self.assertEqual(len(self.ranks.periods(self.account)),2);self.assertEqual(self.ranks.get_active(self.account,'contribution','fictional_30day')['revision_id'],'fictional_rank1')

    def test_sync_receiver_revalidates_independently_and_does_not_emit(self):
        self.ranks.stage(self.snapshot());receiver=RankFacts(self.b);receiver.register_handlers(self.ib)
        self.assertEqual(self.ib.receive(self.wire(self.events(self.a),channel='collection'))['status'],'applied');self.assertEqual(receiver.get_active(self.account,'contribution','fictional_30day')['state'],'complete')
        self.assertEqual(len(self.events(self.b)),0)

    def test_unverified_actual_adapter_never_activates_fake_rank(self):
        from server.identity import IdentityService
        IdentityService(self.a).register_source('real_dom_unverified','1',source_kind='dom_adapter',capabilities=['rank_rows'],verified=True,evidence={'unverified':True})
        result=self.ranks.stage(self.snapshot(source_id='real_dom_unverified'))
        self.assertEqual(result['state'],'partial');self.assertEqual(self.ranks.get_active(self.account,'contribution','fictional_30day')['state'],'unknown')

    def test_float_duplicate_rank_and_unknown_zero_rejected_atomically(self):
        snapshot=self.snapshot();snapshot['pages'][0]['rows'][0]['metric']['value']=1.2
        with self.assertRaises(AppError):self.ranks.stage(snapshot)
        snapshot=self.snapshot();snapshot['pages'][0]['rows'].append(copy.deepcopy(snapshot['pages'][0]['rows'][0]))
        with self.assertRaises(AppError):self.ranks.stage(snapshot)
        snapshot=self.snapshot();snapshot['pages'][0]['rows'][0]['metric']=dict(unknown(),value='0')
        with self.assertRaises(AppError):self.ranks.stage(snapshot)
        self.assertEqual(len(self.events(self.a)),0)

    def test_source_revocation_suspends_projection_without_deleting_original_snapshot(self):
        from server.identity import IdentityService
        self.ranks.stage(self.snapshot());IdentityService(self.a).revoke_source('fictional','1')
        self.assertEqual(self.ranks.get_active(self.account,'contribution','fictional_30day')['state'],'unknown');self.assertEqual(self.ranks.periods(self.account),[])
        self.assertEqual(self.a.conn.execute('SELECT count(*) FROM rank_revision').fetchone()[0],1)
