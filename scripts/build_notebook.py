"""Author and execute the reviewable companion notebook using saved replay outputs."""
from pathlib import Path
import os
import nbformat
from nbclient import NotebookClient

root=Path(__file__).resolve().parents[1]
for variable, subdirectory in [('IPYTHONDIR','ipython'),('JUPYTER_RUNTIME_DIR','jupyter'),('MPLCONFIGDIR','matplotlib')]:
    directory=root/'.cache'/subdirectory
    directory.mkdir(parents=True,exist_ok=True)
    os.environ[variable]=str(directory)
nb=nbformat.v4.new_notebook()
nb.metadata.kernelspec={'display_name':'Python 3','language':'python','name':'python3'}
nb.cells=[
    nbformat.v4.new_markdown_cell('''# StableFlow: research and forecasting replay

This companion inspects saved SQL/Python/PyTorch outputs. The default package is **synthetic engineering test data**, not Ethereum research evidence. Set `STABLEFLOW_OUTPUT` to a completed real-data replay directory to inspect real findings.

## Context & methods
Ethereum mainnet USDT / USDC; native token units, UTC, exclusive interval end. Verified endpoint labels identify protocol associations, not user motives. A shared GRU uses 168 previous hours; seasonal and Ridge references use the same eligible target hours. Calibration is separate from final test.'''),
    nbformat.v4.new_code_cell('''import os
from pathlib import Path
import matplotlib.pyplot as plt
from IPython.display import display, HTML
from stableflow.common import read_json, read_parquet, utc
root = Path.cwd()
output = Path(os.environ.get("STABLEFLOW_OUTPUT", str(root / "reports/synthetic-test")))
analysis = read_json(output / "analysis.json")
experiment = read_json(output / "experiment.json")
manifest = analysis["manifest"]
synthetic = manifest["kind"] == "synthetic-test"
notice = "SYNTHETIC ENGINEERING TEST — NOT ON-CHAIN EVIDENCE" if synthetic else "Historical Ethereum research"
print(notice)
print("Interval:", manifest["start"], "to", manifest["end"], "UTC / exclusive end")
print("Matrix reconciled:", analysis["quality"]["matrix_reconciled"])
print("Common split targets:", experiment["target_counts"])
print("Validation-selected baseline:", experiment["best_baseline"])
assert analysis["quality"]["matrix_reconciled"]
plt.rcParams.update({"figure.figsize":(10,4),"axes.spines.top":False,"axes.spines.right":False,"font.size":10})'''),
    nbformat.v4.new_markdown_cell('''## Data: observed transfer totals
Eligible value excludes zero-address, self and zero-amount transfers. Values remain separate by token. Coverage is value touching at least one verified endpoint; Unknown is not a payments label.'''),
    nbformat.v4.new_code_cell('''fig, axes = plt.subplots(2,1,figsize=(10,6),layout="constrained")
for ax, token in zip(axes,["USDC","USDT"]):
    rows = [r for r in analysis["totals"] if r["token"]==token and r["complete"]]
    ax.plot([utc(r["date"]) for r in rows],[r["amount"] for r in rows],color="#086f75",label="Eligible transfers")
    ax.plot([utc(r["date"]) for r in rows],[r["known_amount"] for r in rows],color="#cf8b39",label="Touches verified endpoint")
    ax.set_ylabel(token+" native units"); ax.legend(loc="upper left")
axes[-1].set_xlabel("UTC date")
fig.suptitle(notice+"\\nDaily transfer value and label coverage")
plt.show()'''),
    nbformat.v4.new_markdown_cell('''## Results: final holdout forecast comparison
All references and GRU variants are evaluated on identical target hours. Macro WAPE averages nonzero-denominator token/category/direction groups. These numbers are evidence of software execution when the input is synthetic, and do not establish on-chain predictive performance.'''),
    nbformat.v4.new_code_cell('''scores = [r for r in experiment["macro_metrics"] if r["split"]=="test"]
names = [r["model"]+(" / "+str(r["seed"]) if r["seed"] is not None else "") for r in scores]
values = [100*r["macro_wape"] if r["macro_wape"] is not None else 0 for r in scores]
fig, ax = plt.subplots(figsize=(10,4),layout="constrained")
ax.barh(names,values,color=["#086f75" if r["model"]=="gru" else "#82959b" for r in scores])
ax.set_xlabel("Macro WAPE (%) — lower is better")
ax.set_title(notice+"\\nFinal test comparison")
plt.show()
print("GRU seed variability:",experiment["seed_variability"])
print("Independent anomaly truth labels: unavailable")'''),
    nbformat.v4.new_markdown_cell('''## Takeaways and evidence boundaries
Use the site's evidence view for source versions and transaction investigation status. Fixed-label retrospective performance does not establish live availability or complete exchange coverage. Residual flags become available after the actual hour ends; they do not identify fraud or an attack.'''),
    nbformat.v4.new_code_cell('''for finding in analysis["findings"]: print(finding)
print("Queued anomaly events:",len(experiment["anomalies"]))
for event in experiment["anomalies"][:3]:
    print(event["token"],event["category"],event["start"],event["investigation"],event.get("evidence_status","pending"))
if synthetic: print("No economic finding is claimed from this notebook.")'''),
]
nbformat.validate(nb)
NotebookClient(nb,timeout=180,kernel_name='python3',resources={'metadata':{'path':str(root)}}).execute()
target=root/'notebooks/stableflow.ipynb'
target.parent.mkdir(exist_ok=True)
nbformat.write(nb,target)
print(target)
