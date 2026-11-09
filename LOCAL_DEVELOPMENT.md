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

- get_prices：已实现XAUUSD已解锁日线，500行上限及数据句柄。其他外汇/上金所/ETF日线白名单的自动适配仍需补充，不宣称已具备多品种实测。
- search_news/read_news：新浪、财联社、长篇新浪按时间窗获取/触限递归切分/去重缓存/正文清洗/关键词过滤/时间排序/证据留档；真实API需账号权限，测试驱动不会暗中联网。
- get_macro/get_positioning/read_webpage：读取显式准备的冻结快照，每条必须带含时区的available_at，按截止过滤。宏观/持仓用dataset匹配，网页用record_id；尚未自动推断PMI/GDP发布日或美债延迟。无准备数据明确返回dataset_not_prepared，不伪造数据。
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
