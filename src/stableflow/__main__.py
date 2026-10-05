"""Command-line entry points. Fetch credentials are never needed for frozen replay."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import load_snapshot, read_json, read_parquet, utc, write_json


def attach_evidence(snapshot: Path, experiment: dict) -> None:
    synthetic=read_json(snapshot/'manifest.json')['kind']=='synthetic-test'
    events_path = snapshot / 'events.parquet'
    events = read_parquet(events_path) if events_path.exists() else read_json(snapshot/'evidence.json') if (snapshot/'evidence.json').exists() else []
    for anomaly in experiment.get('anomalies', []):
        start = anomaly.get('start') or anomaly.get('start_hour') or anomaly.get('hour')
        end = anomaly.get('end') or anomaly.get('end_hour') or start
        if start is None:
            continue
        selected = [row for row in events if row.get('token') == anomaly.get('token')
                    and row.get('hour') is not None and utc(start) <= utc(row['hour']) < utc(end)
                    and anomaly.get('category') in (row.get('from_category'),row.get('to_category'))
                    and row.get('treatment')=='boundary']
        selected.sort(key=lambda row:int(row['amount_raw']), reverse=True)
        anomaly['evidence'] = [{**row,'amount':int(row['amount_raw'])/1e6,
                                'url': None if synthetic else f"https://etherscan.io/tx/{row['tx_hash']}",
                                'evidence_kind': 'synthetic-test' if synthetic else 'real'} for row in selected[:5]]
        anomaly['evidence_status'] = 'available; requires investigation' if selected else 'pending bounded transaction query'
        anomaly.setdefault('investigation','temporarily unexplained')


def reproduce(snapshot: Path, output: Path, *, allow_synthetic=False, epochs=50, seeds=(7,42,2026)) -> dict:
    from .analyze import analyze
    from .ml import run_experiment
    from .report import build_report
    from .site import build_site
    manifest, rows = load_snapshot(snapshot)
    if manifest['kind']=='synthetic-test' and not allow_synthetic:
        raise ValueError('Synthetic fixtures require --allow-synthetic and cannot establish research findings')
    output.mkdir(parents=True,exist_ok=True)
    analysis=analyze(snapshot)
    write_json(output/'analysis.json',analysis)
    experiment=run_experiment(rows,manifest,output/'model',epochs=epochs,seeds=tuple(seeds))
    experiment['output_dir']=str((output/'model').resolve())
    experiment['predictions_path']=str((output/'model'/'predictions.parquet').resolve())
    attach_evidence(snapshot,experiment)
    write_json(output/'model'/'anomalies.json',experiment.get('anomalies',[]))
    write_json(output/'experiment.json',experiment)
    build_report(analysis,experiment,output/'research')
    index=build_site(snapshot,analysis,experiment,output/'site')
    receipt={'snapshot':str(snapshot.resolve()),'kind':manifest['kind'],'epochs_limit':epochs,'seeds':list(seeds),
             'matrix_reconciled':analysis['quality']['matrix_reconciled'],'model_status':experiment.get('status'),
             'site':str(index.resolve()),'research':str((output/'research'/'report.html').resolve())}
    write_json(output/'receipt.json',receipt)
    return receipt


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description='Ethereum stablecoin flows and PyTorch research')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('fetch','export-sql'):
        command=commands.add_parser(name)
        command.add_argument('--config',type=Path,default=Path('configs/ethereum.json'))
        command.add_argument('--output',type=Path,required=True)
    command=commands.add_parser('import-csv')
    command.add_argument('--config',type=Path,default=Path('configs/ethereum.json'))
    command.add_argument('--input',type=Path,required=True)
    command.add_argument('--output',type=Path,required=True)
    command=commands.add_parser('fixture',help='Generate explicitly synthetic engineering test data')
    command.add_argument('--output',type=Path,default=Path('data/snapshots/synthetic-test'))
    command=commands.add_parser('reproduce')
    command.add_argument('--snapshot',type=Path,required=True)
    command.add_argument('--output',type=Path,required=True)
    command.add_argument('--allow-synthetic',action='store_true')
    command.add_argument('--epochs',type=int,default=50)
    command.add_argument('--seeds',type=int,nargs='+',default=[7,42,2026])
    command=commands.add_parser('analyze')
    command.add_argument('--snapshot',type=Path,required=True)
    command.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='fixture':
            from .demo import make_fixture
            if (args.output/'manifest.json').exists() and read_json(args.output/'manifest.json')['kind']!='synthetic-test':
                raise ValueError('Refusing to overwrite a non-synthetic snapshot')
            result=make_fixture(args.output)
        elif args.command in ('fetch','export-sql','import-csv'):
            from . import fetch
            function={'fetch':fetch.fetch,'export-sql':fetch.export_sql,'import-csv':fetch.import_csv}[args.command]
            result=function(args.config,args.input,args.output) if args.command=='import-csv' else function(args.config,args.output)
        elif args.command=='analyze':
            from .analyze import analyze
            result=analyze(args.snapshot)
            write_json(args.output,result)
        else:
            if args.epochs<1 or args.epochs>50 or 42 not in args.seeds:
                raise ValueError('Epochs must be 1..50 and seeds must contain the predefined main seed 42')
            result=reproduce(args.snapshot,args.output,allow_synthetic=args.allow_synthetic,epochs=args.epochs,seeds=args.seeds)
        import json
        from .common import clean_json
        print(json.dumps(clean_json(result),ensure_ascii=False,indent=2))
        return 0
    except (ValueError,FileNotFoundError,PermissionError,RuntimeError) as exc:
        print(f'StableFlow: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
