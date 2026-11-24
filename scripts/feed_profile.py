#!/usr/bin/env python3
"""订阅源画像统计 —— 判断「这个源在说什么、值不值得分级、该屏蔽什么」。

与 scripts/feed_stats.py 的分工：
    feed_stats.py     → 产量口径（过去 N 小时多少条、按源/分类排名）
    feed_profile.py   → 内容口径（一个源里都是什么体裁、什么话题、文本多长）

专为「要不要做分级 / 要不要屏蔽转发回复」这类决策设计。

用法：
    python3 scripts/feed_profile.py --feed 29
    python3 scripts/feed_profile.py --feed 29 --md docs/ops/xxx.md
    python3 scripts/feed_profile.py --feed 29 --json /tmp/out.json
    python3 scripts/feed_profile.py --list-cats          # 看有哪些话题桶

------------ 数据来源与判据（关键，别搞错）------------
本脚本**只读 Miniflux API**，不碰阅读器 SQLite。

RSSHub twitter 路由产出的条目结构（源码级核实，2026-10-16）：
    正文 = <推文自己的文字> <hr/> <div class="rsshub-quote">被引用者: 被引用原文</div>
    ★ <hr/> 只在 is_quote_status == true 时出现 → 有 <hr/> == 引用转发（quote）
    ★ 回复（reply）不在 UserTweets 端点里；转推（RT）已被 excludeRetweet 过滤
    ⇒ 所以「原创 / 引用转发」二分是可靠的，且能反推「屏蔽回复转推」的真实收益

退出码：0 正常 / 2 接口失败
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime

DEFAULT_BASE = "http://localhost:8080"
LIMIT_MAX = 1000

# ------------------------------------------------------------------ 话题桶
# 顺序即优先级：一条条目按顺序命中第一个桶就算它的主话题。
TOPICS = [
    ("Grok/xAI 产品", r"\b(grok|xai|@bot|grokbot|grok bot)\b"),
    ("Starlink/SpaceX", r"\b(starlink|spacex|starship|falcon|superheavy|星链|星舰)\b"),
    ("Tesla/机器人", r"\b(tesla|cybertruck|optimus|robotaxi|roadster|model [ys3x]\b|fsd)\b"),
    ("AI/AGI/安全", r"\b(agi|asi|superintelligence|artificial intelligence|ai safety|llm|neural net)\b"),
    ("政治/美国/移民", r"\b(illegal|immigrant|migrant|border|election|president|congress|democrat|republican|uk|britain|europe|war|ukraine|gaza|israel|tariff)\b"),
    ("X 平台/内容生态", r"(𝕏|x\.com|twitter|town square|community note)"),
    ("加密/金融", r"\b(bitcoin|crypto|doge|xrp|\$[A-Z]{2,6}\b|stock market|ipo)\b"),
    ("其他/未分类", r"(?!)"),  # 兜底：永不匹配，靠 fallback 落进来
]


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
    """本机服务必须绕开 macOS 系统代理（否则 localhost 请求被送上游 → 502）。"""
    user = os.environ.get("MINIFLUX_ADMIN_USERNAME", "admin")
    pwd = os.environ.get("MINIFLUX_ADMIN_PASSWORD", "")
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path: str):
        req = urllib.request.Request(
            base.rstrip("/") + path, headers={"Authorization": "Basic " + token}
        )
        with opener.open(req, timeout=60) as resp:
            return json.load(resp)

    return get


def to_plain(raw: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", raw or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", html.unescape(t)).strip()


# ---------------------------------------------------------------- 条目解析
def fetch_entries(get, feed_id: int):
    out, offset = [], 0
    total = 0
    while True:
        d = get(f"/v1/feeds/{feed_id}/entries"
                f"?limit={LIMIT_MAX}&offset={offset}&order=published_at&direction=desc")
        total = d.get("total", 0)
        items = d.get("entries") or []
        out += items
        if len(out) >= total or not items:
            break
        offset += len(items)
    return total, out


def split_entry(e: dict) -> dict:
    """把 RSSHub 的复合正文切成「本人文字 / 被引用内容 / 媒体」。"""
    raw = e.get("content") or ""
    parts = raw.split("<hr/>")
    own = to_plain(parts[0])
    quoted = to_plain(parts[1]) if len(parts) > 1 else ""
    m = re.match(r"^([^:：\n]{1,40})[:：]", quoted)
    return {
        "id": e["id"],
        "pub": e.get("published_at") or e.get("created_at") or "",
        "url": e.get("url") or "",
        "own": own,
        "quoted": quoted,
        "quoted_by": (m.group(1).strip() if m else ""),
        "is_quote": len(parts) > 1,          # ★ 有 <hr/> == 引用转发
        "is_rt": own.startswith("RT @"),
        "media": ("视频" if "<video" in raw else
                  "图片" if "<img" in raw else "纯文字"),
    }


def main_topic(row: dict) -> str:
    blob = (row["own"] + " " + row["quoted"]).lower()
    for name, pat in TOPICS:
        if re.search(pat, blob):
            return name
    return "其他/未分类"


# ---------------------------------------------------------------- 报告
def build_report(feed: dict, total: int, rows: list, material_chars: int = 1200) -> str:
    n = len(rows)
    quotes = [r for r in rows if r["is_quote"]]
    rts = [r for r in rows if r["is_rt"]]
    pure = [r for r in rows if not r["is_quote"] and not r["is_rt"]]
    own_lens = [len(r["own"]) for r in rows]
    q_lens = [len(r["quoted"]) for r in quotes]

    def med(xs):
        return sorted(xs)[len(xs) // 2] if xs else 0

    # 素材体积：现状（condense 1200 字/条）vs 压成一句话（引用转发只留本人文字）
    raw_total = sum(len(r["own"]) + len(r["quoted"]) for r in rows)
    packed = sum(min(len(r["own"]) + len(r["quoted"]), material_chars) for r in rows)
    slim = sum(len(r["own"]) + min(len(r["quoted"]), 120) for r in rows)

    L = []
    L.append(f"# {feed.get('title')}（feed {feed.get('id')}）源画像")
    L.append("")
    L.append(f"- 路由：`{feed.get('feed_url')}`")
    L.append(f"- 分类：{feed.get('category', {}).get('title')}"
             f"（id={feed.get('category', {}).get('id')}）")
    L.append(f"- 库内条目：**{total}** 条；跨度 "
             f"{rows[-1]['pub'][:16] if rows else '-'} → {rows[0]['pub'][:16] if rows else '-'}")
    L.append("")
    L.append("## 一、体裁构成")
    L.append("")
    L.append("| 体裁 | 条数 | 占比 |")
    L.append("| --- | --- | --- |")
    L.append(f"| 引用转发（quote，带 `<hr/>`） | {len(quotes)} | {len(quotes)/n*100:.0f}% |")
    L.append(f"| 转推（`RT @`） | {len(rts)} | {len(rts)/n*100:.0f}% |")
    L.append(f"| 纯原创（无引用） | {len(pure)} | {len(pure)/n*100:.0f}% |")
    L.append("")
    if not quotes and not rts:
        L.append("> ⚠ 该源的正文里没有 `<hr/>` 与 `RT @` —— 通常说明它**不是 RSSHub twitter 路由**"
                 "（本站的 `rsshub-quote` 拼装只对 twitter 生效）。此时本表的「纯原创 100%」"
                 "是**口径产物，不代表该源真的都是原创**，请以第二节的话题分布为准。")
        L.append("")
    if quotes:
        names = Counter(r["quoted_by"] for r in quotes if r["quoted_by"])
        L.append(f"**被引用账号**：{len(names)} 个不同账号，"
                 f"其中 {sum(1 for v in names.values() if v == 1)} 个只出现 1 次")
        L.append("")
        top = "、".join(f"{k}({v})" for k, v in names.most_common(8))
        L.append(f"Top：{top}")
        L.append("")
        L.append(f"本人文字中位 **{med(own_lens)}** 字符 / 被引用原文中位 **{med(q_lens)}** 字符 "
                 f"⇒ 信息主体在**被引用方**，本人只做点评")
        L.append("")

    L.append("## 二、话题分布（按主话题，取第一个命中）")
    L.append("")
    tc = Counter(main_topic(r) for r in rows)
    L.append("| 话题 | 条数 | 占比 |")
    L.append("| --- | --- | --- |")
    for k, v in tc.most_common():
        L.append(f"| {k} | {v} | {v/n*100:.0f}% |")
    L.append("")
    # ★ 话题桶目前是围绕马斯克/科技类源手写的；落到这个比例说明桶不适配该源，
    #   数字没有解释力，改 TOPICS 后再看，别硬读。
    if tc.get("其他/未分类", 0) / n > 0.7:
        L.append(f"> ⚠ 「其他/未分类」占 {tc['其他/未分类']/n*100:.0f}%，说明本脚本的"
                 f"话题桶不适配这个源（需在 TOPICS 里补该领域的关键词），"
                 f"本表的其余分桶数字**没有解释力**。")
        L.append("")

    L.append("## 三、媒体与节奏")
    L.append("")
    mc = Counter(r["media"] for r in rows)
    L.append("媒体：" + "、".join(f"{k} {v}" for k, v in mc.most_common()))
    dd = Counter(r["pub"][:10] for r in rows)
    L.append("")
    L.append("按天：" + "、".join(f"{k} {dd[k]}" for k in sorted(dd)))
    L.append("")

    L.append("## 四、素材体积（总结模块成本）")
    L.append("")
    L.append("| 口径 | 总字数 | 相对现状 |")
    L.append("| --- | --- | --- |")
    L.append(f"| 现状（全量进 prompt） | {packed:,} | 100% |")
    L.append(f"| 压成一句话（引用转发只留本人文字+120字引文） | {slim:,} | "
             f"{slim/packed*100:.0f}% |")
    L.append("")
    L.append("> 原文总字数（未截断）" + f" {raw_total:,}；单条上限 {material_chars} 字。")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", type=int, help="feed id")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--md", help="输出 markdown 到该路径")
    ap.add_argument("--json", help="输出原始统计到该路径")
    ap.add_argument("--list-cats", action="store_true", help="只列出话题桶")
    args = ap.parse_args()

    if args.list_cats:
        for name, pat in TOPICS:
            print(f"{name:16s} {pat}")
        return 0
    if not args.feed:
        ap.error("需要 --feed <id>")

    load_env(args.env)
    try:
        get = make_client(args.base)
        feed = get(f"/v1/feeds/{args.feed}")
        total, entries = fetch_entries(get, args.feed)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(f"接口失败：{exc}", file=sys.stderr)
        return 2

    rows = [split_entry(e) for e in entries]
    rows.sort(key=lambda r: r["pub"])
    md = build_report(feed, total, rows)

    print(f"feed {args.feed} {feed.get('title')}：库内 {total} 条，"
          f"引用转发 {sum(r['is_quote'] for r in rows)} 条")
    if args.md:
        os.makedirs(os.path.dirname(os.path.abspath(args.md)), exist_ok=True)
        open(args.md, "w", encoding="utf-8").write(md)
        print("已写", args.md)
    else:
        print(md)
    if args.json:
        json.dump({"feed": feed, "total": total, "rows": rows},
                  open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已写", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
