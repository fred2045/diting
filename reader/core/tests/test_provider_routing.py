"""按任务分路由（agnes 降本）：价目表 / 路由决策 / 跨 provider 回退 / 贡献聚合。

『出错谁会知道』是本模块的设计主线：
  · 价目表：agnes 免费期必须记 0 而非 None（否则 cost_cny NULL，按 provider 聚合会漏）；
  · 路由：默认 / 无 key / 不在白名单一律 deepseek（零风险默认，政策突变可一键回退）；
  · 回退：失败必降级、budget_stop 不降级（否则绕过预算闸）、凭证错用 ERROR；
  · 聚合：贡献靠 token 不靠金额（agnes 金额恒 0），估算明确量级。

方案见 docs/design/llm-provider-routing.md（不入库）。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core import citeguard
from core import costreport as cr
from core import llm, provider_route, translate as tr, triage
from core.models import AppSetting, LlmUsage

R = cr.ChatResult


def _row(kind="translate_title", provider="agnes", prompt=100, completion=50,
         cost="0", ts=None, ok=True):
    row = LlmUsage.objects.create(
        kind=kind, provider=provider, model="m",
        prompt_tokens=prompt, completion_tokens=completion,
        cost_cny=Decimal(cost), cost_usd=Decimal("0"), ok=ok)
    if ts is not None:
        LlmUsage.objects.filter(pk=row.pk).update(ts=ts)
    return row


class PriceTest(SimpleTestCase):
    def test_agnes_free_returns_zero_not_none(self):
        usd, cny = llm.estimate_cost("agnes", {"prompt_tokens": 100,
                                              "completion_tokens": 50})
        self.assertEqual((usd, cny), (0.0, 0.0),
                         "agnes 免费期必须返回 0.0 而非 None，否则落库 NULL")

    def test_unknown_provider_still_none(self):
        self.assertEqual(llm.estimate_cost("bogus", {}), (None, None))

    def test_deepseek_unchanged(self):
        usd, cny = llm.estimate_cost("deepseek", {"prompt_tokens": 100,
                                                  "completion_tokens": 50})
        self.assertGreater(usd, 0)
        self.assertGreater(cny, usd)


class RouteTest(SimpleTestCase):
    def test_default_no_key_is_deepseek(self):
        with override_settings(AGNES_API_KEY=""):
            self.assertEqual(provider_route.resolve("translate"), "deepseek")

    def test_non_whitelisted_task_always_deepseek(self):
        with override_settings(AGNES_API_KEY="sk-test"):
            # triage 已于 2026-11-25 复核后移入白名单（见 TriageRouteTest）
            self.assertEqual(provider_route.resolve("summary"), "deepseek")
            self.assertEqual(provider_route.resolve("weekly"), "deepseek")

    def test_unknown_task_key_is_deepseek(self):
        # settingkit 注册表里没有的任务 → 开关视为关，不能 500
        self.assertEqual(provider_route.resolve("bogus"), "deepseek")

    def test_complex_tasks_never_route_to_agnes(self):
        self.assertFalse(provider_route.AGNES_TASKS
                         & provider_route.AGNES_FORBIDDEN_TASKS,
                         "白名单与复杂任务集合不许相交")
        with override_settings(AGNES_API_KEY="sk-test"):
            for task in sorted(provider_route.AGNES_FORBIDDEN_TASKS):
                self.assertEqual(provider_route.resolve(task), "deepseek", task)

    def test_forbidden_task_stays_deepseek_even_if_whitelisted(self):
        # ★ 变异反证式：就算有人误把 wiki 加进白名单，程序闸门仍要挡住
        with mock.patch.object(provider_route, "AGNES_TASKS",
                               frozenset({"wiki"})), \
             override_settings(AGNES_API_KEY="sk-test"):
            self.assertEqual(provider_route.resolve("wiki"), "deepseek")
            self.assertEqual(provider_route.call_params("wiki", "agnes"), {})


class CallParamsTest(SimpleTestCase):
    def test_agnes_uses_short_params(self):
        self.assertEqual(provider_route.call_params("translate", "agnes"),
                         {"retries": 2, "timeout": 30})

    def test_wiki_related_uses_interactive_short_params(self):
        # 审批浮层是交互路径：比后台翻译更急，失败 31s 内即回退
        self.assertEqual(provider_route.call_params("wiki_related", "agnes"),
                         {"retries": 2, "timeout": 15})

    def test_triage_uses_background_short_params(self):
        # 分级是后台 scheduler，慢点无害，与翻译同档（ds 兜底仍用原参数）
        self.assertEqual(provider_route.call_params("triage", "agnes"),
                         {"retries": 2, "timeout": 30})

    def test_other_wiki_tasks_never_use_agnes_params(self):
        for task in ("wiki", "wiki_compile", "wiki_relate", "wiki_recommend"):
            self.assertEqual(provider_route.call_params(task, "agnes"), {}, task)

    def test_deepseek_and_unknown_keep_original_params(self):
        self.assertEqual(provider_route.call_params("translate", "deepseek"), {})
        self.assertEqual(provider_route.call_params("wiki_related", "deepseek"), {})
        self.assertEqual(provider_route.call_params("bogus", "agnes"), {})

    def test_returned_params_are_a_copy(self):
        p = provider_route.call_params("translate", "agnes")
        p["timeout"] = 999
        self.assertEqual(
            provider_route.call_params("translate", "agnes")["timeout"], 30)


class RouteDbTest(TestCase):
    def test_flag_on_with_key_is_agnes(self):
        from django.core.cache import cache
        cache.clear()
        AppSetting.objects.update_or_create(key="translate_prefer_agnes",
                                            defaults={"value": "1"})
        cache.clear()
        try:
            with override_settings(AGNES_API_KEY="sk-test"):
                self.assertEqual(provider_route.resolve("translate"), "agnes")
        finally:
            AppSetting.objects.filter(key="translate_prefer_agnes").delete()
            cache.clear()

    def test_wiki_related_flag_on_with_key_is_agnes(self):
        from django.core.cache import cache
        cache.clear()
        AppSetting.objects.update_or_create(key="wiki_related_prefer_agnes",
                                            defaults={"value": "1"})
        cache.clear()
        try:
            with override_settings(AGNES_API_KEY="sk-test"):
                self.assertEqual(provider_route.resolve("wiki_related"), "agnes")
                # 同属 wiki，但编译/对话是复杂任务，不能被顺带切过去
                self.assertEqual(provider_route.resolve("wiki_compile"), "deepseek")
        finally:
            AppSetting.objects.filter(key="wiki_related_prefer_agnes").delete()
            cache.clear()

    def test_triage_flag_on_with_key_is_agnes(self):
        from django.core.cache import cache
        cache.clear()
        AppSetting.objects.update_or_create(key="triage_prefer_agnes",
                                            defaults={"value": "1"})
        cache.clear()
        try:
            with override_settings(AGNES_API_KEY="sk-test"):
                self.assertEqual(provider_route.resolve("triage"), "agnes")
                # 分级切了不等于日报/周报也切 —— 其余复杂任务闸门仍在
                self.assertEqual(provider_route.resolve("summary"), "deepseek")
                self.assertEqual(provider_route.resolve("weekly"), "deepseek")
                self.assertEqual(provider_route.resolve("wiki_compile"), "deepseek")
        finally:
            AppSetting.objects.filter(key="triage_prefer_agnes").delete()
            cache.clear()

    def test_triage_flag_on_without_key_stays_deepseek(self):
        from django.core.cache import cache
        cache.clear()
        AppSetting.objects.update_or_create(key="triage_prefer_agnes",
                                            defaults={"value": "1"})
        cache.clear()
        try:
            with override_settings(AGNES_API_KEY=""):
                self.assertEqual(provider_route.resolve("triage"), "deepseek")
        finally:
            AppSetting.objects.filter(key="triage_prefer_agnes").delete()
            cache.clear()


class ClassifyTest(SimpleTestCase):
    def test_categories(self):
        cases = [
            ("HTTP 401（认证失败，重试无意义）", "auth"),
            ("HTTP 403（认证失败，重试无意义）", "auth"),
            ("缺少 AGNES_API_KEY", "missing_key"),
            ("HTTPError: HTTP Error 429: Too Many Requests", "rate_limit"),
            ("TimeoutError: timed out", "transient"),
            ("空响应：HTTP 200", "transient"),
        ]
        for err, want in cases:
            self.assertEqual(cr.classify_provider_error(err), want, err)


class FallbackTest(SimpleTestCase):
    """桩 cr.chat_recorded：验证「失败降级 / 成功不降 / budget_stop 不降」。"""

    def test_success_no_fallback(self):
        with mock.patch.object(cr, "chat_recorded",
                               return_value=R("ok", {}, "", False)) as m:
            res = cr.chat_recorded_with_fallback("t", [], primary="agnes")
            self.assertEqual(res.text, "ok")
            self.assertEqual([c.kwargs.get("provider") for c in m.call_args_list],
                             ["agnes"])

    def _assert_degrades(self, err):
        with mock.patch.object(cr, "chat_recorded", side_effect=[
                R("", {}, err, False), R("fb", {}, "", False)]) as m:
            res = cr.chat_recorded_with_fallback("t", [], primary="agnes")
            self.assertEqual(res.text, "fb")
            self.assertEqual([c.kwargs.get("provider") for c in m.call_args_list],
                             ["agnes", "deepseek"])

    def test_rate_limit_degrades(self):
        self._assert_degrades("HTTPError: HTTP Error 429: Too Many Requests")

    def test_auth_degrades(self):
        self._assert_degrades("HTTP 401（认证失败，重试无意义）")

    def test_missing_key_degrades(self):
        self._assert_degrades("缺少 AGNES_API_KEY")

    def test_transient_degrades(self):
        self._assert_degrades("空响应：HTTP 200 但 choices=0")

    def test_budget_stop_does_not_degrade(self):
        # ★ 闸下没调模型没记账，换 provider 也过不了同一道闸 —— 降级 = 绕过预算
        with mock.patch.object(cr, "chat_recorded",
                               return_value=R("", {}, "预算已用尽", True)) as m:
            res = cr.chat_recorded_with_fallback("t", [], primary="agnes")
            self.assertTrue(res.budget_stop)
            self.assertEqual(m.call_count, 1)

    def test_primary_params_override_only_the_first_call(self):
        # ★ agnes 缩短重试/超时，不能顺手把兜底 deepseek 的长任务能力也砍掉
        with mock.patch.object(cr, "chat_recorded", side_effect=[
                R("", {}, "429", False), R("fb", {}, "", False)]) as m:
            cr.chat_recorded_with_fallback(
                "t", [], primary="agnes", retries=3, timeout=90,
                primary_params={"retries": 2, "timeout": 30})
        first, second = m.call_args_list
        self.assertEqual((first.kwargs["retries"], first.kwargs["timeout"]),
                         (2, 30))
        self.assertEqual((second.kwargs["retries"], second.kwargs["timeout"]),
                         (3, 90))

    def test_kwargs_passthrough(self):
        with mock.patch.object(cr, "chat_recorded", side_effect=[
                R("", {}, "429", False), R("fb", {}, "", False)]) as m:
            cr.chat_recorded_with_fallback(
                "t", [], primary="agnes", temperature=0, max_tokens=123,
                feed_id=7, entry_id=9)
            for c in m.call_args_list:
                self.assertEqual(c.kwargs["temperature"], 0)
                self.assertEqual(c.kwargs["max_tokens"], 123)
                self.assertEqual(c.kwargs["feed_id"], 7)
                self.assertEqual(c.kwargs["entry_id"], 9)


class ByProviderTest(TestCase):
    def test_agnes_token_pct_and_counterfactual(self):
        now = timezone.now()
        _row(provider="deepseek", prompt=1000, completion=500, cost="0.1", ts=now)
        _row(provider="agnes", prompt=2000, completion=1000, cost="0", ts=now)
        d = cr.by_provider_range(cr.last_days(1))
        rows = {r["key"]: r for r in d["rows"]}
        self.assertEqual(set(rows), {"deepseek", "agnes"})
        self.assertEqual(rows["agnes"]["tokens"], 3000)
        self.assertEqual(rows["deepseek"]["tokens"], 1500)
        # 3000 / 4500 = 66.7%
        self.assertAlmostEqual(d["agnes_token_pct"], 3000 / 4500 * 100, places=1)
        self.assertTrue(d["has_agnes"])
        # 反事实 > 0（agnes 自己 cost=0，但「若走 deepseek」要花钱）
        self.assertGreater(d["saved_cny_est"], 0)

    def test_no_agnes_zero_saved(self):
        _row(provider="deepseek", ts=timezone.now())
        d = cr.by_provider_range(cr.last_days(1))
        self.assertFalse(d["has_agnes"])
        self.assertEqual(d["saved_cny_est"], 0.0)

    def test_unknown_provider_bucket(self):
        _row(provider="", ts=timezone.now())
        d = cr.by_provider_range(cr.last_days(1))
        self.assertIn("unknown", {r["key"] for r in d["rows"]})


class TranslateCallTest(SimpleTestCase):
    """钉住唯一业务落点：短参数只给 agnes，开关关时调用形状一个字都不变。"""

    def _call_with(self, resolved):
        fake = R("[]", {}, "", False, None)
        with mock.patch.object(provider_route, "resolve", return_value=resolved), \
             mock.patch.object(cr, "chat_recorded_with_fallback",
                               return_value=fake) as mfb, \
             mock.patch.object(cr, "chat_recorded", return_value=fake) as mds:
            tr._call([{"id": 1, "title": "Hello"}], feed_id=1, category_id=2)
        return mfb, mds

    def test_agnes_call_gets_short_params(self):
        mfb, mds = self._call_with("agnes")
        mds.assert_not_called()
        kw = mfb.call_args.kwargs
        self.assertEqual(kw.get("primary_params"),
                         {"retries": 2, "timeout": 30})
        # 兜底参数仍在原 kwargs 里，fallback 那一次会原样用
        self.assertEqual((kw["retries"], kw["timeout"]), (3, 90))

    def test_flag_off_keeps_original_call_shape(self):
        mfb, mds = self._call_with("deepseek")
        mfb.assert_not_called()
        mds.assert_called_once()
        kw = mds.call_args.kwargs
        self.assertNotIn("primary_params", kw)
        self.assertEqual((kw["retries"], kw["timeout"]), (3, 90))


class CiteguardRouteTest(SimpleTestCase):
    """★ citeguard._call 是几十处共用口：没给 route_task 时一个字都不许变。"""

    def _call(self, resolved=None, **extra):
        fake = R("[]", {}, "", False, None)
        with mock.patch.object(provider_route, "resolve",
                               return_value=resolved) as route, \
             mock.patch.object(cr, "chat_recorded", return_value=fake) as mds, \
             mock.patch.object(cr, "chat_recorded_with_fallback",
                               return_value=fake) as mfb:
            citeguard._call("wiki_related", "sys", "user",
                            citeguard.fresh_stats(),
                            pool=cr.POOL_WIKI, max_calls=1, **extra)
        return route, mds, mfb

    def test_route_task_on_uses_fallback_with_short_params(self):
        _route, mds, mfb = self._call("agnes", route_task="wiki_related")
        mds.assert_not_called()
        mfb.assert_called_once()
        kw = mfb.call_args.kwargs
        self.assertEqual((kw["primary"], kw["fallback"]), ("agnes", "deepseek"))
        self.assertEqual(kw["primary_params"], {"retries": 2, "timeout": 15})
        # ★ 兜底那次仍用 citeguard 原参数，不跟着主通道一起缩短
        self.assertEqual((kw["retries"], kw["timeout"]), (2, 180))

    def test_route_task_off_keeps_original_call(self):
        _route, mds, mfb = self._call("deepseek", route_task="wiki_related")
        mfb.assert_not_called()
        mds.assert_called_once()
        kw = mds.call_args.kwargs
        self.assertNotIn("primary_params", kw)
        self.assertEqual((kw["retries"], kw["timeout"]), (2, 180))

    def test_no_route_task_never_even_reads_router(self):
        _route, mds, mfb = self._call(None)
        _route.assert_not_called()
        mds.assert_called_once()
        mfb.assert_not_called()


class TriageChatCallTest(SimpleTestCase):
    """★ 分级唯一调用出口：开关形状 + agnes 失败先降级 ds（不是直接 fail_open）。"""

    ROW = '[{"id":42,"level":2,"why":"具体标的"}]'

    def _call(self, resolved):
        fake = R(self.ROW, {}, "", False, None)
        with mock.patch.object(provider_route, "resolve", return_value=resolved), \
             mock.patch.object(cr, "chat_recorded_with_fallback",
                               return_value=fake) as mfb, \
             mock.patch.object(cr, "chat_recorded", return_value=fake) as mds:
            res = triage._chat_triage(
                [{"role": "user", "content": "x"}], temperature=0.1,
                max_tokens=4096, retries=3, timeout=90, feed_id=7, category_id=2)
        return res, mfb, mds

    def test_flag_on_uses_fallback_with_short_primary_params(self):
        res, mfb, mds = self._call("agnes")
        mds.assert_not_called()
        mfb.assert_called_once()
        kw = mfb.call_args.kwargs
        self.assertEqual((kw["primary"], kw["fallback"]), ("agnes", "deepseek"))
        self.assertEqual(kw["primary_params"], {"retries": 2, "timeout": 30})
        # ★ 兜底那次仍用 triage 原参数，ds 的 3×90s 不跟着缩水
        self.assertEqual((kw["retries"], kw["timeout"]), (3, 90))
        self.assertEqual((kw["max_tokens"], kw["feed_id"], kw["category_id"]),
                         (4096, 7, 2))
        self.assertEqual(triage.parse_rows(res.text)[0]["level"], 2)

    def test_flag_off_keeps_original_call_shape(self):
        res, mfb, mds = self._call("deepseek")
        mfb.assert_not_called()
        mds.assert_called_once()
        kw = mds.call_args.kwargs
        self.assertNotIn("primary_params", kw)
        self.assertNotIn("primary", kw)
        self.assertEqual((kw["retries"], kw["timeout"], kw["max_tokens"]),
                         (3, 90, 4096))
        self.assertEqual(triage.parse_rows(res.text)[0]["level"], 2)

    def test_agnes_fails_ds_succeeds_returns_parsable_rows(self):
        """★ agnes 429 → ds 兜底：调用方拿到的是 ds 的合法正文，不是 fail_open。"""
        ds_result = R(self.ROW, {}, "", False)
        with mock.patch.object(provider_route, "resolve", return_value="agnes"), \
             mock.patch.object(cr, "chat_recorded", side_effect=[
                 R("", {}, "HTTPError: HTTP Error 429", False), ds_result]):
            res = triage._chat_triage(
                [], temperature=0.1, max_tokens=4096, retries=3, timeout=90,
                feed_id=1, category_id=1)
        self.assertFalse(res.err, "ds 兜住后 err 应为空")
        self.assertEqual(triage.parse_rows(res.text)[0]["level"], 2,
                         "调用方应能从 ds 结果里正常取票")

    def test_both_providers_fail_returns_err_to_caller(self):
        """agnes 与 ds 双双失败 → err 非空 → 调用方才走 fail_open。"""
        with mock.patch.object(provider_route, "resolve", return_value="agnes"), \
             mock.patch.object(cr, "chat_recorded", side_effect=[
                 R("", {}, "429", False), R("", {}, "ds 也失败", False)]):
            res = triage._chat_triage(
                [], temperature=0.1, max_tokens=4096, retries=3, timeout=90,
                feed_id=1, category_id=1)
        self.assertEqual(res.err, "ds 也失败")

    def test_budget_stop_propagates_without_degrade(self):
        """闸下不降级（否则绕过预算闸），budget_stop 原样交回调用方。"""
        with mock.patch.object(provider_route, "resolve", return_value="agnes"), \
             mock.patch.object(cr, "chat_recorded",
                               return_value=R("", {}, "预算已用尽", True)) as m:
            res = triage._chat_triage(
                [], temperature=0.1, max_tokens=4096, retries=3, timeout=90,
                feed_id=1, category_id=1)
        self.assertTrue(res.budget_stop)
        self.assertEqual(m.call_count, 1, "闸下只调一次，不换 provider 重跑")


class TriageMixedVoteTest(SimpleTestCase):
    """★★ 用户点名的边界：agnes 前两票成功、第三票失败降级 ds —— 票箱照常聚合。

    每票是独立的 `_chat_triage` 调用，第三票由 fallback 内部换 ds 重跑、不牵连前两票。
    这里用**真的 res.text** 走真的 `parse_rows` + `aggregate_level`，验证三票都进票箱、
    聚合照常、来源混合不影响结论 —— 而不是手塞票再自证。
    """

    def _vote_text(self, level, why):
        return '[{"id":42,"level":%d,"why":"%s"}]' % (level, why)

    def test_two_agnes_one_ds_votes_aggregate_normally(self):
        # 三票真文本：票1/2 = agnes 成功直出；票3 = agnes 429 → ds 兜底成功
        texts = [
            self._vote_text(2, "agnes 说具体标的"),
            self._vote_text(2, "agnes 说有增量"),
            self._vote_text(2, "ds 兜底说具体标的"),
        ]
        votes = []
        with mock.patch.object(provider_route, "resolve", return_value="agnes"):
            for i, text in enumerate(texts):
                seq = ([R("", {}, "HTTP 429", False), R(text, {}, "", False)]
                       if i == 2 else [R(text, {}, "", False)])
                with mock.patch.object(cr, "chat_recorded", side_effect=seq):
                    res = triage._chat_triage(
                        [], temperature=0.1, max_tokens=4096,
                        retries=3, timeout=90, feed_id=1, category_id=1)
                self.assertFalse(res.err, "每票都应有合法正文（第三票由 ds 兜住）")
                rows = [r for r in triage.parse_rows(res.text) if r["id"] == 42]
                self.assertEqual(len(rows), 1, "每票都应能解析出一条有效票")
                votes.append(rows[0])
        self.assertEqual(len(votes), 3, "部分票降级 ds 不该让票箱变短")
        level, info = triage.aggregate_level(votes, default_level=2)
        self.assertEqual(level, 2)
        self.assertEqual(info["counts"], {"2": 3})
        self.assertTrue(any("ds" in v["why"] for v in votes), "第三票应来自 ds")
        self.assertTrue(any("agnes" in v["why"] for v in votes), "前两票应来自 agnes")

    def test_agnes_all_fail_ds_all_succeed_votes_intact(self):
        """agnes 票票 429 → 票票降级 ds：三票齐全，聚合照常，不 fail_open。"""
        text = self._vote_text(3, "ds 兜底说点名标的")
        votes = []
        with mock.patch.object(provider_route, "resolve", return_value="agnes"):
            for _ in range(3):
                with mock.patch.object(cr, "chat_recorded", side_effect=[
                        R("", {}, "429", False), R(text, {}, "", False)]):
                    res = triage._chat_triage(
                        [], temperature=0.1, max_tokens=4096,
                        retries=3, timeout=90, feed_id=1, category_id=1)
                rows = [r for r in triage.parse_rows(res.text) if r["id"] == 42]
                self.assertEqual(len(rows), 1)
                votes.append(rows[0])
        level, info = triage.aggregate_level(votes, default_level=2)
        self.assertEqual(level, 3)
        self.assertEqual(info["counts"], {"3": 3}, "三票全由 ds 兜住，票箱完整")

