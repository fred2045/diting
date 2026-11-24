#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diting · 订阅源健康检查（专门抓「静默失效」）
============================================

为什么需要它
------------
RSSHub 的很多路由是用 cheerio 选择器抠页面的（例：`/anthropic/news` 依赖
`[class^="PublicationList-module-scss-module__"][class$="__list"] a` 这种
**CSS-Module 自动生成的类名**）。站点重新构建后类名 hash 一变：

    RSSHub  → 选择器选不到 → `$()` 返回空数组 → **HTTP 200 + 空 feed**（源码里没有 length 校验）
    Miniflux → 拿到一份"合法但空"的 XML → **parsing_error_count 不 +1**、不禁用、不报错

也就是说 **RSSHub 不告警、Miniflux 也不知道**，源会一直"活着但永远没新内容"。
本脚本就是为了把这类失效变成可见的：

    - disabled / parsing_error_count > 0   → ERROR（Miniflux 已知的错误）
    - 最新条目时间超过预期（陈旧）          → WARN（静默失效的主要特征）
    - 正文长度骤降或过短                    → WARN（列表选择器还在、正文选择器挂了）
    - checked_at 过旧                       → WARN（调度停了 / 容器挂了）
    - --probe 直连 RSSHub 拿到 0 条         → ERROR（在 Miniflux 还没察觉前就发现）
                 ★ 探测会**重试 3 次**（间隔 4s）再定性：RSSHub 上游瞬时失败很常见
                   （如 youtube 路由的 `TypeError: fetch failed`），一次就报 ERROR 会天天误报。
                   重试仍失败时按错误性质分级：真失效（参数非法/路由空/选择器炸）→ ERROR，
                   纯瞬时故障（fetch failed / timeout）→ 降级 WARN，等下一轮自愈。

★ 调度层体检（collect_scheduling）—— 补上「延迟变成丢失」这条缝
    - next_check_at 已过期（到期了却没被刷新）→ 「积压」；全局超过半数源逾期且中位逾期
      > 2×POLLING_FREQUENCY 时判为**环境停摆**（Mac 合盖 / Docker 暂停）而不是源坏了
    - parsing_error_count ≥ POLLING_PARSING_ERROR_LIMIT → ★ 该源**被永久排除出调度**：
      Miniflux 的批次查询带 `parsing_error_count < N` 条件，命中后源再也不进批次，
      而 UI 上只表现为"积压"、三层监控全都不告警 —— 延迟直接变成永久停更。
      `POLLING_PARSING_ERROR_LIMIT=0` 时该条件不加，即**永不踢出**（本项目当前配置）。
      调度参数直接从 docker-compose.yml 读取，保证与运行实例一致，不靠人脑同步。

用法
----
    python3 scripts/feed_health.py                 # 全量体检，人读表格
    python3 scripts/feed_health.py --probe         # 额外直连 RSSHub 探测上游（慢一点，更准）
    python3 scripts/feed_health.py --json          # 机器读
    python3 scripts/feed_health.py --only 35,36    # 只看指定 feed

退出码：0 = 全部健康；1 = 有 ERROR；2 = 只有 WARN。
便于外部调度器（`*/60 * * * *` 或 APScheduler）据此发告警。

配置：**没有** per-feed 硬编码表（那等于把订阅列表固化进代码，且 id 会漂）。
      陈旧阈值按每个源自己的**发布间隔中位数 × 3** 推（`derive_expect`），
      确实要按源定制的写本地 `data/feed_expect.json`（键=feed_url，不入库）。
基线：data/feed_health.json（键=feed_url）。两个用途：① 与上次对比正文骤降；
      ② 记录本次推导出的阈值，便于事后复盘"当时为什么判它陈旧"。

凭证：从项目 .env 读 MINIFLUX_ADMIN_USERNAME / MINIFLUX_ADMIN_PASSWORD
      （兼容旧 MINIFLUX_USER / MINIFLUX_PASS）。
Miniflux 地址：--base 或 MINIFLUX_URL（默认 http://localhost:8080）。
RSSHub 探测地址：--rsshub 或 RSSHUB_PUBLIC_URL（默认 http://localhost:1200）。
"""

import argparse
import base64
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
BASELINE_PATH = os.path.join(ROOT, "data", "feed_health.json")

# ★ 正文长度检查只看"最近 CONTENT_WINDOW 条"，不能看全量：
#   - 很多源的历史条目是"标题流"（原生 feed 的 <description> 为空），全量中位数会被历史永久拉低；
#   - 窗口越大越迟钝：中位数要过半变坏才翻转，N 条就要坏 N/2+1 条才发现。
#   - 10 是折中：既有足够样本抗单条波动，又能在几天内翻脸。
#   ⚠ 调大这个值前先确认该源"窗口内"没有历史标题流残留，否则会长期误报 WARN。
CONTENT_WINDOW = 10

# ★ 本机服务必须显式绕开 macOS 系统代理（urllib 不读系统代理的例外列表，
#   开了全局代理工具时连 localhost 也会被代理 → 502）
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ATOM = "{http://www.w3.org/2005/Atom}"

# ---------------------------------------------------------------- 期望配置
# ★★ 这里**刻意没有** per-feed 期望表。三个原因，都是踩出来的：
#   1. `feed_id` 是各实例自己的自增主键 —— 写死等于把「某一个人的订阅列表」固化进
#      代码，开源即泄露；对别人还完全无用，他们的 id 对不上。
#   2. id 会漂移：删源重建 / 换路由重加都拿新 id，写死的阈值会**静默套到别的源上**。
#      本项目实测：一行指向某账号的阈值，后来管着一个完全无关的账号；另有三行指向
#      已不存在的源（等于死配置，看着"有配置"其实没生效）。
#   3. 硬编码「当前库状态」当期望值 = 用快照冒充机制，数据一变就是假告警/假通过。
#   ⇒ 能推的一律**从本实例真值推**（`derive_expect` + 基线对比）；
#     确实要按源定制的（长期归档的源、天生无正文的源），写进**本地**
#     `data/feed_expect.json`（被 .gitignore 的 `data/*` 覆盖，不入库），
#     键用 `feed_url`（稳定身份）而不是 id。
#
# max_age_days : 最新条目距今多少天算"陈旧"（默认由发布节奏推，见 CADENCE_*）
# min_content  : 最近 CONTENT_WINDOW 条正文长度**中位数**的绝对下限（0 = 不定绝对线）
# min_entries  : 该源至少该有多少条历史（0 = 不检查）
# allow_empty_content : 允许整源无正文（天生只有标题的源；默认 False = 要检查）
DEFAULTS = {"max_age_days": 30, "min_content": 0, "min_entries": 1,
            "allow_empty_content": False}

# ★ 陈旧阈值 = CADENCE_FACTOR × 本源发布间隔中位数，而不是全局一个 30 天：
#   小时级源静默 3 天就该报，季更博客静默 3 个月是常态。一个常数不可能同时成立。
#   下限防「分钟级源 1 小时没更新就告警」；上限给归档源留余地。
CADENCE_FACTOR = 3
CADENCE_MIN_DAYS = 7          # 下限：日更源停一周才算异常（3 天会把周末/长假算成故障）
CADENCE_CAP_DAYS = 3650
CADENCE_MIN_SAMPLES = 3        # 间隔样本少于此值 → 推不出节奏，退回 DEFAULTS

OVERRIDE_PATH = os.path.join(ROOT, "data", "feed_expect.json")


def load_overrides(path=None):
    """读本地 per-feed 覆盖（可选，不入库）。键 = `feed_url`，不是 `feed_id`。

    文件不存在 = 正常情况（新人 clone 下来就没有），返回 {}。
    """
    p = path or OVERRIDE_PATH
    if not os.path.exists(p):
        return {}
    try:
        raw = json.load(open(p, encoding="utf-8"))
    except Exception as e:                      # 坏 JSON 不该让体检整个失败
        print(f"[warn] {p} 读取失败，按「无覆盖」继续：{e}", file=sys.stderr)
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("feeds"), dict):
        raw = raw["feeds"]                       # 允许 {"feeds": {...}} 包一层
    return raw if isinstance(raw, dict) else {}


def cadence_days(published):
    """这条源「典型的最长静默」有多久（天）；样本不足返回 None。

    ★ 用 **p75** 而不是中位数：中位数会被"同一天连发几条"带偏 —— 突发式更新的源
      （NVIDIA / Sequoia / 微软研究院）中位间隔趋近 0，阈值被压到 3 天下限，
      于是"周末没发"就报警。p75 说的是"四分之三的间隔都比它短"，即这条源自己
      常态下能安静多久，突发不影响它。
    ★ 用 published_at（内容自己声明的时间），不用入库时间：后者会被补跑 / 换源 /
      crawler 回填污染，算出的节奏是假的。
    """
    ds = [d for d in published if d]
    gaps = [(ds[i] - ds[i + 1]).total_seconds() / 86400 for i in range(len(ds) - 1)]
    gaps = [g for g in gaps if g > 0]            # 同一秒发两条（转发/置顶）不算间隔
    if len(gaps) >= 4:
        return statistics.quantiles(gaps, n=4)[2]
    if len(gaps) >= CADENCE_MIN_SAMPLES:
        return statistics.median(gaps)
    return None


def derive_expect(entries, override=None):
    """该源的期望值：**先按它自己的节奏推**，再用本地覆盖打补丁。"""
    exp = dict(DEFAULTS)
    gap = cadence_days([parse_dt(e.get("published_at"))
                        for e in entries.get("entries", [])])
    if gap:
        days = round(gap * CADENCE_FACTOR)
        exp["max_age_days"] = int(min(max(days, CADENCE_MIN_DAYS), CADENCE_CAP_DAYS))
    if override:
        exp.update({k: v for k, v in override.items() if k in DEFAULTS})
    return exp

# 轮询间隔（分钟）。checked_at 的判定是"全局中位数"，不是逐源 —— 因为
# Mac 合盖睡眠 / Docker 暂停会让**所有源**一起变旧，那是环境问题不是源的问题。
# ★ 调度参数一律以 docker-compose.yml 为准（见 sched_config()），这里只是兜底默认值。
POLL_INTERVAL_MIN = 60


def load_env_file():
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_get(url, auth, timeout=30):
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with NO_PROXY_OPENER.open(req, timeout=timeout) as r:
        return json.load(r)


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def median(xs):
    xs = sorted(xs)
    if not xs:
        return 0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) // 2


def fmt_local(dt):
    """把 aware datetime 显示为本地时间（便于人读；对照 UI 用）。"""
    if not dt:
        return "-"
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


# ---------------------------------------------------------------- 「最后一次成功拉取」
# ★ 关键事实（已核实 Miniflux feeds 表结构，非推断）：
#   feeds 表**根本没有** "last success" 字段。有的只是：
#     checked_at          最后一次「尝试」的时刻 —— **成功、失败都会写**（失败也写 now()）
#     next_check_at       下次计划检查时刻 = 本次完成时刻 + interval（成败都写）
#     parsing_error_count **连续**失败次数（成功即清零，实测 2→3 递增 / 清零见源码 SQL
#                         `UPDATE feeds SET parsing_error_count=0, parsing_error_msg=''`）
#     parsing_error_msg   最近一次失败的原因
#   ⇒ UI 上 feed 详情页的 "Last check: 30 minutes ago" 就是 checked_at，
#     它**只说明"30 分钟前尝试过"，不代表那次成功了**。
#   ⇒ 想知道"最后一次成功"，只能这样推：
#       err_cnt == 0  → 最后一次尝试就是成功的 → checked_at **精确**是最后成功时间
#       err_cnt == N  → 最近 N 次连续失败；用**本源实测间隔**（next_check_at − checked_at，
#                       比用 compose 里的全局值更准）回推 N 次 → 最后成功的**估算值**
def last_success(feed):
    """推导该 feed 的最后一次成功拉取时间。返回 dict 或 None。"""
    checked = parse_dt(feed.get("checked_at"))
    if not checked:
        return None
    errs = feed.get("parsing_error_count") or 0
    nxt = parse_dt(feed.get("next_check_at"))
    interval_min = None
    if nxt and nxt > checked:
        interval_min = round((nxt - checked).total_seconds() / 60)
    if errs == 0:
        return {"kind": "exact", "dt": checked, "interval_min": interval_min, "err_count": 0}
    if not interval_min:
        return {"kind": "unknown", "dt": None, "interval_min": None, "err_count": errs}
    return {
        "kind": "estimate",
        "dt": checked - timedelta(minutes=interval_min * errs),
        "interval_min": interval_min,
        "err_count": errs,
    }


def last_success_text(feed):
    """给报告的「最后一次成功 ≈ ...」一行文案。"""
    ls = last_success(feed)
    if not ls:
        return "最后成功时间：未知（无 checked_at）"
    if ls["kind"] == "exact":
        return f"最后成功：{fmt_local(ls['dt'])}（err_cnt=0 → 上次检查即成功，精确值=checked_at）"
    if ls["kind"] == "unknown":
        return (f"最后成功：未知 —— 已连续失败 {ls['err_count']} 次，"
                f"Miniflux 不存该字段，且本源间隔不可得，无法回推")
    return (f"最后成功 ≈ {fmt_local(ls['dt'])}"
            f"（Miniflux 无此字段：连续失败 {ls['err_count']} 次 × 本源实测间隔 {ls['interval_min']}min 回推；"
            f"最近一次尝试 {fmt_local(parse_dt(feed.get('checked_at')))}）")


# ---------------------------------------------------------------- RSSHub 直连探测
# ★ 为什么要重试：像 `/youtube/channel/:id` 这类路由要打 youtubei / Playwright，
#   上游会**瞬时**失败（实测 `TypeError: fetch failed`，10 秒后抛，HTTP 503）。
#   探测一次失败就报 ERROR 会变成"狼来了"，所以先重试若干次；仍失败再按**错误性质**分级：
#     真失效（参数非法 / 路由空 / 选择器炸）      → ERROR（要人处理）
#     瞬时故障（fetch failed / timeout / 无文案的 5xx）→ WARN（下一轮大概率自愈）
PROBE_ATTEMPTS = 3
PROBE_RETRY_WAIT = 4          # 秒

# 命中即判为"真故障"：RSSHub 会把上游异常的类名/消息写进 503 页面。
# ⚠ 不要用过于宽泛的词（比如单独的 "empty"），错误页里含 CSS/HTML，容易误命中。
FATAL_PATTERNS = (
    "InvalidParameterError", "Invalid ",
    "route is empty", "NotFound", "does not exist",
    "Cannot read propert", "Cannot read undefined", "is not a function",
)


def probe_rsshub(feed_url, rsshub_base, timeout=60):
    """直接问 RSSHub 上游：还能解析出条目吗？

    返回 dict(ok, items, with_content, avg_len, error, severity, attempts)。
    items == 0 即"选择器已经失效"的最强信号 —— 此时 Miniflux 侧往往还一切正常。
    """
    if "rsshub" not in feed_url:
        return None
    path = feed_url.split("rsshub", 1)[1]
    path = path.split("/", 1)[1] if "/" in path else ""
    url = f"{rsshub_base.rstrip('/')}/{path}"
    out = {"ok": False, "items": 0, "with_content": 0, "avg_len": 0,
           "error": "", "url": url, "severity": "ERROR", "attempts": 0}

    for attempt in range(1, PROBE_ATTEMPTS + 1):
        out["attempts"] = attempt
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "diting-health/1.0"})
            with NO_PROXY_OPENER.open(req, timeout=timeout) as r:
                raw = r.read()
            root = ET.fromstring(raw)
            items = root.findall(".//item") or root.findall(f".//{ATOM}entry")
            out["ok"] = True
            out["severity"] = "OK"
            out["items"] = len(items)
            lens = []
            for it in items:
                body = ""
                for tag in ("description", "content", f"{ATOM}content", f"{ATOM}summary"):
                    el = it.find(tag)
                    if el is not None and el.text:
                        body = el.text
                        break
                if len(body) > 50:
                    out["with_content"] += 1
                lens.append(len(body))
            out["avg_len"] = int(sum(lens) / len(lens)) if lens else 0
            return out
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:4000].decode("utf-8", "replace")
            except Exception:
                pass
            out["error"] = f"HTTP {e.code}"
            # 客户端错误码（路由不存在/参数非法）或命中致命文案 → 定性为真故障，不再重试
            if e.code in (400, 401, 403, 404) or any(p in body for p in FATAL_PATTERNS):
                out["severity"] = "ERROR"
                detail = " ".join(body.split())[:150]
                out["error"] = f"HTTP {e.code}：{detail}" if detail else f"HTTP {e.code}"
                return out
        except Exception as e:
            out["error"] = type(e).__name__ + ": " + str(e)[:120]
        if attempt < PROBE_ATTEMPTS:
            time.sleep(PROBE_RETRY_WAIT)

    # 重试到底仍失败 → 按瞬时故障处理，降级 WARN，避免天天"狼来了"
    out["severity"] = "WARN"
    out["error"] = f"{out['error']}（重试 {out['attempts']} 次仍失败 → 疑似瞬时抖动，非选择器失效）"
    return out


# ---------------------------------------------------------------- 调度层体检
COMPOSE_PATH = os.path.join(ROOT, "docker-compose.yml")

# Miniflux 的代码默认值（源码 internal/config/options.go）。
# 注意 POLLING_LIMIT_PER_HOST 的代码默认是 0 = 不限制；POLLING_PARSING_ERROR_LIMIT 默认 3。
SCHED_DEFAULTS = {
    "POLLING_FREQUENCY": 60,                    # 调度器多久开一次闸（分钟）
    "SCHEDULER_ROUND_ROBIN_MIN_INTERVAL": 60,   # 单源基准间隔下限（分钟）
    "POLLING_LIMIT_PER_HOST": 0,                # 每轮同一 host 最多放行几个
    "POLLING_PARSING_ERROR_LIMIT": 3,           # 连续失败几次后踢出调度（0 = 永不踢出）
}


def sched_config():
    """从 docker-compose.yml 读调度参数，避免脚本与运行实例各说各话。"""
    cfg = dict(SCHED_DEFAULTS)
    try:
        for line in open(COMPOSE_PATH, encoding="utf-8"):
            line = line.strip()
            if not line.startswith("- ") or "=" not in line:
                continue
            k, v = line[2:].split("=", 1)
            k = k.strip()
            if k in cfg:
                try:
                    cfg[k] = int(v.strip())
                except ValueError:
                    pass
    except Exception:
        pass
    return cfg


def collect_scheduling(feeds, now=None, cfg=None, max_detail=8):
    """调度层体检：积压 / 被踢出调度 / 环境停摆。

    返回 (lines, info)。lines 为 Markdown 列表行，可直接拼进体检报告或每日汇总素材。
    """
    now = now or datetime.now(timezone.utc)
    cfg = cfg or sched_config()
    freq = cfg["POLLING_FREQUENCY"] or 60
    limit = cfg["POLLING_PARSING_ERROR_LIMIT"]
    total = len(feeds)
    lines = []

    # --- 1) 是否被永久踢出调度 ----------------------------------------
    kicked = []
    if limit and limit > 0:
        kicked = [f for f in feeds if (f.get("parsing_error_count") or 0) >= limit]
    for f in kicked:
        lines.append(
            f"- ★ #{f['id']} {f.get('title','')[:30]}：失败 {f['parsing_error_count']} 次"
            f" ≥ 上限 {limit} → **已被永久排除出调度**（UI 上只显示'积压'，实际永不更新）"
        )
    if not limit:
        # limit=0 时不踢出，但要把「若用默认阈值早就被踢出」的风险显式说出来，
        # 否则"没被踢出"会被误读成"这些源没问题"。
        default_lim = SCHED_DEFAULTS["POLLING_PARSING_ERROR_LIMIT"]
        near = [f for f in feeds if (f.get("parsing_error_count") or 0) >= default_lim]
        extra = ""
        if near:
            ids = "、".join(f"#{f['id']} {f.get('title','')[:16]}" for f in near)
            extra = (f"。⚠ 其中 {ids} 的失败次数已达默认阈值 {default_lim}，"
                     f"**现在没被踢出全靠这项设为 0**")
        lines.append(
            "- 调度容错：`POLLING_PARSING_ERROR_LIMIT=0` → 源**永不**因连续失败被踢出调度；"
            "失败仍在「解析错误」里逐条列出，是否废弃由人判断（不再静默变成永久停更）" + extra
        )

    # --- 1b) 解析错误源：附「最后一次成功」推导 -------------------------
    # ★ UI 上 feed 详情的 "Last check: 30 min ago" = checked_at，成功失败都刷新，
    #   所以它**不能**回答"上次成功是什么时候"。Miniflux 也没有 last-success 字段。
    #   这里统一推出「最后成功」并显式标注是精确值还是估算值。
    err_feeds = [f for f in feeds if (f.get("parsing_error_count") or 0) > 0]
    if err_feeds:
        err_feeds.sort(key=lambda x: -(x.get("parsing_error_count") or 0))
        lines.append(f"- 解析错误 {len(err_feeds)} 个源 —— ⚠ UI 的 “Last check” "
                     f"只表示「尝试过」，**不代表成功**；「最后成功」如下（Miniflux 本身不存该字段）：")
        for f in err_feeds[:max_detail]:
            lines.append(f"    · #{f['id']} {f.get('title','')[:24]}：{last_success_text(f)}")
        if len(err_feeds) > max_detail:
            lines.append(f"    · …另有 {len(err_feeds) - max_detail} 个源有解析错误")

    # --- 2) 积压：next_check_at 已过期却没被刷新 -----------------------
    overdue = []
    for f in feeds:
        nxt = parse_dt(f.get("next_check_at"))
        if not nxt:
            continue
        mins = (now - nxt).total_seconds() / 60
        if mins > 0:
            overdue.append((mins, f))
    overdue.sort(key=lambda x: -x[0])
    max_ov = int(overdue[0][0]) if overdue else 0

    if overdue:
        ratio = len(overdue) / max(total, 1)
        if ratio > 0.5 and max_ov > freq * 2:
            lines.append(
                f"- ★ 调度积压（**全局**）：{len(overdue)}/{total} 个源到期未刷新，"
                f"最长逾期 {max_ov/60:.1f} 小时 → 先怀疑容器/宿主停摆（Mac 合盖会让 Docker 暂停）"
                f"或 host 限额过小，而不是单个源失效"
            )
        else:
            lines.append(
                f"- 调度积压：{len(overdue)}/{total} 个源已过 next_check_at，最长逾期 "
                f"{max_ov/60:.1f} 小时（健康值应 < {freq} 分钟；明显超出说明 "
                f"POLLING_LIMIT_PER_HOST 或 WORKER_POOL_SIZE 不够）"
            )
        for mins, f in overdue[:max_detail]:
            lines.append(f"    · #{f['id']} {f.get('title','')[:30]}：逾期 {mins/60:.1f} h")
        if len(overdue) > max_detail:
            lines.append(f"    · …另有 {len(overdue) - max_detail} 个源逾期")

    info = {
        "total": total,
        "overdue_count": len(overdue),
        "max_overdue_min": max_ov,
        "kicked_count": len(kicked),
        "error_feed_count": len(err_feeds),
        "polling_frequency_min": freq,
        "parsing_error_limit": limit,
        "limit_per_host": cfg.get("POLLING_LIMIT_PER_HOST"),
        "min_interval_min": cfg.get("SCHEDULER_ROUND_ROBIN_MIN_INTERVAL"),
    }
    return lines, info


def check_feed(feed, entries, expect, rsshub_base, do_probe, prev=None):
    """返回 (level, problems, stats)。level ∈ OK / WARN / ERROR

    `prev` = 该源上一次的基线（按 feed_url 取），用于「正文较上次骤降」。
    """
    fid = feed["id"]
    problems = []
    now = datetime.now(timezone.utc)

    total = entries.get("total", 0)
    ents = entries.get("entries", [])
    newest = parse_dt(ents[0]["published_at"]) if ents else None
    age = (now - newest).days if newest else None
    clens = [len(e.get("content") or "") for e in ents]
    cmed = median(clens) if clens else 0
    # ents 已按 published_at 倒序 → ents[0] 是最新一条
    clast = clens[0] if clens else 0

    checked = parse_dt(feed.get("checked_at"))
    checked_gap = int((now - checked).total_seconds() // 60) if checked else None

    # --- ERROR 级 -------------------------------------------------------
    if feed.get("disabled"):
        problems.append(("ERROR", "feed 已被 Miniflux 自动禁用（连续解析失败）"))
    if (feed.get("parsing_error_count") or 0) > 0:
        msg = (feed.get("parsing_error_message") or "")[:80]
        # ★ "Last check: 30 minutes ago" 只说明「30 分钟前尝试过」，不代表成功。
        #   这里直接把「最后一次成功」推出来附在后面，避免把它误读成成功时间。
        problems.append(("ERROR", f"解析错误 ×{feed['parsing_error_count']}：{msg}｜{last_success_text(feed)}"))

    probe = None
    if do_probe:
        probe = probe_rsshub(feed.get("feed_url", ""), rsshub_base)
        if probe and not probe["ok"]:
            problems.append((probe.get("severity", "ERROR"),
                             f"RSSHub 探测失败：{probe['error']}"))
        elif probe and probe["items"] == 0:
            problems.append(("ERROR", "RSSHub 返回 0 条 → 上游选择器大概率已失效"))
        elif probe and probe["with_content"] == 0 and probe["items"] > 0:
            problems.append(("WARN", f"RSSHub {probe['items']} 条但全部无正文 → 正文选择器失效"))

    # --- WARN 级 --------------------------------------------------------
    if total < expect.get("min_entries", 0):
        problems.append(("WARN", f"历史条目数 {total} < 期望 {expect['min_entries']}"))
    if age is None:
        problems.append(("WARN", "一条条目都没有（从未入库）"))
    elif age > expect["max_age_days"]:
        problems.append(("WARN", f"最新条目已 {age} 天（阈值 {expect['max_age_days']} 天）"))
    # --- 正文层 ---------------------------------------------------------
    # ★ 三条判据都**不依赖 per-feed 阈值**，各自用自己那批数据当参照系：
    #   ① 整源无正文：足够多的历史条目里正文一条都没有 → 与"天生只有标题的源"的区别
    #      在于后者条目少且稳定；这里要求 ≥CONTENT_WINDOW 条才算证据。
    #      想放行某个天生无正文的源，在本地覆盖里写 "allow_empty_content": true。
    if total >= CONTENT_WINDOW and cmed == 0 and not expect.get("allow_empty_content"):
        problems.append(("WARN", f"整源无正文：最近 {len(clens)} 条正文全为 0"
                                 f"（≥{CONTENT_WINDOW} 条仍全空 → 疑似正文选择器失效 / crawler 未开）"))
    #   ② 较上次骤降：基线就是"这个源上一次的真值"，会自己长，所以"本来就短"的源
    #      永远不会被误判（基线也短），而刚坏掉的源第一天就掉下来。
    if prev and (prev.get("content_med") or 0) >= 200 and cmed < prev["content_med"] * 0.5:
        problems.append(("WARN", f"正文较上次骤降：中位数 {prev['content_med']} → {cmed}"
                                 f"（跌破基线 50% → 疑似选择器/crawler 刚坏）"))
    #   ③ 哨兵：**连最新两条**都远短于窗口内中位数 → 疑似选择器刚坏。
    #      要求"两条"是因为单条短很常见（推文、链接帖、图集），一个源偶尔发条短的
    #      不该报；而真坏掉之后新进来的每一条都短，所以晚一轮也跑不掉。
    if cmed >= 200 and len(clens) >= 2 and max(clens[:2]) < cmed * 0.3:
        problems.append(("WARN", f"最新两条正文仅 {clens[0]}/{clens[1]} 字（窗口内中位数 {cmed} 正常）"
                                 f" → 疑似 crawler/选择器刚刚失效"))
    #   ④ 绝对下限只在本地覆盖里显式写了 min_content 时才生效（给"卡死一条线"的场合）。
    if expect.get("min_content", 0) > 0 and cmed < expect["min_content"]:
        problems.append(("WARN", f"正文长度中位数 {cmed} < 本地设定下限 {expect['min_content']}"
                                 f"（最近 {len(clens)} 条）"))
    # 单个源被调度漏掉：只有在"全局调度正常"的前提下才可能是这条源的问题
    if checked_gap is not None and checked_gap > POLL_INTERVAL_MIN * 48:
        problems.append(("WARN", f"距上次轮询 {checked_gap} 分钟（{checked_gap // 1440} 天）→ 该源被调度漏下"))

    level = "OK"
    for lv, _ in problems:
        if lv == "ERROR":
            level = "ERROR"
        elif level != "ERROR":
            level = "WARN"

    ls = last_success(feed)
    if ls and ls["kind"] == "exact":
        ls_txt = "✓ " + fmt_local(ls["dt"])
    elif ls and ls["kind"] == "estimate":
        ls_txt = "≈ " + fmt_local(ls["dt"]) + f" (×{ls['err_count']})"
    else:
        ls_txt = "-"

    stats = {
        "id": fid,
        "url": feed.get("feed_url", ""),      # ★ 基线用它当键：id 会漂，url 不会
        "title": feed.get("title", "")[:28],
        "category": (feed.get("category") or {}).get("title", "-"),
        "total": total,
        "newest": newest.strftime("%Y-%m-%d") if newest else "-",
        "age_days": age if age is not None else -1,
        "content_med": cmed,
        "content_last": clast,
        "max_age_days": expect.get("max_age_days"),   # 本次用的阈值（按节奏推出来的）
        "checked_gap_min": checked_gap if checked_gap is not None else -1,
        "last_success": ls_txt,
        "last_success_dt": ls["dt"].isoformat() if (ls and ls["dt"]) else None,
        "last_success_kind": ls["kind"] if ls else None,
        "probe_items": probe["items"] if probe else None,
        "probe_avg_len": probe["avg_len"] if probe else None,
    }
    return level, problems, stats


def main():
    ap = argparse.ArgumentParser(description="diting 订阅源健康检查（抓静默失效）")
    ap.add_argument("--base", default=None, help="Miniflux 地址，默认 MINIFLUX_URL 或 http://localhost:8080")
    ap.add_argument("--rsshub", default=None, help="RSSHub 探测地址，默认 RSSHUB_PUBLIC_URL 或 http://localhost:1200")
    ap.add_argument("--probe", action="store_true", help="直连 RSSHub 探测上游（默认关闭，慢）")
    ap.add_argument("--only", default="", help="只检查指定 feed id，逗号分隔")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-save", action="store_true", help="不写基线文件（仍会读基线做对比）")
    ap.add_argument("--expect", default=None,
                    help=f"本地 per-feed 覆盖文件（键=feed_url），默认 {os.path.relpath(OVERRIDE_PATH, ROOT)}")
    args = ap.parse_args()

    load_env_file()
    base = (args.base or os.environ.get("MINIFLUX_URL", "http://localhost:8080")).rstrip("/")
    rsshub_base = (args.rsshub or os.environ.get("RSSHUB_PUBLIC_URL", "http://localhost:1200")).rstrip("/")
    user = os.environ.get("MINIFLUX_ADMIN_USERNAME") or os.environ.get("MINIFLUX_USER", "")
    pwd = os.environ.get("MINIFLUX_ADMIN_PASSWORD") or os.environ.get("MINIFLUX_PASS", "")
    if not (user and pwd):
        sys.exit("缺少 Miniflux 管理员凭证（.env 里的 MINIFLUX_ADMIN_USERNAME/PASSWORD）")
    auth = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None

    feeds = api_get(f"{base}/v1/feeds", auth)
    if only:
        feeds = [f for f in feeds if f["id"] in only]
    feeds.sort(key=lambda f: f["id"])

    overrides = load_overrides(args.expect)

    # ★ 基线要**先读**：它既是"上次的真值"（用于对照组文骤降），也是本次的写回目标。
    #   --no-save 只影响写，不影响读 —— 否则加了这个开关就等于关掉了骤降检测。
    try:
        baseline = json.load(open(BASELINE_PATH, encoding="utf-8"))
    except Exception:
        baseline = {}

    rows = []
    for f in feeds:
        fid = f["id"]
        url = f.get("feed_url", "")
        try:
            ents = api_get(
                f"{base}/v1/entries?feed_id={fid}&limit={CONTENT_WINDOW}&order=published_at&direction=desc", auth
            )
        except Exception as e:
            rows.append({"id": fid, "title": f.get("title", "")[:28], "level": "ERROR",
                         "problems": [("ERROR", f"条目查询失败：{e}")], "stats": {}})
            continue
        expect = derive_expect(ents, overrides.get(url))
        level, problems, stats = check_feed(f, ents, expect, rsshub_base, args.probe,
                                            prev=baseline.get(url))
        rows.append({"id": fid, "level": level, "problems": problems, "stats": stats})

    if not args.no_save:
        os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        # ★ 键 = feed_url：id 会因删源重建/换路由漂移，用 id 当键会让基线与源错位。
        #   --only 时不能整份重写（否则把没检查的源的基线清空），保留其余。
        out = dict(baseline) if only else {}
        for r in rows:
            s = r.get("stats") or {}
            key = s.get("url")
            if not key:
                continue
            out[key] = {
                "title": s.get("title"), "total": s.get("total"),
                "newest": s.get("newest"), "content_med": s.get("content_med"),
                "content_last": s.get("content_last"), "max_age_days": s.get("max_age_days"),
                "checked_at": now_iso,
            }
        json.dump(out, open(BASELINE_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    # --- 全局调度滞后：所有源一起变旧 = 环境问题（Mac 睡眠 / Docker 暂停），
    #     不是某个源坏了。单独提示，避免被误读成 30 个源同时失效。
    global_warn = None
    gaps = [r["stats"].get("checked_gap_min", -1) for r in rows if r.get("stats")]
    gaps = [g for g in gaps if g >= 0]
    if gaps:
        med = median(gaps)
        if med > POLL_INTERVAL_MIN * 6:
            global_warn = (
                f"轮询整体滞后：{len(gaps)} 个源距上次检查的中位数 = {med} 分钟"
                f"（{med / 60:.1f} 小时，阈值 {POLL_INTERVAL_MIN * 6}）"
                f" → 常见于 Mac 合盖睡眠 / Docker 暂停，不是单个源失效"
            )

    # --- 调度层体检：积压 / 被踢出调度。--only 时跳过（样本不全，会误导）---
    sched_lines, sched_info = ([], {})
    if not only:
        sched_lines, sched_info = collect_scheduling(feeds)

    if args.json:
        print(json.dumps({"rows": rows, "global_warn": global_warn,
                          "scheduling": {"info": sched_info, "lines": sched_lines}},
                         ensure_ascii=False, indent=1))
    else:
        print(f"{'ID':>3} {'等级':<6} {'分类':<6} {'条数':>5} {'最新':<11} {'天':>4} {'阈值':>5} {'正文中位/最新':>16} {'上游':>5} {'最后成功':<22} {'标题'}")
        print("-" * 136)
        for r in rows:
            s = r.get("stats") or {}
            mark = {"OK": " 正常", "WARN": "⚠ 警告", "ERROR": "✗ 错误"}[r["level"]]
            pi = s.get("probe_items")
            print(f"{r['id']:>3} {mark:<6} {s.get('category','-'):<6} {s.get('total',0):>5} "
                  f"{s.get('newest','-'):<11} {s.get('age_days',-1):>4} "
                  f"{str(s.get('max_age_days','-')):>5} "
                  f"{str(s.get('content_med',0))+'/'+str(s.get('content_last',0)):>16} "
                  f"{(str(pi) if pi is not None else '-'):>5} {s.get('last_success','-'):<22} {s.get('title','')}")
        print("（阈值 = 该源「发布间隔中位数 × 3」推出来的陈旧线，随源自己的节奏变；"
              "天 > 阈值 才报陈旧）")
        print("（上游 = --probe 时直连 RSSHub 解析到的条目数；0 即选择器已失效）")
        print("（最后成功：✓ = 精确（err_cnt=0，上次检查即成功，等于 checked_at）；"
              "≈ = 估算（Miniflux 不存该字段，按「连续失败次数 × 本源实测间隔」回推，括号内为失败次数））")
        bad = [r for r in rows if r["level"] != "OK"]
        if global_warn:
            print("\n[环境] " + global_warn)
        if sched_lines:
            print(f"\n[调度] {sched_info['total']} 源 · 逾期 {sched_info['overdue_count']} 个 · "
                  f"被踢出 {sched_info['kicked_count']} 个 "
                  f"（FREQUENCY={sched_info['polling_frequency_min']}min / MIN_INTERVAL={sched_info['min_interval_min']}min / "
                  f"PER_HOST={sched_info['limit_per_host']} / ERROR_LIMIT={sched_info['parsing_error_limit']}）")
            for ln in sched_lines:
                print("  " + ln)
        if bad:
            print("\n问题明细：")
            for r in bad:
                for lv, msg in r["problems"]:
                    print(f"  [{lv}] #{r['id']} {r.get('stats',{}).get('title','')} — {msg}")
        else:
            print("\n各源正常。")
        print(f"\n基线已写入 {os.path.relpath(BASELINE_PATH, ROOT)}"
              if not args.no_save else "")

    if any(r["level"] == "ERROR" for r in rows):
        sys.exit(1)
    if any(r["level"] == "WARN" for r in rows):
        sys.exit(2)


if __name__ == "__main__":
    main()
