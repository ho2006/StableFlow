# 发布说明

源码仓库与研究数据分别发布。当前真实资金流获取尚未完成，Notebook 的合成数据只用于工程验证。

## GitHub Pages

先从经过核验的冻结快照生成站点：

```powershell
python -m stableflow reproduce --snapshot data/snapshots/ethereum --output reports/ethereum
```

将 `reports/ethereum/site/` 的内容提交到仓库的 `gh-pages` 分支，再在 GitHub 的 Settings → Pages 中选择该分支的根目录。

工程测试站点使用 `--allow-synthetic` 生成，公开时必须保留页面及导出表中的合成数据标记。源码上传不会自动发布站点。

发布冻结数据时同时提供 `manifest.json`、来源和许可说明、SHA-256 校验和；不要提交 Google 凭证、预算账本或虚拟环境。
