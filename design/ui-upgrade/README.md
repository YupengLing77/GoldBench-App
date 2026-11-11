# Home · final — design reference

This is a design mockup created in a visual design tool, exported as a
standalone page. Treat it as a REFERENCE MOCKUP, not production code:
the markup and inline styles carry the design's precise values — colors,
font sizes, spacing, radii, shadows, layout — which an implementation
should replicate faithfully in its own components and styling system
rather than copy wholesale.

## Contents

- `Home.dc.html` — the artboard (a Design Component: an `<x-dc>`
  template + a small logic class). The values to replicate live in its
  inline `style="…"` attributes and the `<helmet><style>` block.
- `support.js`, `vendor/react*.js` — the runtime that renders the
  component in a browser; not part of the design.

## Viewing

Serve the folder (e.g. `python3 -m http.server`) and open `Home.dc.html`;
some browsers block the scripts over file://.

## GoldBench UI upgrade branch

本目录保存 `Home · final-html.zip` 中的独立首页设计原型，供后续 UI 升级参考。

- `Home.dc.html`、`support.js` 和 `vendor/` 与原始压缩包保持一致。
- 页面中的行情、模型收益、工具调用及活动信息均为预设或随机生成的演示数据。
- 尚未连接 GoldBench 后端、模型服务或 Tushare 数据。
- Leaderboard、Agents、Replay 对应页面未包含在本次导出中；部分导航和按钮为占位。
- 字体从 Google Fonts 加载，离线时可能使用回退字体。

### 本地预览

在仓库根目录执行：

```bash
python3 -m http.server 8790 --bind 127.0.0.1 --directory design/ui-upgrade
```

打开 <http://127.0.0.1:8790/Home.dc.html>。

后续与现有前端的合并方式另行确定；本次提交仅增加设计参考与预览文件。
