# 全A股市场择时仪表盘：SMA20宽度 × TRIN

**统计范围：沪市 + 深市 + 北交所的全部A股，不再限制于沪深300。** 纯静态网页托管在GitHub Pages；GitHub Actions通过AKShare在中国时间工作日18:30（计划时间，可能延迟）采集**全市场一次性日终快照**，而不是每天对数千只个股逐一下载历史。

## 如何使用（网页）

1. 解压ZIP包，把里面**所有文件和文件夹**上传到你自己的GitHub公开仓库根目录，包括隐藏文件夹 `.github` 和 `data` 文件夹。GitHub网页上传隐藏文件夹不方便时，可使用GitHub Desktop或Git命令行。
2. Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**（不是 Deploy from a branch）。此包的采集工作流会在更新数据后直接部署网站，避免机器人提交数据无法触发分支发布的问题。
3. Actions → `Update all A-share breadth and TRIN` → Run workflow → main → Run workflow，手动触发第一次采集并发布网页（建议A股收盘后）。
4. 成功后仓库中会新增 `data/price_cache.json` 并更新 `data/market.json`。GitHub Pages会展示每日市场TRIN和统计样本信息。之后交易日自动采集。
5. 访问 `https://你的用户名.github.io/你的仓库名/`。

若Pages设置后无法显示数据，先看Actions任务是否为绿色成功状态；如工作流缺少写权限，进入Settings → Actions → General → Workflow permissions开启读写权限。

**若你已部署旧的沪深300版：** 用本ZIP里的全部文件覆盖旧版本，保留GitHub仓库名与Pages设置不变。旧版 `data/price_cache.json` 的结构不同，**需要删除旧文件**；新脚本仅认可新版包含 `snapshots` 的缓存。旧版 `data/market.json` 的沪深300记录**不能保留或拼接**：请上传本包中空的 `data/market.json` 覆盖旧版，避免口径混杂。

## 为什么全A股市场宽度开始时是“数据不足”？

TRIN可从**一天**全市场涨跌家数、对应股票成交量计算；而SMA20需要每只股票连续取得20个有效交易日收盘价。项目不调用昂贵或容易限流的数千次个股历史接口来伪装完整回测。因此：

- 首次成功抓取当日：TRIN显示数值，SMA20宽度显示“数据不足（1/20）”。
- 以后每个有效交易日，项目逐日积累全市场收盘价；在**至少20个成功采集的交易日**后，满足85%以上当日交易样本具备20个有效收盘价，才显示真正全A股宽度。
- TRIN MA5必须有5个有效交易日数据。历史图只显示实际采集到的数值，不填充虚假记录。
- 如某天采集失败，会跳过该天；需后续采集补足20次有效观测，因此实际等待可能超过20个交易日。停牌、不足20日历史、数据缺失会减少有效样本。

## 精确计算口径

- **全A股股票池**：AKShare `stock_zh_a_spot_em()`（东财沪深京A股，无沪深300名单限制），去重后保留有效股票代码。
- **TRIN** = `(上涨家数 / 下跌家数) / (上涨股成交量合计 / 下跌股成交量合计)`。上涨/下跌使用当天涨跌幅正/负，平盘、停牌、成交量为0或行情无效的股票排除。成交量单位一致即可。
- **SMA20宽度** = `今天价格 > 最近20次有效交易日收盘价的简单平均价`的股票数 / 当前可计算SMA20且当天有成交的股票数。
- **防伪机制**：不足3500只股票的行情视为不完整；有效交易股票小于70%也拒绝更新；能计算SMA20的样本覆盖低于当天有效交易股票的85%时，宽度为null并标注进度。
- **参考时效**：仅收盘后日终记录，不是盘中实时。交易所节假日跳过，接口不可用就保留最后一次成功记录。每日数值源于公开第三方接口，可能变化或中断。
- 收盘价为**不复权市场快照价格**。分红除权、拆并股可能影响SMA20比较；严谨回测需要经过验证的复权序列和历史可交易股票池。

## 本地运行（可选）

需Python 3.11+及可用互联网环境：

```bash
pip install -r requirements.txt
python update_data.py
python -m http.server 8000
```

浏览器访问 `http://localhost:8000/`。直接双击 `index.html` 时可用演示和手动模式；自动模式需要HTTP服务器和采集数据。示例/手动值不能当成真实行情。

## 注意

统计范围已统一为全A股，但新口径与旧沪深300宽度（例如之前截图43.3%）**不可直接比较**。规则阈值仅供学习观察，没有经过历史样本外检验，不提供确定的仓位建议。Github Pages公开仓库的代码及行情数据可被公开访问。使用数据请遵守来源条款。

### 首次上传特别说明
- 请把解压后的文件放到仓库**根目录**，不要多套一层文件夹。
- `.github/workflows/update.yml` 必须存在。GitHub 网页拖拽整个隐藏文件夹可能不方便；可在仓库中通过 Add file → Create new file，文件名直接填写 `.github/workflows/update.yml`，复制本包对应内容，然后提交。
- 该更新工作流需要仓库 Settings → Actions → General → Workflow permissions 中的 **Read and write permissions**（部分仓库可能已允许）。
- Settings → Pages → Source 一定选择 **GitHub Actions**。
- GitHub Actions 首次调用行情接口可能失败；任务失败时不要误认为行情已实时更新，点击工作流运行的失败步骤查看日志。
