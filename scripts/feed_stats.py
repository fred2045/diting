#!/usr/bin/env python3
"""Miniflux 订阅产出统计 —— 按【文章发布时间 pubDate】而非【抓取入库时间】。

口径说明（关键，别搞错）：
    published_at  = feed 里 <pubDate>/<published> → Miniflux 的 published_at  ← 本脚本用它
    created_at    = Miniflux 拉取到该条目的时间（入库时间）

两者在 Mac 睡眠后补拉时会明显不同：库里可能有一批「老 pubDate + 新 created_at」的条目。

用法：
    python3 scripts/feed_stats.py                     # 过去 24h，终端表格
    python3 scripts/feed_stats.py --hours 6
    python3 scripts/feed_stats.py --by-hour           # 附每小时分布（本地时区）
    python3 scripts/feed_stats.py --md data/feed-stats.md --json data/feed-stats.json
    python3 scripts/feed_stats.py --no-verify         # 跳过 API 口径交叉校验

退出码：0 正常 / 2 接口失败
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime

DEFAULT_BASE = "http://localhost:8080"
LIMIT_MAX = 1000  # Miniflux 上限，>1000 返回 400


# ---------------------------------------------------------------- 基础设施

def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def make_client(base: str):
    """本地服务必须绕开 macOS 系统代理（代理工具会把 localhost 请求送上游 → 502）。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    u = os.environ["MINIFLUX_ADMIN_USERNAME"]
    p = os.environ["MINIFLUX_ADMIN_PASSWORD"]
    auth = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()

    def get(path: str):
        req = urllib.request.Request(base + path, headers={"Authorization": auth})
        with op.open(req, timeout=30) as r:
            return json.load(r)

    return get


def parse_ts(s):
    """Miniflux 返回 RFC3339（UTC，带 Z 或带偏移）。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fmt_local(ts, tz) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")


def paged_entries(get, extra: str = "") -> list:
    """分页拉取条目（limit 上限 1000）。"""
    out, offset = [], 0
    while True:
        d = get(f"/v1/entries?limit={LIMIT_MAX}&offset={offset}"
                f"&order=published_at&direction=desc{extra}")
        items = d.get("entries", [])
        out.extend(items)
        total = d.get("total")
        if len(items) < LIMIT_MAX or (isinstance(total, int) and len(out) >= total):
            break
        offset += LIMIT_MAX
    return out


# ---------------------------------------------------------------- 统计

def build_report(get, hours: int, verify: bool, by_hour: bool, tz):
    now = int(time.time())
    t0 = now - hours * 3600

    feeds = {f["id"]: f for f in get("/v1/feeds")}
    cats = {c["id"]: c["title"] for c in get("/v1/categories")}

    # 全库拉一次：既做本地过滤（主口径），又给 0 条源补「库里最新发布时间」
    all_e = paged_entries(get)
    win = [e for e in all_e if (parse_ts(e.get("published_at")) or -1) > t0]

    # API 口径交叉校验（published_after 过滤）
    verified = None
    if verify:
        api = paged_entries(get, f"&published_after={t0}")
        verified = {"api": len(api), "local": len(win), "library": len(all_e)}

    # 入库时间（created_at）口径：必须对【全库】算，才能看出补拉进来的旧文章
    created_recent = sum(1 for e in all_e if (parse_ts(e.get("created_at")) or -1) > t0)
    both = sum(1 for e in win if (parse_ts(e.get("created_at")) or -1) > t0)
    # 入库在窗口内、但发布时间早于窗口 → 补拉进来的历史文章（新源首导 / 停更后恢复）
    bf_by_feed = defaultdict(int)
    for e in all_e:
        c = parse_ts(e.get("created_at")) or -1
        p = parse_ts(e.get("published_at")) or -1
        if c > t0 and p <= t0:
            bf_by_feed[e["feed_id"]] += 1
    backfilled = sum(bf_by_feed.values())
    # 发布时间在窗口内、但入库也在窗口之外更早？→ feed 给了未来时间戳等异常
    ahead = len(win) - both
    # 24h 内入库的时间分布（看是否有一段完全空档 = 宿主机停摆）
    created_hourly = defaultdict(int)
    for e in all_e:
        c = parse_ts(e.get("created_at"))
        if c and c > t0:
            created_hourly[datetime.fromtimestamp(c, tz).strftime("%m-%d %H:00")] += 1

    per_feed = defaultdict(list)
    for e in win:
        per_feed[e["feed_id"]].append(e)

    lib_last = {}   # feed_id -> 全库最新 pubDate
    lib_cnt = defaultdict(int)
    for e in all_e:
        fid = e["feed_id"]
        lib_cnt[fid] += 1
        ts = parse_ts(e.get("published_at")) or 0
        if ts > lib_last.get(fid, 0):
            lib_last[fid] = ts

    per_cat = defaultdict(int)
    cat_feeds = defaultdict(lambda: [0, 0])   # 分类 -> [有产出源数, 源总数]
    for fid, f in feeds.items():
        ctitle = (f.get("category") or {}).get("title", "(无分类)")
        cat_feeds[ctitle][1] += 1
        if fid in per_feed:
            cat_feeds[ctitle][0] += 1
            per_cat[ctitle] += len(per_feed[fid])

    rows = []
    for fid, f in feeds.items():
        items = per_feed.get(fid, [])
        ctitle = (f.get("category") or {}).get("title", "(无分类)")
        newest = max((parse_ts(e.get("published_at")) or 0) for e in items) if items else None
        susp = 0
        for e in items:
            pa, ca = parse_ts(e.get("published_at")), parse_ts(e.get("created_at"))
            if pa and ca and abs(pa - ca) < 60:
                susp += 1
        rows.append({
            "id": fid, "title": f.get("title", ""), "category": ctitle,
            "count": len(items), "newest": newest,
            "lib_last": lib_last.get(fid), "lib_count": lib_cnt.get(fid, 0),
            "age_days": round((now - lib_last[fid]) / 86400, 1) if fid in lib_last else None,
            "suspect_pubdate": susp,
            "feed_url": f.get("feed_url", ""),
            "checked_at": f.get("checked_at", ""),
        })
    rows.sort(key=lambda r: (r["category"], -r["count"], r["id"]))

    hourly = None
    if by_hour:
        buckets = defaultdict(int)
        for e in win:
            ts = parse_ts(e.get("published_at"))
            if ts:
                buckets[datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:00")] += 1
        hourly = sorted(buckets.items())

    # 无源分类（元数据里有，但没有任何 feed 挂在下面）
    empty_cats = [t for i, t in cats.items() if t not in cat_feeds]

    return {
        "now": now, "window_start": t0, "hours": hours,
        "total": len(win), "verified": verified,
        "pub_recent": len(win), "created_recent": created_recent,
        "both": both, "backfilled": backfilled, "ahead": ahead,
        "backfill_by_feed": dict(bf_by_feed),
        "created_hourly": sorted(created_hourly.items()),
        "per_cat": dict(per_cat), "cat_feeds": dict(cat_feeds),
        "rows": rows, "hourly": hourly, "empty_cats": empty_cats,
        "feed_total": len(feeds), "library_total": len(all_e),
    }


# ---------------------------------------------------------------- 渲染

def render_text(rep, tz) -> str:
    L = []
    L.append("Miniflux 订阅产出统计 —— 口径：文章发布时间（published_at / feed 的 pubDate）")
    L.append(f"窗口：{fmt_local(rep['window_start'], tz)} → {fmt_local(rep['now'], tz)}"
             f"（滚动 {rep['hours']} 小时，本地时区 {time.strftime('%Z')}）")
    L.append(f"时间窗口内条目：{rep['total']} 条（库共 {rep['library_total']} 条 / {rep['feed_total']} 个源）")
    v = rep["verified"]
    if v:
        flag = "✓ 一致" if v["api"] == v["local"] else f"✗ 不一致（差 {v['api'] - v['local']}）"
        L.append(f"[交叉校验] API published_after 口径 {v['api']} / 本地全量过滤 {v['local']} → {flag}")
    L.append(f"[口径对比] 过去 {rep['hours']}h：按【发布时间 pubDate】{rep['pub_recent']} 条 "
             f"／ 按【入库时间 created_at】{rep['created_recent']} 条")
    if rep["backfilled"]:
        L.append(f"           → 其中 {rep['backfilled']} 条「发布早于窗口、本次才入库」= 补拉的历史文章"
                 f"（新源首次导入 / 长时间停更后恢复），不该算进 24h 发布量")
        names = {r["id"]: r["title"] for r in rep["rows"]}
        top = sorted(rep["backfill_by_feed"].items(), key=lambda x: -x[1])[:5]
        L.append("           → 主要来自：" + "、".join(
            f"#{fid} {names.get(fid, '?')[:18]} {n}条" for fid, n in top))
    if rep["ahead"]:
        L.append(f"           → 另有 {rep['ahead']} 条「发布在窗口内、入库却更早」= feed 给了未来时间戳（口径异常）")
    if not rep["backfilled"] and not rep["ahead"]:
        L.append("           → 两口径完全重合：窗口内既无补拉，也无未来时间戳异常")
    L.append("")

    L.append("【按分类】")
    L.append(f"{'分类':<10} {'条数':>5} {'占比':>7}   {'有产出/总源数'}")
    L.append("-" * 56)
    for cname, n in sorted(rep["per_cat"].items(), key=lambda x: -x[1]):
        got, tot = rep["cat_feeds"].get(cname, [0, 0])
        share = f"{n / rep['total'] * 100:.1f}%" if rep["total"] else "-"
        L.append(f"{cname:<10} {n:>5} {share:>7}   {got}/{tot}")
    zero_cats = [c for c in rep["cat_feeds"] if c not in rep["per_cat"]]
    for c in zero_cats:
        L.append(f"{c:<10} {0:>5} {'-':>7}   {0}/{rep['cat_feeds'][c][1]}   ← 窗口内无产出")
    if rep["empty_cats"]:
        L.append(f"{'— 空分类 —':<10} {'':>5} {'':>7}   {'、'.join(rep['empty_cats'])}（未挂任何源）")
    L.append("")

    L.append("【按源明细】0 条的源会显示「库内最新发布」以判断是否已停更")
    L.append(f"{'分类':<8} {'ID':>3} {'条数':>5} {'窗口最新':<12} {'库内最新':<12} {'存疑':>4}  源")
    L.append("-" * 104)
    for r in rep["rows"]:
        susp = str(r["suspect_pubdate"]) if r["suspect_pubdate"] else ""
        L.append(f"{r['category']:<8} {r['id']:>3} {r['count']:>5} "
                 f"{fmt_local(r['newest'], tz):<12} {fmt_local(r['lib_last'], tz):<12} "
                 f"{susp:>4}  {r['title'][:42]}")
    L.append("")

    dead = [r for r in rep["rows"] if r["count"] == 0]
    if dead:
        L.append(f"【窗口内 0 条的源】{len(dead)} 个（库内最新发布时间 → 判断是「本来就没更新」还是「源挂了」）")
        for r in dead:
            age = f"{r['age_days']:.0f} 天前" if r["age_days"] is not None else "?"
            L.append(f"  #{r['id']:<3} {r['category']:<6} 库内最新 {fmt_local(r['lib_last'], tz)}"
                     f"（{age}） 共 {r['lib_count']:>3} 条  {r['title'][:38]}")
        L.append("")

    if rep["hourly"]:
        pd = dict(rep["hourly"])
        cd = dict(rep["created_hourly"] or [])
        mx = max(list(pd.values()) + list(cd.values()) + [1])
        L.append("【每小时分布】（本地时区） █ = 按发布时间（内容真实节奏）  ▒ = 按入库时间（宿主实际抓取）")
        for h in sorted(set(pd) | set(cd)):
            p, c = pd.get(h, 0), cd.get(h, 0)
            L.append(f"  {h}  发布{p:>3}  入库{c:>3}  {'█' * round(p / mx * 24):<24}| {'▒' * round(c / mx * 24)}")
        gaps = [h for h in cd if h not in pd]
        if gaps:
            L.append(f"  ⚠ 仅有入库、无发布的小时（宿主机在该时段补拉）：{'、'.join(gaps[:8])}")
        L.append("")

    susp_rows = [r for r in rep["rows"] if r["suspect_pubdate"]]
    if susp_rows:
        L.append("【⚠ pubDate 存疑的源】published_at ≈ created_at → 源很可能没给 pubDate，")
        L.append("  Miniflux 用抓取时间顶替，此时「发布时间」实为「拉到的时间」，口径失真：")
        for r in susp_rows:
            L.append(f"  #{r['id']:<3} {r['category']:<6} {r['count']:>3} 条中 {r['suspect_pubdate']} 条存疑"
                     f"  {r['title'][:34]}")
        L.append("")

    return "\n".join(L)


def render_md(rep, tz) -> str:
    L = ["# Miniflux 订阅产出统计", "",
         "- 口径：**文章发布时间**（feed 的 `pubDate` → Miniflux `published_at`），非抓取时间 `created_at`",
         f"- 窗口：**{fmt_local(rep['window_start'], tz)} → {fmt_local(rep['now'], tz)}**"
         f"（滚动 {rep['hours']} 小时，{time.strftime('%Z')}）",
         f"- 窗口内条目：**{rep['total']}** 条（库共 {rep['library_total']} 条 / {rep['feed_total']} 个源）",
         f"- 口径对比：按**发布时间** {rep['pub_recent']} 条 ／ 按**入库时间** {rep['created_recent']} 条"
         f"（补拉的旧文章 {rep['backfilled']} 条，未来时间戳异常 {rep['ahead']} 条）"]
    if rep["verified"]:
        v = rep["verified"]
        L.append(f"- 交叉校验：API 口径 {v['api']} / 本地全量过滤 {v['local']}")
    L += ["", "## 按分类", "", "| 分类 | 条数 | 占比 | 有产出/总源数 |", "|---|---:|---:|---:|"]
    for cname, n in sorted(rep["per_cat"].items(), key=lambda x: -x[1]):
        got, tot = rep["cat_feeds"].get(cname, [0, 0])
        L.append(f"| {cname} | {n} | {n / rep['total'] * 100:.1f}% | {got}/{tot} |")
    for c in rep["cat_feeds"]:
        if c not in rep["per_cat"]:
            L.append(f"| {c} | 0 | 0.0% | 0/{rep['cat_feeds'][c][1]} |")
    L += ["", "## 按源明细", "",
          "| 分类 | ID | 条数 | 窗口最新发布 | 库内最新发布 | pubDate存疑 | 源 |",
          "|---|---:|---:|---|---|---:|---|"]
    for r in rep["rows"]:
        L.append(f"| {r['category']} | {r['id']} | {r['count']} | {fmt_local(r['newest'], tz)} "
                 f"| {fmt_local(r['lib_last'], tz)} | {r['suspect_pubdate'] or ''} | {r['title']} |")
    if rep["hourly"]:
        L += ["", "## 每小时分布（本地时区）", "", "| 小时 | 条数 |", "|---|---:|"]
        for h, n in rep["hourly"]:
            L.append(f"| {h} | {n} |")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Miniflux 订阅产出统计（按文章发布时间）")
    ap.add_argument("--hours", type=int, default=24, help="滚动窗口小时数，默认 24")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"Miniflux 地址，默认 {DEFAULT_BASE}")
    ap.add_argument("--env", default=".env", help=".env 路径，默认 ./.env")
    ap.add_argument("--json", dest="json_out", help="落盘 JSON")
    ap.add_argument("--md", dest="md_out", help="落盘 Markdown")
    ap.add_argument("--by-hour", action="store_true", help="附每小时分布")
    ap.add_argument("--no-verify", action="store_true", help="跳过 API 口径交叉校验")
    args = ap.parse_args()

    load_env(args.env)
    if not os.environ.get("MINIFLUX_ADMIN_USERNAME"):
        print("[ERROR] 缺少 MINIFLUX_ADMIN_USERNAME（检查 .env）", file=sys.stderr)
        return 2
    get = make_client(args.base)
    tz = datetime.now().astimezone().tzinfo

    try:
        rep = build_report(get, args.hours, not args.no_verify, args.by_hour, tz)
    except urllib.error.HTTPError as e:
        print(f"[ERROR] Miniflux API HTTP {e.code}: {e.read()[:200]}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    print(render_text(rep, tz))

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"\n→ JSON 已写入 {args.json_out}")
    if args.md_out:
        os.makedirs(os.path.dirname(args.md_out) or ".", exist_ok=True)
        with open(args.md_out, "w", encoding="utf-8") as f:
            f.write(render_md(rep, tz))
        print(f"→ Markdown 已写入 {args.md_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
