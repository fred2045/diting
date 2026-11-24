# 标题翻译（中英对照）

> 返回 [README](../README.md)

英文源看标题费劲时，可以**按 feed** 打开标题翻译：

- 阅读页树上 feed 右键开启，**默认关闭**；开启时回填最近 7 天、最多 50 条；
- 后续新文章由后台自动翻译（默认每 5 分钟一轮增量扫描）；
- 译文**中文在前、英文在后**，正文不翻译；
- 只翻标题，不进分级 / 总结 / 搜索，也不改 Miniflux 原文；
- **独立记账、独立日预算**（默认 ¥0.3/天，`/costs` 可调），超限只停翻译，
  不影响分级 / 日报 / 周报 / 知识库。

## 可调参数（`.env` 的 `TRANSLATE_*` 段）

| 键 | 默认 | 说明 |
| -- | ---- | ---- |
| `TRANSLATE_ENABLED` | `1` | 总开关（没有 feed 开启 = 零调用） |
| `TRANSLATE_BUDGET_CNY` | `0.3` | 翻译池日预算（¥） |
| `TRANSLATE_LOOKBACK_DAYS` | `7` | 回填/新文章的发布时间窗口 |
| `TRANSLATE_BACKFILL_MAX` | `50` | 每 feed 开启时的回填上限 |
| `TRANSLATE_PER_TICK_MAX` | `20` | 每 feed 每 tick 增量上限 |
| `TRANSLATE_BATCH_SIZE` | `20` | 一次 LLM 调用翻多少条标题 |
| `TRANSLATE_BATCH_CHARS` | `4000` | 单批标题总字符上限（超了拆批） |
| `TRANSLATE_TICK_SEC` | `300` | 增量扫描间隔（秒） |
| `TRANSLATE_SCAN_MIN` | `5` | 同一 feed 两次扫描的最小间隔（分钟） |
| `TRANSLATE_SCAN_LIMIT` | `500` | 每 feed 每轮拉多少条条目 |
| `TRANSLATE_CHAR_LIMIT` | `400` | 超长标题跳过（不截半句） |
| `TRANSLATE_MAX_ATTEMPTS` | `3` | 单条自动重试上限 |
| `TRANSLATE_MAX_CALLS_PER_DAY` | `200` | 非 DeepSeek 无法估费时的调用次数兜底 |