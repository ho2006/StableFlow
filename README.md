# StableFlow

**Ethereum 稳定币资金流、可核验使用结构与 PyTorch 预测研究。**

目标是展示从公开链上数据、SQL 核算到时间外模型实验和英文研究表达的完整能力。研究 USDT / USDC 的类别边界流，不把未知地址转账直接解释为支付，也不把神经网络胜出作为完成条件。

## 当前状态

代码包含预算受控的 BigQuery 获取、手动 CSV 导入、DuckDB 精确核算、四种基线、PyTorch GRU、流量特征消融、残差事件、静态交互站点和可执行 Notebook。

已保存官方 Aave / Arbitrum / Uniswap 地址资料与 Uniswap 工厂只读调用证据。CEX 标签暂未覆盖。**完整真实资金流快照尚待 Google 认证和首次查询验证；工程预览使用醒目标记的合成测试数据，不宣称链上研究结论。**

## 快速运行

Python 3.11+；本地验收环境为 Python 3.14、PyTorch CPU。直接使用原生命令，禁止 RTK。

```powershell
Set-Location C:\dev\StableFlow
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
& ./.venv/Scripts/python.exe -m pip install -e . --no-deps --no-build-isolation
& ./.venv/Scripts/python.exe -m unittest discover -v
```

生成并运行**仅用于工程验证**的六个月测试快照：

```powershell
& ./.venv/Scripts/python.exe -m stableflow fixture --output data/snapshots/synthetic-test
& ./.venv/Scripts/python.exe -m stableflow reproduce --snapshot data/snapshots/synthetic-test --output reports/synthetic-test --allow-synthetic --epochs 2
& ./.venv/Scripts/python.exe -m http.server 8765 --directory reports/synthetic-test/site --bind 127.0.0.1
```

打开 `http://127.0.0.1:8765`。默认实验采用最多 50 epochs、三个固定种子及一个消融模型；上面的 `--epochs 2` 是快速集成检查。所有生成结果记录实际参数。

## 获取真实数据

见 [免费数据入口](docs/data-access.md)。在独立 BigQuery Sandbox 项目登录后，将公开项目 ID 填入 `configs/ethereum.json`；凭证不放入仓库。

```powershell
& ./.venv/Scripts/python.exe -m stableflow export-sql --config configs/ethereum.json --output data/sql-export
& ./.venv/Scripts/python.exe -m stableflow fetch --config configs/ethereum.json --output data/snapshots/ethereum
& ./.venv/Scripts/python.exe -m stableflow reproduce --snapshot data/snapshots/ethereum --output reports/ethereum
```

首次只读 schema 检查会锁定字段，dry-run 决定使用十二个月还是六个月。查询上限 800 GiB，包含 200 GiB 核验保留量；超额度即停止。没有完整来源和查询证明的 CSV 不能标为真实研究快照。

## 核算与实验

- 原始金额保存为整数微单位；两种代币独立展示，金额单位不是自动换算的美元。
- 事件唯一键 `chain_id + tx_hash + log_index`。Unknown 保留；同实体、同类别和特殊转账分别核算。
- GRU 读取过去 168 小时，预测下一小时流入和流出；基线为 lag24、lag168、四周同小时中位数和 Ridge。
- 目标小时按日历顺序进入训练、验证、校准和测试；预处理只拟合训练数据，缺口不补零。
- 展示 MAE、WAPE、log-MAE、净流量 MAE 与种子差异。阈值来自独立校准段；异常仍需交易证据解释。

## 审阅入口

| 内容 | 位置 |
|---|---|
| 数据访问和公开证据 | `docs/data-access.md`、`data/evidence/` |
| 完整工程接口 | `docs/contracts.md`、`src/stableflow/` |
| 数据与模型检查 | `tests/` |
| 执行后的实验 Notebook | `notebooks/stableflow.ipynb` |
| 交互站点 | 复现输出的 `site/index.html` |
| 英文研究记录 | 复现输出的 `research/report.html` 与 `report.md` |
| 输入来源与校验和 | 每个快照的 `manifest.json` |
| 训练参数、曲线和权重 | 复现输出的 `model/` |

Notebook 默认阅读明确标记的测试回放；真实回放后设置 `STABLEFLOW_OUTPUT` 指向对应输出目录。英文 HTML 可打印为 A4 研究记录。站点只消费已生成的结果，无需浏览器模型推理或云凭证。

## 开源与发布

代码使用 MIT；来源文档、区块链数据和公开数据集保留各自来源与条款。冻结快照单独发布并保留 SHA-256。GitHub Pages 发布操作见 [发布说明](docs/publishing.md)。发布真实研究前，必须完成真实快照获取、数据核验和模型回放；测试预览始终保留醒目标记。
