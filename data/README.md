# 本地数据

真实行情、新闻、账号审计和导入数据放在 `local/` 中；该目录被Git忽略，不随仓库上传。

当前“已有Tushare十月审计”选项读取：

```text
data/local/october-coverage/audit-20261109-130254.jsonl
```

文件沿用原审计格式：每行一个接口结果对象，黄金条目为 `api=fx_daily`、`params.ts_code=XAUUSD.FXCM`，行情记录放在 `rows`。

没有此文件时返回空数据并在预检中报告缺失。新克隆仍可运行内置免费工程行情；也可从界面导入规范化JSON或使用自己的Tushare权限获取行情。
