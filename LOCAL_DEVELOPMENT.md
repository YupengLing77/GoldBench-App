# GoldBench 本地历史回测 v0.1.0

依据[设计概要](docs/DESIGN.md)实现的第一版可运行本地应用。完整本地记录位于docs/local/，不随GitHub仓库提交。React/TypeScript + FastAPI + 独立Python worker + SQLite。

## 启动

当前环境已安装依赖并完成前端生产构建。在项目根目录的PowerShell执行：

```powershell
.\scripts\start_goldbench.ps1
```

浏览器打开 http://127.0.0.1:8765 。关闭浏览器不停止worker；关机后重启脚本即可恢复服务。普通工程回测可从已完成轮次恢复。停止前建议先在界面暂停任务并等待落定，然后执行：

```powershell
.\scripts\stop_goldbench.ps1
```

服务只绑定127.0.0.1，无公网部署。启动器仅从进程或用户环境读取TUSHARE_TOKEN和POE_API_KEY，不展示密钥。不需要Poe密钥即可运行工程验证。

## 用法

1. 新建历史实验，默认工程行情 + 确定性测试驱动，免费。默认4模型名称×2组×2重复及3基准，共19条轨迹。测试驱动不是对应模型，所有页面明确标识。
2. 点击数据预检，再创建运行。总览自动更新，模型比较按组展开，回放选择账户和日期。未完成收益是暂定值，不进入正式主榜。
3. 评分页先独立匿名人工复核。真实模型完整实验可启动两裁判评分；查看机器分前先提交独立人工分。机器NA/分歧>=2/关键证据冲突触发复核，另按模型×组分层随机20%并覆盖两次重复。
4. 运行页可暂停/恢复及导出JSON/CSV；SQLite位于runtime/bench.sqlite，原始请求、证据、账户和复核版本均持久化。
5. 行情可选data/local/中的已有十月审计、直接Tushare按需读取并冻结，或高级JSON导入。审计里的周五缺行保留为缺价；不填成正常市场数据。真实Poe实验缺关键价格时预检拒绝；工程驱动可用于验证缺价分支。

## 真实模型启动门槛

Poe Chat Completions原生工具循环已实现，但本次未进行真实付费校准。按照[Poe兼容接口](https://creator.poe.com/docs/external-applications/openai-compatible-api)和[工具调用文档](https://creator.poe.com/docs/external-applications/tool-calling)接入，不使用网页聊天或自动代换模型。

必须先完成实际bot、工具支持、推理参数、内置联网关闭、计费usage验证；再在高级配置写入paid_enabled=true、compatibility_verified=true、各模型及裁判当前单价与已验证参数。不可把此标记当成系统已经替你完成校准。

```json
{
  "driver": "poe",
  "dataset": "import",
  "paid_enabled": false,
  "compatibility_verified": false,
  "prices": {},
  "model_parameters": {},
  "rows": [],
  "research_data": {
    "get_macro": [],
    "get_positioning": [],
    "read_webpage": []
  }
}
```

prices按bot名称映射为{"input":单价,"output":单价}，单位美元/百万token。模型参数仅允许reasoning_effort、thinking_budget，不允许覆盖系统工具和预算。具体Poe支持参数以校准结果为准，不静默删除不支持参数。

rows每行需要date（YYYY-MM-DD）、bid_open、ask_open、bid_close、ask_close，可加tick_qty。必须覆盖每个预定轮次、至少60轮背景及下一轮期末端点。来源、日期解锁、字段单位须由导入方验证；配置冻结后不修改。价格与配置保存SHA256。

月账本上限10美元，调用前原子预留保守最大费用，取得usage后用冻结单价估算。这里不是Poe官方积分账单，应校准后核对。启动预检按累计20k/2k最坏研究用量和3.5美元裁判缓冲估算；实际证据裁判若超出预留，仍由逐次月预算拦截，不保证整月一定够用。

对明确429最多额外重试1次；未知完成状态、缺usage、服务端不确定错误保留在途预留并阻止自动恢复，避免重复消费。此版本对未知付费检查点采取人工核账后恢复的保守策略，尚未实现供应商请求自动对账。不会为失败模型换用测试驱动。

## 研究工具状态

工具扩展组使用按需router：初始目录包含全部17个工具，完整schema先提供价格、历史、选择和提交4个；select_tools按需加载任意数量可选工具，切换从下一请求生效。不限制路由和研究调用次数，但所有模型请求仍共用token/费用预算。详见[Agent工具说明](docs/AGENT_TOOLS.md#按需工具-router)。

- GRAM补齐：get_fx_rates（主要货币对/FXCM美元篮子）、get_gold_etf_shares（境内黄金ETF份额/规模）、get_risk_index（VIX辅助风险）、get_us_yields(kind="breakeven")。仍缺的原始变量维护在[GRAM缺项清单](docs/GRAM_GAPS.md)。
- 新增/扩展市场工具：get_gold_futures、get_gold_spot、get_gold_warehouse、get_us_yields、get_economic_calendar，以及在线get_macro/get_positioning；数据来源、参数、时间和修订版本限制见[Agent工具说明](docs/AGENT_TOOLS.md)。
- get_prices：已实现XAUUSD已解锁日线，500行上限及数据句柄。其他外汇/上金所/ETF日线白名单的自动适配仍需补充，不宣称已具备多品种实测。
- search_news/read_news：新浪、财联社、长篇新浪按时间窗获取/触限递归切分/去重缓存/正文清洗/关键词过滤/时间排序/证据留档；真实API需账号权限，测试驱动不会暗中联网。
- get_macro/get_positioning兼容导入快照并支持上述Tushare来源。月度/季度宏观和财经日历在线数据只从首次观察时间可见，缺历史版本返回historical_vintage_unavailable；不将当前修订值倒填到历史发布日期。read_webpage仍读取带available_at的存档。工程驱动不隐式联网。
- get_history：只读自己的最近30轮，单次至多20，自动提供5轮。未成熟标签不进入提示词。
- run_backtest：已获取句柄上的固定SMA20，缺连续bar保持，最后取得bar开盘统一退出；不开放参数自动搜索。
- run_python：仅在已安装Docker且本地存在python:3.11-slim镜像时可用。network=none、只读根目录、无宿主挂载、非root、资源限额、20秒超时与强制清理；不会自动拉镜像。在当前环境未实测Docker隔离，工具会明确返回不可用。正式工具扩展组启动前须验证此能力。

全部工具返回16k字符上限，真实Poe输入以保守UTF-8字节估算上界、usage对账；因此可能比原token上限更早结束研究，尚未接入各模型精确tokenizer。

## 已验证与尚未验证

已验证：工程数据19轨迹闭环、基准、清算净值、满仓/翻仓/点差/佣金/舍入/破产、日历与as-of、缺价NA/整组暂停、断点幂等、月预算原子预留、导出、匿名复核。模拟Poe响应验证提交循环和双裁判分歧/人工替代，不冒充真实调用。

未验证：真实Poe兼容/计费，Docker实际隔离，Tushare新闻和新增数据在线拉取。十月真实行情覆盖仍不足。暂未实现前瞻常驻调度和真实到达延迟回放、融资费启用、全部研究品种适配；当前是本地历史版本，不等于全部研究设计已交付。

## 测试和构建

```powershell
New-Item -ItemType Directory -Force tmp | Out-Null
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=tmp/pytest-local
cd frontend
npm run build
```

如运行在限制Windows事件循环内部套接字的沙箱，ASGI测试/服务器会卡在socketpair；需允许本机套接字，而不是修改代码跳过测试。

新机器安装（Python 3.11+、Node 22+）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd frontend
npm ci
npm run build
```

SQLite备份须在服务停止后复制整个runtime目录，或使用SQLite在线备份；不要运行中仅拷贝主db漏掉WAL。首次完成真实试跑前，不将界面测试轨迹用于论文或模型能力排名。

### DeepSeek 十月模拟交易

`configs/deepseek_october_run.json` 使用百炼北京 `deepseek-v4-pro-0813`、单账户扩展组和审核后的 `gold-research-v3`。本次共享总预算为 1,000,000 tokens（含首次失败尝试及兼容性校准），通过原实验的 `token_budget_parent` 汇总并在每次请求前原子预留。密钥沿用被 git 忽略的 `runtime/qwen.env`，不会写入配置或请求 JSON。

用户明确同意排除 2026-10-02、09、16、23、30 五个缺价周五，共17个模型决策日；配置中 `holidays` 在本实验表示用户批准的排除日期，并非真实交易所休市日。期末按既有规则在 2026-11-02 开盘统一退出，包含周末持仓风险。报价不填充、不移动日期。此结果不能称为完整22个交易日回测。

DeepSeek 每轮研究预算为累计30k输入/10k输出、600秒，每次最多3k输出并为提交预留；最终提交允许超出单轮保守输入估算以避免无决策退出，但全实验1M硬上限始终在供应商请求边界执行。续轮估算复用已观测输入token数，对新增消息/工具定义按UTF-8字节加上协议余量保守预留；移除定义不返还估算额度。若某日未成功提交，调度器在该日检查点后暂停，避免连续付费失败。

费用以美元保守估算（每百万输入1.5、输出4.5），不是供应商账单；参考百炼北京该快照忙时原价为人民币9/27，未计缓存和闲时折扣。准确账单以百炼为准。

显式运行入口：`.venv/bin/python -m goldbench.experiment --config configs/deepseek_october_run.json --prepare`；首次准备后用 `--resume`。状态文件独立保存，重复调用不自动启动已存在实验。历史请求状态不明时拒绝自动恢复。

### 模拟过程监测页面

现有网站新增 `/monitor`（原工作台侧栏也有入口），使用 Sixmind 风格的深色、奶油白标题与薄荷绿配色。可通过 `?experiment=<id>` 指定实验；前端每2秒读取只读 `/api/experiments/{id}/live`。提供净值/收益率曲线、基准排名、日期回放、模型理由、实际研究调用次数、请求状态与工具参数/结果展开。暂停刷新只影响页面，不改变实验运行。接口不返回系统prompt、隐藏思考、完整行情集或密钥；只监听本机。Anton 字体与OFL许可随站点本地提供，不使用参考站的分析脚本或外部资源。

### Flash：仅全实验1M token 上限

`configs/flash_october_unbounded.json` 使用百炼北京 `deepseek-v4.1-flash` 和全新的独立1M预算，策略为 `research_policy=total_tokens_only`。保留审核通过的黄金prompt，运行状态消息明确只有总预算。取消单轮输入/输出额度、600秒时限、提前强制提交、思考力度low覆盖、禁用并行工具调用覆盖、历史条数裁剪、5.5KB结果截短、工具查询窗口/条数上限及120秒抓取截止。注册工具可直接调用，router不再是额外执行门槛；工具参数类型、支持的数据集/品种、时间线及密钥/沙箱隔离仍有效。Docker的网络、文件系统和资源隔离保留，脚本不再设20秒研究时限或8KB输出截短。

每次供应商调用前，用序列化输入UTF-8保守上界加协议余量及输出偏差预留，动态计算本次最多可输出量；仅受剩余总预算及供应商393,216输出容量限制。已用输入（含缓存输入）、思考、输出和兼容性校准全部计入1M，预留在成功后按usage释放并核账。费用只记录，不以本地月度费用额度阻断本模式。HTTP调用不设本地读超时；供应商自身的服务/限流/容量限制仍可能导致失败。

预算不足以支付下一请求时停止，状态 `token_budget_exhausted`；不为尚未提交的当天生成假成交、失败保持决策或完整月末收益。不要求保证完成17个有效报价日，不为后续日期预先切分或预留研究额度。预检只验证行情、凭证及配置，不要求剩余额度足以跑完整月。

连接中断时，未知计费请求保留最坏token预留。恢复实验必须通过 `token_budget_parent` 显式共享原账本，不释放未知占用或重新分配总预算；包含本地账本ID的续跑配置不提交。新连接使用SSE流式返回和最终usage核账；请求发出前即归档参数，断流时保留预留，绝不按字符数猜测计费。
