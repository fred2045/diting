# scripts/ —— 独立运维脚本（与 Django 阅读器无关）

这里的脚本**独立运行**：直连 Miniflux / 只读 `.env`，不需要起 reader。
阅读器本体的离线自检脚本（`verify_*.py`）在 [`reader/scripts/`](../reader/scripts/)，不在这里。

| 脚本 | 用途 |
| ---- | ---- |
| `feed_health.py` | 订阅源健康检查（专抓「静默失效」） |
| `feed_stats.py` | 产量口径：过去 N 小时多少条、按源/分类排名 |
| `feed_profile.py` | 内容口径：一个源里是什么体裁/话题（分级/屏蔽决策用） |
| `mf_add_feed.py` | 加源到 Miniflux |

> 早期原型（LLM 分级 / 素材提取 / 快速刷新的命令行前身）与特定平台的
> 订阅构建脚本已随开源清理移出本仓库；它们的能力都由 `reader/core/` 或
> RSSHub 生态承接。
