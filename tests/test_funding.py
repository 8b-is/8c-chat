import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import funding
import hosted
from worker import save


class FundingTests(unittest.TestCase):
    def test_paid_refunded_and_disputed_are_reconciled_without_donor_identity(self):
        item = {'id': 'cs_one', 'payment_link': 'plink_one', 'livemode': True,
                'payment_status': 'paid', 'currency': 'usd', 'amount_total': 500,
                'payment_intent': {'latest_charge': {'paid': True, 'disputed': False, 'amount_refunded': 100}}}
        self.assertEqual(funding.stripe_total([item], 'plink_one'), 400)
        self.assertEqual(funding.stripe_total([{**item, 'payment_status': 'unpaid'}], 'plink_one'), 0)
        with self.assertRaises(ValueError): funding.stripe_total([item, item], 'plink_one')
        with self.assertRaises(ValueError): funding.stripe_total([item], 'plink_other')
        item['payment_intent']['latest_charge']['disputed'] = True
        self.assertEqual(funding.stripe_total([item], 'plink_one'), 0)

    def test_pool_is_shared_replay_safe_fresh_and_conservative_on_unknown_outcomes(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            snapshot = root/'pool.json'
            snapshot.write_text(json.dumps({'version':1, 'checked_at':time.time_ns()//1_000_000_000, 'allocated_micro_usd':2_000_000}))
            pool = funding.Pool(snapshot, root/'receipts')
            self.assertTrue(pool.reserve('a'*64))
            self.assertFalse(pool.reserve('a'*64))
            self.assertTrue(pool.reserve('b'*64))
            self.assertEqual(pool.available(), 0)
            pool.settle('a'*64, '0.1000001')
            self.assertEqual(pool.available(), 899999)
            pool.settle('b'*64, None)
            self.assertEqual(pool.available(), 899999)
            snapshot.write_text(json.dumps({'version':1, 'checked_at':1, 'allocated_micro_usd':2_000_000}))
            self.assertEqual(pool.available(), 0)

    def test_verified_shared_allocation_wakes_same_identity_in_another_room(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            snapshot = root/'pool.json'
            save(snapshot, {'version':1,'checked_at':time.time_ns()//1_000_000_000,'allocated_micro_usd':0})
            agent = {'id':'astra','name':'Astra','runtime':'opencode','provider':'openai','model':'gpt-6-astra',
                     'binary':'/usr/bin/opencode','fallback_provider':'openrouter','fallback_model':'openai/gpt-6-astra'}
            bridge = hosted.Bridge({'agents':[agent],'funding_snapshot':str(snapshot)}, root/'commons')
            try:
                save(bridge.state/'astra-pause.json',{'until':time.time_ns()//1_000_000_000+900})
                body = {**agent,'agent':'astra'}
                self.assertFalse(bridge.handle('/inventory',body)['available'])
                save(snapshot, {'version':1,'checked_at':time.time_ns()//1_000_000_000,'allocated_micro_usd':4_000_000})
                self.assertTrue(bridge.handle('/inventory',body)['available'])
                body['job']={'id':1,'lease':'funded','room':'hot-tub','prompt':'Hello everyone','system':'One continuing Astra identity'}
                with patch.object(hosted,'request',return_value={}), patch.object(hosted,'opencode',return_value=('Hello everyone','ses_funded',10,[],'1/10')) as generate:
                    result=bridge.handle('/infer',body)
                    self.assertEqual(result,bridge.handle('/infer',body))
                    self.assertEqual(generate.call_count,1)
                self.assertEqual(result['inference']['provider'],'openrouter')
                self.assertEqual(result['work']['workspace'],'8c-commons/astra')
                self.assertEqual(bridge.pool.available(),3_900_000)
            finally:bridge.lock.close()
