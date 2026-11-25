"""按任务分路由：决定每个 LLM 任务走哪个 provider。

★ 为什么独立成模块（而不是塞进 llm.py）：llm.py 是最底层的 HTTP + 记账层，不该
  依赖 settingkit / 业务开关。路由是**业务决策**（读设置、读有没有 key、查白名单），
  放这里职责单一、好测、好守卫。调用方（如 translate._call）只问
  `resolve("translate")`，拿到的就是该用的 provider 名。

★ 零风险默认：任何开关没开 / key 缺失 / 任务不在白名单 → deepseek（现状行为）。
  Agnès 只是「可选降本通道」，不是唯一依赖 —— 政策突变时关开关即回退。

★ 设计细节见 docs/design/llm-provider-routing.md（不入库）。
"""
from django.conf import settings

from core import settingkit as sk

# 允许走 agnes 的任务白名单（显式集合，不搞隐式规则）。
# 标题翻译：纯语言任务、短上下文短输出、质量敏感度低。
# wiki_related：审批浮层「找找相关页」，宽口径提示、人不采纳就否掉（错误成本低）。
# triage：分级。27 条人工复核里 agnes 判断力不弱于 ds（4 条分歧赢 3 平 1），
#   短板只是 429/偶发漏返回 —— 由 chat_recorded_with_fallback 整单降级 ds 兜住，
#   且 fail_open 只在 agnes 与 ds 双双失败时才发生。详见
#   docs/design/bench_triage_report.md（不入库）。★ 票数保持 3、不搞票数补偿。
# ★ 零 LLM 的 wiki_relate（确定式 token 相似度）和长上下文的 wiki 编译/对话都不在内。
# 将来放行新任务时往这里加一项即可，但必须先确认它不在下面的复杂任务集合里。
AGNES_TASKS = frozenset({"translate", "wiki_related", "triage"})

# 永不走 agnes 的任务（长上下文 / 判断型且不可逆 / 交互路径）。
# ★ 这是程序闸门，不只是注释承诺：即使有人误把任务名加进 AGNES_TASKS，
#   resolve() 与 call_params() 仍会把它按 deepseek 处理（测试钉死）。
# ★ triage 已于 2026-11-25 复核后移出（并入白名单），其余仍是复杂/长文任务。
AGNES_FORBIDDEN_TASKS = frozenset({
    "summary", "weekly", "wiki", "wiki_compile", "wiki_relate",
    "wiki_recommend", "conversation", "ingest", "grade", "classify",
})

# agnes 有 deepseek 整单兜底，失败要快：不让它吃满重试预算。
# translate：后台任务，2 次 × 30s + 1s 退避 ≈ 61s 即降级（ds 仍 3×90s）。
# triage：后台 scheduler，慢点无害，与翻译同档 2×30s（ds 仍按 triage 原 3×90s /
#   2×90s 补票，不跟着缩短）；限流时多花的只是免费侧的失败调用。
# wiki_related：审批浮层自动 POST 是交互路径，2 次 × 15s + 1s ≈ 31s 即降级
#   （ds 仍按 citeguard 的 2×180s；命中指纹缓存时零调用）。
AGNES_CALL_PARAMS = {
    "translate": {"retries": 2, "timeout": 30},
    "triage": {"retries": 2, "timeout": 30},
    "wiki_related": {"retries": 2, "timeout": 15},
}


def has_agnes_key() -> bool:
    """有没有可用的 agnes key（路由层短路用，省一次必然失败的无谓调用）。"""
    return bool(str(getattr(settings, "AGNES_API_KEY", "") or "").strip())


def resolve(task: str) -> str:
    """返回该任务该用的 provider 名：'agnes' 或 'deepseek'。

    task 是**路由键**（如 translate / triage / wiki），不是 LlmUsage.kind。
    判定顺序（短路，任一不满足即 deepseek）：
      1. 该任务在白名单里；
      2. 「优先 agnes」开关开了（按任务读 `<task>_prefer_agnes`）；
      3. 有合法 agnes key。
    """
    if task in AGNES_FORBIDDEN_TASKS or task not in AGNES_TASKS:
        return "deepseek"
    flag_key = f"{task}_prefer_agnes"
    # 未在 settingkit 注册表的任务 → 开关视为关（拼错 key 不该 500，也不该误切）。
    try:
        enabled = bool(sk.effective(flag_key))
    except KeyError:
        return "deepseek"
    if not enabled or not has_agnes_key():
        return "deepseek"
    return "agnes"


def call_params(task: str, provider: str) -> dict:
    """主通道的调用参数覆盖（只作用于 agnes 那一次，兜底通道不受影响）。

    ★ 返回空 dict = 调用方保持原参数（deepseek 仍是 3 次 × 90s）。
    ★ 复杂任务即使被误加进白名单，这里也返回空 —— 与 resolve() 同一道闸门。
    """
    if provider == "agnes" and task in AGNES_TASKS \
            and task not in AGNES_FORBIDDEN_TASKS:
        params = AGNES_CALL_PARAMS.get(task)
        return dict(params) if params else {}
    return {}
