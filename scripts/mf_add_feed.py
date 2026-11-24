#!/usr/bin/env python3
"""在 Miniflux 上添加订阅（可顺带创建分类）。

凭证从项目 .env 读取，不落命令行。典型用法：

  # 把豆瓣「即将上映」/「正在上映」加进「生活」分类（分类不存在则自动创建）
  python3 scripts/mf_add_feed.py --category 生活 --url /douban/movie/later
  python3 scripts/mf_add_feed.py --category 生活 --url /douban/movie/playing/7.5

  # 指定 rsshub 基础地址（默认 http://rsshub:1200）与是否开 crawler
  python3 scripts/mf_add_feed.py --category AI --url /arxiv/cs.AI --base http://rsshub:1200 --crawler

  # 只列出现有分类与订阅，不做修改
  python3 scripts/mf_add_feed.py --list

约定：--url 可以是 RSSHub 的**路由**（/douban/movie/later，自动补 --base 前缀），
也可以直接给**完整 feed URL**（http/https 开头，原样使用）。
"""
import argparse
import base64
import json
import os
import sys
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.request import Request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(HERE, ".env")

# ★ Miniflux 是本机服务，必须显式绕开 macOS 系统代理：
#   Python 的 urllib 只读「代理地址」，不读 macOS 系统代理的「例外列表」，
#   所以用户一旦开着 代理工具 之类的全局代理（127.0.0.1:7890），
#   连 http://localhost:8080 也会被丢给代理 → 代理不认识该地址 → 502。
#   curl / 裸 socket 都不受影响，只有 urllib 会中招。
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_env():
    """把 .env 里的键值对注入 os.environ（已存在的环境变量优先）。"""
    if not os.path.exists(ENV):
        return {}
    loaded = {}
    for line in open(ENV, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        loaded[k] = v
        if k and v and not os.environ.get(k):
            os.environ[k] = v
    return loaded


ENV_KEYS = load_env()
URL = os.environ.get("MINIFLUX_URL", "http://localhost:8080").rstrip("/")
USER = os.environ.get("MINIFLUX_USER") or os.environ.get("MINIFLUX_ADMIN_USERNAME", "")
PASS = os.environ.get("MINIFLUX_PASS") or os.environ.get("MINIFLUX_ADMIN_PASSWORD", "")
API = os.environ.get("MINIFLUX_API_KEY", "")


def headers():
    h = {"Content-Type": "application/json"}
    if API:
        h["X-Auth-Token"] = API
    elif USER:
        h["Authorization"] = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()
    return h


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(URL + path, data=data, headers=headers(), method=method)
    try:
        with NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except URLError as e:
        return 0, f"网络错误：{e.reason}"


def full_feed_url(u, base):
    """路由以 / 开头 → 补 base 前缀；已是 http(s) 完整地址 → 原样返回。"""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return base.rstrip("/") + "/" + u.lstrip("/")


def main():
    ap = argparse.ArgumentParser(description="在 Miniflux 上添加订阅")
    ap.add_argument("--url", help="RSSHub 路由（/douban/movie/later）或完整 feed URL")
    ap.add_argument("--category", help="分类名（不存在则创建）")
    ap.add_argument("--category-id", type=int, help="直接指定分类 id（优先于 --category）")
    ap.add_argument("--base", default="http://rsshub:1200", help="RSSHub 基础地址（默认 http://rsshub:1200）")
    ap.add_argument("--crawler", action="store_true", help="开启 crawler 抓全文（反爬站点勿开）")
    ap.add_argument("--list", action="store_true", help="只列出现有分类与订阅后退出")
    args = ap.parse_args()

    if not API and not USER:
        sys.exit("缺少凭证：请在 .env 配置 MINIFLUX_ADMIN_USERNAME / MINIFLUX_ADMIN_PASSWORD（或 MINIFLUX_API_KEY）")
    print(f"【连接】{URL}  认证方式：{'API token' if API else 'Basic ' + USER}")

    st, cats = call("GET", "/v1/categories")
    if st != 200:
        sys.exit(f"读取分类失败（{st}）：{cats}")
    print("\n【现有分类】")
    for c in cats:
        print(f"  id={c['id']:<3} {c['title']}")

    if args.list:
        st, feeds = call("GET", "/v1/feeds")
        if st == 200:
            print("\n【现有订阅】")
            for f in feeds:
                cat = (f.get("category") or {}).get("title", "?")
                print(f"  id={f['id']:<3} [{cat}] {f.get('title')}  <{f.get('feed_url')}>")
        return

    if not args.url:
        sys.exit("\n请用 --url 指定要添加的源（或用 --list 只查看）")

    # ---- 1. 解析分类 ----
    if args.category_id:
        cat_id = args.category_id
        hit = next((c for c in cats if c["id"] == cat_id), None)
        print(f"\n【分类】指定 id={cat_id}（{hit['title'] if hit else '未知，Miniflux 会拒绝'}）")
    else:
        if not args.category:
            sys.exit("\n请用 --category 指定分类名，或 --category-id 指定分类 id")
        hit = next((c for c in cats if c["title"] == args.category), None)
        if hit:
            cat_id = hit["id"]
            print(f"\n【分类】「{args.category}」已存在，复用 id={cat_id}")
        else:
            st, res = call("POST", "/v1/categories", {"title": args.category})
            if st not in (200, 201):
                sys.exit(f"创建分类失败（{st}）：{res}")
            cat_id = res["id"]
            print(f"\n【分类】已创建「{args.category}」 id={cat_id}")

    # ---- 2. 添加订阅 ----
    feed_url = full_feed_url(args.url, args.base)
    print(f"【目标源】{feed_url}")

    st, feeds = call("GET", "/v1/feeds")
    existing = next((f for f in feeds if f.get("feed_url") == feed_url), None) if st == 200 else None
    if existing:
        cur = (existing.get("category") or {}).get("id")
        print(f"【订阅】已存在 id={existing['id']}（当前分类 id={cur}）")
        if cur != cat_id:
            st, res = call("PUT", f"/v1/feeds/{existing['id']}", {"category_id": cat_id})
            print(f"        分类已更新为 id={cat_id}" if st == 200 else f"        改分类失败（{st}）：{res}")
        feed_id = existing["id"]
    else:
        st, res = call("POST", "/v1/feeds",
                       {"feed_url": feed_url, "category_id": cat_id, "crawler": args.crawler},
                       timeout=120)
        if st not in (200, 201):
            sys.exit(f"添加订阅失败（{st}）：{res}")
        feed_id = res["feed_id"]
        print(f"【订阅】已添加 id={feed_id}  「{res.get('title')}」")
        print(f"        category = {cat_id}  crawler = {args.crawler}")

    # ---- 3. 复核 ----
    st, f = call("GET", f"/v1/feeds/{feed_id}")
    if st == 200:
        print("\n【订阅详情】")
        print(f"  title    : {f.get('title')}")
        print(f"  category : {(f.get('category') or {}).get('title')} (id={(f.get('category') or {}).get('id')})")
        print(f"  crawler  : {f.get('crawler')}")
        print(f"  site_url : {f.get('site_url')}")
        print(f"  解析错误 : {f.get('parsing_error_message') or '无'}")
    st, ents = call("GET", f"/v1/feeds/{feed_id}/entries?limit=5")
    if st == 200:
        print(f"\n【最新条目】共 {ents.get('total')} 条")
        for e in ents.get("entries", [])[:5]:
            print(f"  #{e['id']} {(e.get('title') or '')[:52]}")


if __name__ == "__main__":
    main()
