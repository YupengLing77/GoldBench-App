# Agent 研究工具

工具定义和路由：`goldbench/agents.py`。黄金市场和宏观数据适配：`goldbench/market_tools.py`。

工具扩展组现在有 **17 个研究工具 + submit_decision**。基础组保持仅有提交工具。
模型初始 `data_catalog.tushare_research` 会列出市场、默认代码、宏观数据集和时间规则。
新工具通过现有 evidence/trace/worker/event 流程留档；不改变模拟账户成交品种。

## 来源与参数

所有新增/扩展市场工具支持 `start_date`、`end_date`（YYYY-MM-DD）、`limit`（默认20，1—100）
和 `offset`（默认0）。默认最近30个日历日；`get_macro` 默认最近366日。
单次日期跨度最多366日，未来结束日期会截到当前研究日。日期参数指日线/事件日期或宏观参考期。

| Agent 工具 | Tushare API | 特有参数和返回 |
| --- | --- | --- |
| `get_gold_futures` | `fut_daily`, `fut_mapping`, `fut_basic` | `ts_code` 默认 `AU.SHF`，也支持 `AU2612.SHF` 等月合约；日线、结算、成交、持仓、主力映射和静态合约定义 |
| `get_gold_spot` | `sge_daily`, `sge_basic` | 默认 `Au99.99`；支持 `Au99.95`, `Au100g`, `iAu99.99`, `Au(T+D)`, `mAu(T+D)`；行情、均价、交收等供应商字段 |
| `get_gold_warehouse` | `fut_wsr` | 固定 SHFE/AU；仓单量、增减、仓库和供应商单位 |
| `get_positioning` | `fut_holding` | `symbol` 默认 `AU`，或 `AU2612`；会员多空排名、增减；也兼容 `dataset` 指定的导入快照 |
| `get_us_yields` | `us_tycr`, `us_trycr` | `kind`: `nominal`, `real`, `both`（默认）、`breakeven`；原始期限字段和年利率百分数单位 |
| `get_macro` | `cn_cpi`, `cn_ppi`, `cn_pmi`, `cn_gdp`, `cn_m`, `sf_month`, `shibor`, `shibor_lpr` | `dataset` 必须指定；支持以上 API 名称和原有导入数据集名；PMI选择制造业/非制造业/综合主要指数 |
| `get_economic_calendar` | `eco_cal`, `cn_schedule` | `calendar`: `global`（默认）或 `china`；全球默认 `country="美国"`；`query` 对事件/标题做本地包含匹配 |

原有 `get_prices`, `search_news`, `read_news`, `get_history`, `read_webpage`, `run_backtest`,
`run_python`, `submit_decision` 保留。`get_prices` 仍读取实验冻结的 XAUUSD 成交数据。
新行情工具返回可供 `run_python` 使用的 `handle`；固定 SMA20 `run_backtest` 仍仅接受
`get_prices` 的 bid/ask 行情句柄，不能把沪金/上金所 OHLC 当成 XAUUSD 执行。

## 模型调用示例

```json
{"name":"get_gold_futures","arguments":{"ts_code":"AU.SHF","limit":5}}
```

```json
{"name":"get_gold_spot","arguments":{"ts_code":"Au99.99","limit":5}}
```

```json
{"name":"get_positioning","arguments":{"symbol":"AU2612","limit":10}}
```

```json
{"name":"get_us_yields","arguments":{"kind":"both","start_date":"2026-10-01","end_date":"2026-10-14"}}
```

```json
{"name":"get_macro","arguments":{"dataset":"cn_cpi","limit":6}}
```

```json
{"name":"get_economic_calendar","arguments":{"country":"美国","query":"CPI"}}
```

## 历史时间与版本

- 国内日线、持仓和仓单使用“数据日之后的北京时间次日09:00”作为保守可用时间假设。
- 美国收益率使用“美国数据日之后的北京时间次日18:00”作为保守可用时间假设。
- 每条供应商记录注明 `source_api`, `data_date`, `available_at`, `observed_at`, `availability_basis`。
  上述日线假设不是逐条验证的发布时间或供应商真实到达时间，仍沿用供应商历史数据可信假设。
- 中国月度/季度 API 返回最新修订值，不能仅根据统计月份或发布日程倒填历史可用时间。
  这些值只在缓存首次观察时间已经早于回测截止时返回，否则返回
  `historical_vintage_unavailable`。历史回测可导入经过核验的当时版本。
- 财经日历的历史预测、前值和日程本身也可能变化；在线结果同样以首次观察时间控制可见性。
  `eco_cal` 的事件时间时区尚未验证，不根据 `date + time` 猜测历史发布时间。
  日程和已公布结果应导入为不同时间版本；未来研究日之后的事件目前不查询。
- 导入记录必须带含时区的 `available_at`。它是导入方对记录完整内容当时可见性的声明；
  应核验修订版本，尤其不要把事后结果放进提前可见的日程记录。
- 静态合约定义注明来自供应商当前定义，不声称恢复历史合约规则变更。主力连续序列不复权，
  换月跳空不能当作可交易收益，当天的主力映射也不能用于选择当天开盘合约。

## 导入快照与工程测试

`research_data` 支持上述全部工具以及原有 `read_webpage`，记录在创建实验时保存：

```json
{
  "research_data": {
    "get_macro": [
      {
        "dataset": "cn_cpi",
        "month": "202609",
        "available_at": "2026-10-14T09:30:00+08:00",
        "nt_yoy": 1.0,
        "source": "example_only_replace_with_verified_vintage"
      }
    ],
    "get_gold_spot": [
      {
        "ts_code": "Au99.99",
        "trade_date": "20261013",
        "available_at": "2026-10-14T09:00:00+08:00",
        "close": 900,
        "unit": "CNY/gram"
      }
    ]
  }
}
```

示例数值只说明格式，不代表真实市场或发布事实。
导入数据优先于在线数据，同一工具配置了快照后不自动混入供应商当前版本。
工程驱动不会隐式访问 Tushare，未准备快照时返回 `dataset_not_prepared`。
真实 Poe 驱动从后端环境读取 `TUSHARE_TOKEN`；无权限、缺密钥、网络失败会明确留档，
不生成工程数据替代。MCP连接的凭证不会自动传给本地应用。

## 缓存、长度和数据口径

- `research_cache` 表按实验、API和参数冻结供应商响应及首次接收时间；跨账户同实验同请求共享。
- 达到供应商条数阈值会按日期递归拆分，单日仍触限则明确失败；不静默截断。
- 不限制实际外部请求次数，120秒后不再开始新请求（在途HTTP请求本身最多30秒）；
  窗口过大时缩短日期范围。
- 结果按最新记录分页，页内按时间顺序排列；大结果进一步按UTF-8字节收缩，给提交决策留下上下文。
  `total_visible`, `returned`, `omitted`, `next_offset` 明确返回数量；下一页使用 `next_offset`。
- 沪金价格元/克，成交量和持仓量为手，成交额万元；上金所价格元/克、成交量千克、成交额元。
  不将人民币/克与XAUUSD美元/盎司直接相减。
- 上金所保留 `raw_pct_change`，另算 `computed_return = change/(close-change)`（小数），避免供应商
  百分数字段口径不一致。`Au(T+D)` / `mAu(T+D)` 标记为延期交收。
- 仓单不是全部库存；会员排名不是全市场投资者净仓位，也不是CFTC COT，缺失排名不可当成零仓位。
- 合约基础信息权限与日线权限可能不同；基础信息不可用会标注状态，仍可返回已取得日线。
- 当前不提供COMEX行情或完整美国宏观历史版本库。

## 官方接口文档

- [期货日线](https://tushare.pro/document/2?doc_id=138)、[主力映射](https://tushare.pro/document/2?doc_id=189)、[期货合约](https://tushare.pro/document/2?doc_id=135)
- [上金所日线](https://tushare.pro/document/2?doc_id=285)、[上金所合约](https://tushare.pro/document/2?doc_id=284)
- [持仓排名](https://tushare.pro/document/2?doc_id=139)、[仓单](https://tushare.pro/document/2?doc_id=140)
- [美国名义利率](https://tushare.pro/document/2?doc_id=219)、[美国实际利率](https://tushare.pro/document/2?doc_id=220)
- [CPI](https://tushare.pro/document/2?doc_id=228)、[PMI](https://tushare.pro/document/2?doc_id=325)、[社融](https://tushare.pro/document/2?doc_id=310)
- [财经日历](https://tushare.pro/document/2?doc_id=233)、[中国发布日程](https://tushare.pro/document/2?doc_id=461)

## 验证

离线测试覆盖各接口路由、时间边界、缓存冻结、条数触限拆分、分页、凭证错误、基础信息权限差异、
快照导入、工程模式禁止联网、以及真实 Agent 工具循环的模拟响应。真实 Poe 兼容和费用仍需独立校准。

## GRAM 补齐工具（2026-11-10）

| 工具 | 参数 | 数据及单位 |
| --- | --- | --- |
| `get_fx_rates` | `ts_code` 默认 `EURUSD.FXCM`；另支持USDJPY、USDCNH、AUDUSD、GBPUSD、USDCAD、USDCHF（均带`.FXCM`），以及`USDOLLAR` | FXCM双边日线，明确基础/报价货币、收盘中间价、报价笔数；美元篮子为指数点，CNH为离岸人民币 |
| `get_gold_etf_shares` | `ts_code` 默认 `518880.SH`；`source_api` 默认`fund_share`，可明确选择`etf_share_size` | 份额归一到份，规模归一到元；附上原单位、两次观测间变化、跟踪标的；不自动换来源 |
| `get_risk_index` | 通用日期和分页参数；固定VIX | 股票30天隐含波动率，标记为补充风险代理，不称为黄金三个月IV |
| `get_us_yields` 扩展 | `kind="breakeven"` | 同日期、同期限名义减实际收益率，单位百分点；附输入值、公式，不补齐缺失期限或跨日拼接 |

通用日期、limit和offset规则继续适用。外汇工具也返回Python句柄；其句柄不能用于固定XAUUSD回测。
FX日线按GMT日期解释，保守在次日北京时间09:00解锁；VIX在次日18:00解锁；ETF份额和规模在次日12:00解锁。
均为显式标注的保守假设，不声称精确复原数据实际到达。

`USDOLLAR` 在2023-06-01以前用`USDOLLAR.FXCM`查询，之后用无后缀代码；跨界窗口会拆分，返回保留
`provider_ts_code`。它是FXCM美元篮子，成分/权重不能当成ICE DXY或GRAM原外汇指数。

ETF白名单由本次 `etf_basic` 核对生成，支持14只Au99.99/SHAU跟踪ETF：
`159812.SZ`, `159934.SZ`, `159937.SZ`, `518660.SH`, `518800.SH`, `518850.SH`, `518880.SH`,
`159830.SZ`, `159831.SZ`, `159834.SZ`, `518600.SH`, `518680.SH`, `518860.SH`, `518890.SH`。
会检查上市日期；黄金产业股票ETF和跟踪元数据未核实品种不混入。
份额来源可能只在变更日有记录，所以返回`previous_observation_date`，不把间隔多日的变化称为单日净申购。
份额未做拆分调整，规模包含估值效应，二者都不直接命名为全球黄金ETF资金流。

```json
{"name":"get_fx_rates","arguments":{"ts_code":"USDCNH.FXCM","limit":5}}
```

```json
{"name":"get_gold_etf_shares","arguments":{"ts_code":"518880.SH","source_api":"fund_share","limit":5}}
```

```json
{"name":"get_risk_index","arguments":{"limit":5}}
```

```json
{"name":"get_us_yields","arguments":{"kind":"breakeven","limit":5}}
```

新增工具支持同名`research_data`导入快照，继承`available_at`校验和工程驱动不联网规则。
Agent初始目录按GRAM五个维度给出工具映射，并列出原始指标仍未匹配的ID。
完整缺项与检索依据见[GRAM缺项清单](GRAM_GAPS.md)和[`goldbench/gram_coverage.json`](../goldbench/gram_coverage.json)。
研究预算临近用尽时，只发送提交工具schema，释放上下文用于提交；不提升原有研究/消费预算。

## 按需工具 Router

**完整目录先给模型，参数定义按需加载。** 每轮开始时，`tool_directory` 会列出全部17个研究工具的
名称、一句话用途和分类；初始仅附4份完整schema：
`get_prices`、`get_history`、`select_tools`、`submit_decision`。
因此模型知道所有工具的存在，可以自主选择研究方向。

Router在 `goldbench/tool_router.py` 实现，本地查注册表，不发起独立模型/供应商请求。
模型调用 `select_tools` 后，下一次模型请求加载选中的参数定义。常驻工具不受替换影响；
不限制可选工具数量，可以一次加载全部工具；selector 始终可用。最终提交模式只发送提交schema。

| 分类 | 可选工具 |
| --- | --- |
| `gold_market` | `get_gold_futures`, `get_gold_spot`, `get_gold_warehouse` |
| `macro` | `get_macro`, `get_economic_calendar` |
| `rates_fx` | `get_us_yields`, `get_fx_rates` |
| `risk_news` | `get_risk_index`, `search_news`, `read_news` |
| `positioning` | `get_positioning`, `get_gold_etf_shares` |
| `analysis` | `run_backtest`, `run_python` |
| `archives` | `read_webpage` |

按分类选择：

```json
{"name":"select_tools","arguments":{"categories":["rates_fx"]}}
```

或按工具选择跨分类组合：

```json
{"name":"select_tools","arguments":{"tools":["get_fx_rates","get_gold_etf_shares","search_news"]}}
```

选择会**替换**上次可选工具，不累积；`{"tools":[]}`可以清空。可同时选择多个分类或全部工具；无效名称明确报错并保持原选择。
本次response中的研究调用只能使用这次请求实际附带的schema：不能在同一批里先选中工具再直接调用，
也不能越过基础组权限。基础组继续只有`submit_decision`。

每个账户、每轮新建独立router，不限制选择次数和研究调用次数。
所有模型请求仍计入原有20k/2k token、600秒和费用预算。
路由过程不生成研究证据，独立记录在event的`routing` / `routing_calls`；数据证据、已取得句柄在切换后继续保留。

研究结果进入上下文前统一按UTF-8字节限制到约5.5KB。超长文本或数组会被明确标记`context_limited`，
保留有效JSON、数据句柄和必要分页字段；模型可以缩小limit或继续分页。
**证据ID基于实际展示的结果生成**；原始大结果另存该工具trace的`raw_result`，不混入裁判使用的可见证据。
这是确定性的截短/采样，不使用另一模型生成摘要，也没有自动删改整段历史对话。

系统指导位于 `goldbench/prompts.py`，版本 `gold-research-v3`：采用审核通过的简洁中文版本，说明收益目标、GRAM 研究视角、风险与成本、主动研究及结构化提交要求，不逐项介绍工具或指定研究路径。本轮账户、时间、规则、历史、目录和预算由 `agents.py` 作为独立状态消息传入。初始完整 schema 仍为4份，这是按需加载的起点，不是数量上限。
