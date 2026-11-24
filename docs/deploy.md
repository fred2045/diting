# 部署与运维

快速开始见 [README](../README.md#快速开始)（唯一一份，本页不重复安装步骤）。
功能介绍见 README「核心能力」与专题页：[分级](triage.md) · [知识库](wiki.md) ·
[标题翻译](translate.md) · [系统通知](notifications.md)。

本页只回答四类问题：**跑起来后怎么观察、出问题怎么排查、怎么手动补跑、改代码后怎么自证。**

## 1. 运行须知

- **Docker Desktop**：macOS / Windows 桌面端都用它跑采集层（Windows 走 WSL2 后端），
  安装前置交给你的 agent；国内拉镜像超时可在 Engine 设置里加加速器，不改仓库文件。
- **调度随 Web 进程启动**：分级 / 快速刷新 / 标题翻译 / 每日日报 / 每周周报都在
  `runserver` 进程里跑，不需要单独的 worker/队列进程。
- **改了 `.env`**：Django 只在启动时读一次，需重启。
- **改了 `.py`**：`--noreload` 下必须重启；改 `.html` 可热重载（settings 显式声明了
  模板 loaders 绕开缓存）。
- **对外访问**：默认只听 127.0.0.1，且 reader **没有任何登录鉴权**。要放开需设置
  `DJANGO_ALLOWED_HOSTS`，且 `DJANGO_DEBUG=0` 时必须同时设置 `DJANGO_SECRET_KEY`
  （否则拒绝启动——这是保护，不是 bug）。

## 2. 日志与运行状态

- 在线看：左侧栏「日志」或 <http://127.0.0.1:8099/logs>（级别/模块/关键字过滤，自动刷新）。
- 文件在 `reader/data/logs/`：`reader.log` 全量、`error.log` 只收 ERROR+。

## 3. 手动命令（排错 / 补跑）

```bash
# 分级：手动跑一轮（--dry-run 只统计）
python manage.py triage_once --max-calls 50

# 日报：重跑某天。默认走缓存，只有输入变了的层才真的调模型
python scripts/rerun_daily.py 2026-11-10            # 增量重跑
python scripts/rerun_daily.py 2026-11-10 --no-wait  # 不等 feed 新鲜度，立刻开跑
python scripts/rerun_daily.py 2026-11-10 --no-cache # 忽略缓存重跑（会花钱）

# 周报：过去 7 天日报的合集，不重采不重判——缺哪天的日报会直接说
python scripts/run_weekly.py                        # 本期
python scripts/run_weekly.py --week-end 2026-11-01  # 指定那期（= 那周的周日）
python scripts/run_weekly.py --dry                  # 只检查素材齐不齐，不调模型

# 重置本地数据：删掉 SQLite 即清空谛听自己的记录（Miniflux 正文/已读不受影响）
rm reader/data/reader.sqlite3*
```

`--no-wait` 用于「有个源一直拉不下来，但不该挡住重跑」的场合：默认会按
`SUMMARY_FRESH_DEADLINE_MIN` 轮询最多 30 分钟。缺**历史**某天日报时周报立即失败
（历史日报不会自己出现，得先补那份）；缺**当天**的会提示等待，guard 自动重试。

## 4. 目录

```
reader/
├── config/            settings / urls / wsgi
├── core/              领域层：数据源抽象、分级、prompt 构建、日报/周报编排、
│                     知识库提炼编译（ingest/wiki）、报告存取、树索引、预算闸
│   ├── datasource/    DataSource 抽象 + Miniflux 实现
│   └── tests/         单元测试（纯函数不变量）
├── apps/              阅读 / 分级 / 总结 / 翻译 / 知识库 / 分组 / 开销 / 设置 / 日志 视图
├── scripts/           verify_* 离线自检 + rerun_daily / run_weekly 等运维脚本
├── templates/         服务端渲染 + htmx（vendor 本地化，离线可用）
├── static/            app.css / app.js / 图标
└── data/              SQLite（WAL）与日志，运行时产物，不入库
```

## 5. 排错

- **页面空白 / 500**：看终端日志；多半是采集层没起来或 `.env` 凭据不对。
- **没配 LLM key**：系统能正常起——分级走零成本默认路径、翻译/总结调用会失败并
  在状态页如实标注，不静默。
- **curl 本机端口 502**：系统代理把 127.0.0.1 也送进代理了，加 `--noproxy '*'`。
- **模板注释**：Django 模板只吃单行 `{# #}`，跨行的会原样渲染到页面上；
  多行注释一律 `{% comment %}`。

## 6. 验证

### 6.1 离线自检（不需要服务在跑）

```bash
cd reader
python scripts/verify_ui_shell.py           # 界面外壳 / L 标记 / 级别多选 / 命中区
python scripts/verify_groups.py             # 虚拟分组装配层 + 零污染 + 事故回归防线
python scripts/verify_prompt_layering.py    # 分级 prompt 四层分层（自建假配置）
python scripts/verify_triage_state.py       # 分级状态链路 + 总结记账/分批
python scripts/verify_cite_format.py        # 总结引用格式（零 LLM）
python scripts/verify_summary_freshness.py  # 日报新鲜度口径与窗口
python scripts/verify_translate.py          # 标题翻译：判定/解析/预算/幂等
python scripts/verify_logging.py            # 日志系统
python scripts/verify_notify.py             # 系统通知 mac 后端（假 binary）
python scripts/verify_notify_win.py         # 系统通知 win 后端（假 winotify）
python scripts/verify_clicks.py             # 点击与深链契约
python scripts/verify_feed_edit.py          # 源编辑表单与校验
python scripts/verify_feed_health.py        # 订阅源健康检查的口径与解析
python scripts/verify_prefs.py              # 偏好设置读写
python scripts/verify_weekly.py             # 周报三源整合
python scripts/verify_wiki.py               # 知识库守卫全集
python scripts/verify_wiki_mutex_guard.py   # 写视图互斥守卫（AST 扫全集）
```

全部零成本：不调 LLM、不写真实数据（用假配置/假客户端，结束删净）。
**退出码 0 = 全绿**，适合作为改动后的回归面。
前置：先 `python manage.py migrate`（verify 脚本会读本地 SQLite；空库也能跑，
多数断言空通过）。

### 6.2 浏览器端回归

真浏览器 e2e 回归套件（无头 Chrome / CDP 直驱）**未随开源仓库发布**：它依赖特定隔离服务、
种子数据与系统 Chrome，只对项目内部的改动回归有用。发布版的回归面是单元测试
（`python manage.py test core`）+ 17 个离线自检（§6.1），改动后跑这两层即可自证没破契约。