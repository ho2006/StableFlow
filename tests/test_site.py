"""Small offline-site contract and injection checks."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from stableflow.common import write_json
from stableflow.site import WARNING, _csv, build_site


class SiteTest(unittest.TestCase):
    def test_offline_payload_and_safe_embed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            snapshot=root/'snapshot'
            snapshot.mkdir()
            manifest={"schema_version":1,"kind":"synthetic-test","start":"2025-10-01T00:00:00Z","end":"2026-10-01T00:00:00Z","categories":["dex"],"source":{"name":"</script><script>alert(1)</script>"},"files":{},"complete_months":[]}
            write_json(snapshot/'manifest.json',manifest)
            write_json(snapshot/'labels.json',[])
            edge={"date":"2025-10-01","token":"USDC","from_category":"unknown","to_category":"dex","treatment":"boundary","amount":12.5,"amount_raw":"12500000","transfer_count":1}
            predictions=[{"split":"test","model":"gru","seed":42,"hour":"2026-09-01T00:00:00Z"}, {"split":"test","model":"gru","seed":7,"hour":"2026-09-01T00:00:00Z"}, {"split":"validation","model":"gru","seed":42,"hour":"2026-06-01T00:00:00Z"}]
            result=build_site(snapshot,{"manifest":manifest,"edges":[edge],"hourly":[],"totals":[],"quality":{},"findings":[]},{"status":"pending","anomalies":[],"predictions":predictions},root/'site')
            document=result.read_text(encoding='utf-8')
            self.assertIn(WARNING,document)
            self.assertNotIn("</script><script>alert(1)</script>",document)
            match=re.search(r'<script id="stableflow-data" type="application/json">(.*?)</script>',document,re.S)
            payload=json.loads(match.group(1))
            self.assertEqual(payload['manifest']['source']['name'],manifest['source']['name'])
            self.assertEqual(payload['edges'][0]['amount'],12.5)
            self.assertEqual(payload['predictions'],predictions[:1])
            self.assertTrue((root/'site'/'plotly.min.js').is_file())
            self.assertNotIn('cdn.plot.ly',document)
            for marker in ('start-filter','token-filter','category-filter','internal-toggle','downloadCSV','safeLink','tabindex="-1"'):
                self.assertIn(marker,document)
            self.assertIn('Research starts with evidence.',document)
            self.assertTrue((root/'site'/'downloads'/'flows.csv').exists())
            self.assertIn(WARNING,(root/'site'/'downloads'/'flows.csv').read_text(encoding='utf-8-sig'))
            self.assertIn('validation',(root/'site'/'downloads'/'predictions.csv').read_text(encoding='utf-8-sig'))

    def test_csv_spreadsheet_formula_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'table.csv'
            _csv(path,[{"entity":"  =HYPERLINK(\"bad\")","amount":-3},{"entity":"ordinary","amount":1}])
            content=path.read_text(encoding='utf-8-sig')
            self.assertIn("'  =HYPERLINK",content)
            self.assertIn('-3',content)


if __name__=='__main__':
    unittest.main()
