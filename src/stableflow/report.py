"""A printable English research record whose claims are tied to generated evidence."""
from __future__ import annotations

import html
import platform
from pathlib import Path

from .common import write_json


def build_report(analysis: dict, experiment: dict, output: Path) -> Path:
    output.mkdir(parents=True,exist_ok=True)
    manifest=analysis['manifest']
    synthetic=manifest['kind']=='synthetic-test'
    status='SYNTHETIC ENGINEERING TEST — NOT ON-CHAIN EVIDENCE' if synthetic else 'Historical Ethereum on-chain research'
    metrics=experiment.get('metrics',[])
    if isinstance(metrics,dict): metrics=metrics.get('rows',[])
    test=[m for m in metrics if m.get('split')=='test']
    lines=['# StableFlow: Ethereum stablecoin flows and forecasting',f'**{status}**',
           f"Observed interval: {manifest['start']} to {manifest['end']} (UTC, exclusive end).",'## 1. Research question and evidence',
           'The study separates verifiable endpoint associations from inferred motives. It asks where USDC and USDT move within a stated address universe, whether their observed protocol structures differ, and whether a small neural model improves next-hour forecasts.',
           'Each transfer is identified by chain, transaction hash and log index. Six-decimal token quantities retain integer precision in snapshots. USDC and USDT are reported separately; quantities are not automatically valued in dollars. Addresses are not unique people.',
           'Source snapshot files are SHA-256 checked before replay. Labels retain their evidence URL, retrieval date and known validity interval. Unknown endpoints stay unknown. Current-only labels describe a fixed-label historical replay, not contemporaneous recognition.',
           *[f'- {text}' for text in analysis.get('findings',[])],
           '## 2. Accounting and observable usage',
           'Both endpoints receive unique categories before aggregation. Boundary inflows cross from another category; boundary outflows cross to another category. Transfers inside a category remain in the matrix but do not inflate category boundary flows. Known intra-entity transfers can be shown separately.',
           'Zero-address, self and zero-amount transfers are excluded from eligible-value totals and disclosed as special events. The Transfer table alone does not establish complete token issuance or destruction. One edge contributes once to the flow matrix; adding all category inflows and outflows double-counts edge value.',
           'A transfer involving a DEX pool establishes a protocol association. It does not by itself establish trade volume, a payment or customer intent. The Ethereum side of an Arbitrum gateway does not prove a completed cross-chain journey. Unknown-to-unknown flows cannot be called payments.',
           f"Verified model categories: {', '.join(manifest['categories'])}. Uncovered categories: {', '.join(analysis['quality']['uncovered_categories']) or 'none'}. Unidentified wallets within covered categories remain a source of measurement error.",
           f"Matrix reconciliation: {analysis['quality']['matrix_reconciled']}. Incomplete hourly rows: {analysis['quality']['incomplete_hourly_rows']}. Duplicate events removed: {analysis['quality']['duplicates_removed']}.",
           '## 3. Forecasting experiment',
           'A shared one-layer PyTorch GRU with 32 hidden units reads the preceding 168 hours. It predicts log1p inflow and outflow; net flow is their difference. Sequence identity and target calendar features are explicit inputs. The global feature scaler is fit only on training observations.',
           'Seasonal references use the previous day, previous week and median of the four preceding matching weekly hours. Ridge uses lagged flows, rolling statistics and calendar features. All models receive the same complete-history target hours. The first 28 days are a warm-up.',
           'Twelve-month snapshots use eight training months, one validation month, one calibration month and two test months. Six-month snapshots use three, one, one and one. Validation selects the strongest baseline and early stopping; calibration and test never update weights.',
           'Three predetermined seeds measure training variation, with seed 42 as the main display. A flow-only ablation checks whether transaction activity contributes useful information. Huber loss, Adam, gradient clipping and a 50-epoch ceiling are fixed before holdout evaluation.',
           'Original-unit MAE, WAPE and log-MAE are reported by token, category and direction. Zero-denominator WAPE is unavailable. Net-flow error uses MAE. The project does not require the neural model to win.',
           f"Experiment status: {experiment.get('status','pending')}. Test metric rows: {len(test)}. Runtime: Python {platform.python_version()}.",
           '## 4. Residual events and investigation',
           'Each series and direction has a fixed threshold at the calibration absolute-log-residual 95th percentile. Residuals above that threshold become observation candidates only after actual flow is known. This is retrospective detection, not advance risk prediction or a calibrated probability of an attack.',
           'Consecutive candidate hours are combined. The highest-ranked events are accompanied by evidence requests or available contribution transactions. Investigation states are evidence-supported business event, data/label issue, or temporarily unexplained. Unreviewed cases remain unexplained.',
           f"Events included in the investigation queue: {len(experiment.get('anomalies',[]))}. No independent anomaly ground truth is available, so anomaly accuracy and fraud precision are not reported.",
           '## 5. Interpretation and reproducibility',
           'The strongest conclusion supported by this package is limited to the stated chain, contracts, interval and verified address coverage. Sparse or partial entity labels can make internal exchange movement appear to cross into Unknown. Data missingness is preserved instead of substituted with zero.',
           'The model evaluates an event-time historical replay. Source arrival delays are not measured, and live availability is not assumed. A single test period cannot establish cross-cycle stability. Real extreme events remain in the holdout; documented source errors require a revised snapshot.',
           'Replay verifies snapshots, rebuilds indicators, runs all baselines and the GRU, writes metrics and weights, and builds the site. Compare count outputs exactly and floating results with disclosed tolerances in an identical dependency environment. Cross-device bitwise training identity is not promised.',
           'If this is a synthetic package, all patterns and forecast scores are software-test outputs only. Genuine economic findings require an authenticated public-data query or an imported, provenance-complete real extract.',
           'Source references: Google Blockchain Analytics schema; Circle USDC contract addresses; Tether supported protocols; Uniswap Ethereum deployments; Aave address-book; Arbitrum contract addresses. The snapshot contains the selected evidence and versions.']
    markdown='\n\n'.join(lines)+'\n'
    (output/'report.md').write_text(markdown,encoding='utf-8')
    body=[]
    for line in lines:
        if line.startswith('# '): body.append('<h1>'+html.escape(line[2:])+'</h1>')
        elif line.startswith('## '): body.append('<h2>'+html.escape(line[3:])+'</h2>')
        elif line.startswith('**'): body.append('<p class="status">'+html.escape(line.strip('*'))+'</p>')
        else: body.append('<p>'+html.escape(line)+'</p>')
    template='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>StableFlow research record</title><style>body{max-width:850px;margin:50px auto;padding:0 24px;font:16px/1.65 Georgia,serif;color:#142c38;background:#fff}h1{font:700 34px/1.2 system-ui}h2{font:650 23px system-ui;border-top:1px solid #cbd8da;padding-top:24px;margin-top:44px}.status{font-weight:bold;background:#fff1d6;padding:16px}a{color:#006e75}@media print{@page{size:A4;margin:20mm}body{margin:0;padding:0;font-size:11pt}h2{break-before:page;margin-top:0}h1{font-size:26pt}p{orphans:3;widows:3}}</style><body>BODY</body></html>'''
    target=output/'report.html'
    target.write_text(template.replace('BODY','\n'.join(body)),encoding='utf-8')
    write_json(output/'provenance.json',{'snapshot':manifest,'experiment_status':experiment.get('status'),'kind':manifest['kind']})
    return target
