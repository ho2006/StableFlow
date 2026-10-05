import tempfile
import unittest
from pathlib import Path

from stableflow.analyze import normalize_events, snapshot_from_events, analyze
from stableflow.common import TOKENS, ZERO, load_snapshot


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.c = ['0x' + str(i) * 40 for i in (1, 2, 3)]
        self.labels = [{"address": self.a,"entity":"A","category":"dex","verified":True},
                       {"address": self.b,"entity":"A","category":"dex","verified":True}]

    def event(self, source, target, amount="1000001", index=0):
        return {"block_timestamp":"2026-04-01T01:00:00Z","token_address":TOKENS['USDC']['address'],
                "from_address":source,"to_address":target,"amount_raw":amount,"tx_hash":"0x"+'f'*64,"log_index":index}

    def test_event_accounting_unknown_internal_special_and_duplicates(self):
        events = [self.event(self.c,self.a),self.event(self.a,self.b,index=1),self.event(self.c,ZERO,index=2),
                  self.event(self.c,self.c,index=3),self.event(self.c,self.a,"0",index=4)]
        rows, duplicates = normalize_events(events+[events[0]],self.labels,'2026-04-01','2026-04-02')
        self.assertEqual(duplicates,1)
        self.assertEqual([r['treatment'] for r in rows],['boundary','internal','zero_address','self','zero_amount'])
        self.assertEqual(rows[0]['from_category'],'unknown')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            snapshot_from_events(events+[events[0]],self.labels,'2026-04-01T00:00:00Z','2026-04-02T00:00:00Z',path,
                                 kind='synthetic-test',complete_months=['2026-04'],source={'test':True})
            report=analyze(path)
            self.assertTrue(report['quality']['matrix_reconciled'])
            self.assertAlmostEqual(sum(r['amount'] for r in report['totals']),2.000002)
            row=next(r for r in report['hourly'] if str(r['hour']).startswith('2026-04-01 01') or str(r['hour']).startswith('2026-04-01T01'))
            dex=next(r for r in report['hourly'] if r['category']=='dex' and r['inflow_count']==1)
            self.assertEqual(int(dex['inflow_raw']),1000001)
            self.assertEqual(dex['active_senders'],1)
            self.assertEqual(dex['outflow'],0)

    def test_conflict_missing_and_checksum(self):
        collision=self.labels+[{'address':self.a,'entity':'B','category':'lending','verified':True}]
        rows,_=normalize_events([self.event(self.c,self.a)],collision,'2026-04-01','2026-04-02')
        self.assertEqual(rows[0]['to_category'],'unknown')
        with self.assertRaises(ValueError):
            normalize_events([self.event(self.c,self.a),self.event(self.c,self.a,'9')],self.labels,'2026-04-01','2026-04-02')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            snapshot_from_events([self.event(self.c,self.a)],self.labels,'2026-04-01T00:00:00Z','2026-04-02T00:00:00Z',path,
                                 kind='synthetic-test',complete_months=[],source={'test':True})
            _,hourly=load_snapshot(path)
            self.assertTrue(all(not r['complete'] and r['inflow'] is None for r in hourly))
            with (path/'labels.json').open('a') as stream: stream.write(' ')
            with self.assertRaisesRegex(ValueError,'checksum'):
                load_snapshot(path)

    def test_integer_identity_cannot_be_truncated(self):
        for field in ('chain_id','log_index'):
            bad=self.event(self.c,self.a)
            bad[field]=1.75
            with self.assertRaises(ValueError):
                normalize_events([bad],self.labels,'2026-04-01','2026-04-02')


if __name__=='__main__': unittest.main()
