# GRAM 数据覆盖与缺项清单

最近核对：**2026-11-10（北京时间）**。

本清单以用户提供的世界黄金协会 GRAM 图中的变量为依据。
机器可读来源为 [`goldbench/gram_coverage.json`](../goldbench/gram_coverage.json)，
Agent 的初始数据目录会同步给出维度与尚未匹配的原始变量 ID。

核对范围：已连接的 Tushare 工具目录、Tushare 官方接口文档/搜索，以及少量历史样本查询。
“本次未找到”不等于证明 Tushare 永远不存在该数据；不把缺权限、空样本与不存在接口混为一谈。

## 已找到并加入 Agent

| 数据能力 | Agent 入口 | Tushare 来源 | 验证与语义 |
| --- | --- | --- | --- |
| 欧元、日元、离岸人民币、澳元等主要货币对 | `get_fx_rates` | `fx_daily` / `fx_obasic` 核对代码 | MCP已返回EURUSD/USDJPY/USDCNH/AUDUSD的2026-10-12至14日样本；明确基础/报价货币，CNH不标成CNY |
| FXCM美元篮子 | `get_fx_rates(ts_code="USDOLLAR")` | `fx_daily` | MCP样本已返回；遵循2023-06-01代码去后缀规则；不标成ICE DXY或GRAM原外汇指数 |
| 境内黄金ETF份额 | `get_gold_etf_shares` | `fund_share` | MCP已返回518880.SH样本；万份转换为份；支持经ETF基础信息核对的14只Au99.99/SHAU实物金价跟踪ETF |
| 境内黄金ETF规模 | 同上，`source_api="etf_share_size"` | `etf_share_size` | MCP已返回份额和规模；万元转换为元；规模差额和份额差额分开，不命名为资金净流入 |
| VIX辅助风险指标 | `get_risk_index` | `vix_index` | MCP已返回历史样本；标注为标普500未来30天股票隐含波动率，作为补充风险指标 |
| 同期限盈亏平衡通胀率近似值 | `get_us_yields(kind="breakeven")` | `us_tycr` + `us_trycr` | 同日期、同期限名义减实际收益率；不跨日补值；单位百分点，保留输入值和公式 |

此前已接入：黄金现货/期货、会员持仓、仓单、美债收益率、中国宏观与财经日历、新闻。
中国GDP/PMI是经济扩张维度的显式扩展，截图中的原GRAM该项是常数。
黄金滞后收益可由已有历史行情和 `run_python` 计算；残差是归因模型输出，不单列缺失数据接口。

## 原始变量仍缺失或口径未匹配

| ID | 缺项 | 当前状态 | 已查范围及不能直接替代的原因 | 可用辅助工具 |
| --- | --- | --- | --- | --- |
| `equity_bond_flows` | GRAM口径股债资金流 | 本次未找到匹配接口 | `moneyflow`是A股主动订单流，韩国基金流仅覆盖韩国，均未匹配原模型跨资产流量口径 | 暂无直接匹配 |
| `gold_3m_implied_volatility` | 黄金三个月隐含波动率 | 本次未找到专用序列 | 期权合约/价格接口不等于已构建的三个月黄金IV；VIX是股票30天IV，不混用 | `get_risk_index` 提供VIX辅助信息 |
| `federal_reserve_assets` | 美联储总资产时间序列 | 本次未找到 | 国际宏观目录确认了美国利率，但未找到美联储资产负债表对应接口 | 暂无直接匹配 |
| `geopolitical_risk_index` | 地缘政治风险指数GPR | 本次未找到 | 新闻提供事件证据，不直接提供命名GPR指数数值 | `search_news`、`read_news` |
| `developed_market_fx_index` | GRAM原口径发达市场外汇指数 | 原定义与序列未匹配 | 已找到货币对及FXCM美元篮子，尚未匹配原指数成分、权重与历史序列 | `get_fx_rates` |
| `emerging_market_fx_index` | GRAM原口径新兴市场外汇指数 | 原定义与序列未匹配 | 同上；USDCNH是离岸人民币，不能直接替作原口径CNY指数 | `get_fx_rates` |
| `global_gold_etf_flows` | 全球黄金ETF资金流 | 本次未找到匹配接口 | 境内ETF份额只覆盖部分市场，规模受价格影响；美股ETF成交量也不等于申赎资金流 | `get_gold_etf_shares` |
| `comex_gold_net_long` | COMEX黄金净多头 | 本次未找到 | 未找到对应CFTC/COT序列；沪金会员排名不是COMEX投资者分类净多头 | `get_positioning` |

## 品种层面的待核验项

- `159832.SZ` 平安上海金ETF、`159833.SZ` 大成上海金ETF：本次 `etf_basic` 名称有返回，
  但 `index_code` / `index_name` 为空。当前未加入白名单，核验跟踪标的后再开放。
- 黄金产业股票ETF未纳入实物金价跟踪ETF清单，避免把矿业股仓位当成黄金ETF持金需求。

## 使用条件与历史版本

MCP验证使用连接器自己的权限；本地应用仍需后端环境中的 `TUSHARE_TOKEN`。
`etf_share_size` 官方文档列8000积分、VIX列5000积分，权限失败明确返回，不自动换数据源。
日线/ETF数据的可用时间沿用工具文档注明的保守假设，尚未逐条核验实际供应商到达时间。
宏观修订值、财经日历的历史版本不足是另一个限制：即使API存在，也不能把今天取得的版本提前给历史Agent。

## 后续维护规则

1. 新找到接口时，更新JSON中的状态、官方文档链接、核对日期、样本范围和口径说明。
2. 明确区分“原始变量”“替代指标”“缺权限”“无样本”“历史版本不足”。
3. 接入工具、数据目录、时间过滤和测试后，才从原始缺项移除；仅找到替代指标时保留缺项。
4. 更新本文件，保留检索依据。不要为了填满维度虚构指数或把近似值重命名为原指标。

## 核对来源

- [世界黄金协会GRAM](https://www.gold.org/goldhub/tools/gold-return-attribution-model)
- [Tushare数据目录](https://tushare.pro/document/2?doc_id=342)
- [外汇基础信息](https://tushare.pro/document/2?doc_id=178)、[外汇日线及代码迁移](https://tushare.pro/document/2?doc_id=179)
- [基金份额](https://tushare.pro/document/2?doc_id=207)、[ETF份额规模](https://tushare.pro/document/2?doc_id=408)
- [VIX](https://tushare.pro/document/2?doc_id=498)、[A股资金流](https://tushare.pro/document/2?doc_id=170)
- [国内期货会员排名](https://tushare.pro/document/2?doc_id=139)、[美债名义收益率](https://tushare.pro/document/2?doc_id=219)、[美债实际收益率](https://tushare.pro/document/2?doc_id=220)
