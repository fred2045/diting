"""设置页（v2）：分组渲染 settingkit 注册表 + 保存/重置 + 全局层文件编辑。

边界（2026-10-26 开源决策）：
  - **不收密钥**（LLM key 仍在 .env，BYOK 非必要）；
  - 预算设置留在 /costs 页（它跟开销数据长在一起，不放这里）；
  - 注册表（core/settingkit.py GROUPS）是键值类设置的唯一事实来源 —— 本页只负责
    渲染与提交，加设置项 = 改注册表，本页自动跟上。

★ v2 新增「全局层文件」分组（2026-10-31，wiki 阶段 0 的 7.3）：
  `interest-profile.md`（读者画像）与 `ontology.md`（知识本体）是**整篇 markdown
  的配置文件，不是键值**，所以不走 settingkit（那边限定 ≤120 字符、走 AppSetting 表），
  单独一组 + 单独一个保存端点，直接写回 `Meta/*.md`。
  两个文件都是「每人一份、可手改、git 可追溯」的东西：放本地文件而不是数据库，
  用户能直接 vim、能 diff、能备份 —— 这是刻意的。
"""
import logging
import os
import shutil
from datetime import datetime

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core import notify
from core import settingkit as sk
from core.models import AppSetting

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ 全局层文件
#: ★ 白名单写死，**不接受前端传路径**：只认 key。一旦路径可由前端拼就是任意文件
#:   读写 —— 本地工具也不能免这个。
#: ★ 两个文件进模型的方式不同：画像**全文**进分级/日报 prompt；本体只有「领域」表
#:   被 `wiki.ontology_domains()` 解析（其余文字只喂 `ontology_hash()`，无人调用）。
#:   别照「都进 prompt」写注释，否则等于承诺一段不会生效的配置。
#: ★ 「不进 settingkit」的理由见模块 docstring：字数量级差两个数量级（120 字符 vs 数千字）。
GLOBAL_FILES = [
    {
        "key": "profile",
        "label": "读者画像",
        "file": "interest-profile.md",
        "example": "interest-profile.example.md",
        "help": "回答「读者是谁、什么对他有价值」，用于分级与日报札记。"
                "★ 不要写 L0–L3 判据（会被拼进每一个源的 prompt）。",
        "hint": "建议 ≤ 2000 字（分级侧按 2000 截断）",
    },
    {
        "key": "ontology",
        "label": "知识本体",
        "file": "ontology.md",
        "example": "ontology.example.md",
        "help": "回答「知识分哪几个域」：表格里的受控 domain 标签决定每页落在 wiki/"
                "的哪个子目录，也是模型唯一可选的范围。"
                "★ 只写领域表；概念聚类用页里的 tags，不在这里维护。",
        "hint": "改标签 = 改分类法；tags 是自由的，不受本文件约束",
    },
]
BY_FILE_KEY = {f["key"]: f for f in GLOBAL_FILES}

#: 每次保存前留一份历史（防误覆盖）。保留份数不宜大：这是救急，不是版本管理。
HISTORY_KEEP = 10


def _meta_dir():
    return settings.PROJECT_ROOT / "Meta"


def _global_file_rows() -> list:
    """渲染结构：当前内容 + 是否存在 + 字节数。缺失时给 cp 命令而不是空白。"""
    rows = []
    for spec in GLOBAL_FILES:
        p = _meta_dir() / spec["file"]
        try:
            text = p.read_text(encoding="utf-8")
            exists = True
        except OSError:
            text, exists = "", False
        rows.append({
            **spec,
            "text": text,
            "exists": exists,
            "chars": len(text),
            "path": str(p),
            "cp_cmd": f"cp Meta/{spec['example']} Meta/{spec['file']}",
        })
    return rows


def _backup(p) -> str:
    """把**旧内容**存进 Meta/.history/，返回相对路径（没有旧文件则空串）。"""
    try:
        old = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not old.strip():
        return ""
    hist = _meta_dir() / ".history"
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = hist / f"{p.stem}-{stamp}.md"
    try:
        dst.write_text(old, encoding="utf-8")
    except OSError:
        log.warning("全局层文件备份失败：%s", dst)
        return ""
    # 只留最近 N 份（按修改时间），多了删旧的
    try:
        olds = sorted(hist.glob(f"{p.stem}-*.md"),
                      key=lambda x: x.stat().st_mtime, reverse=True)
        for f in olds[HISTORY_KEEP:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass
    return str(dst.relative_to(settings.PROJECT_ROOT))


def _rows():
    """注册表 → 渲染结构。每项带当前生效值 + 是否已被设置页覆盖。"""
    overridden = set(AppSetting.objects.values_list("key", flat=True))
    groups = []
    for g in sk.GROUPS:
        fields = []
        for f in g.fields:
            fields.append({
                "f": f,
                "value": sk.effective(f.key),
                "overridden": f.key in overridden,
            })
        groups.append({"g": g, "fields": fields})
    return groups


def _ctx(request):
    """渲染上下文。active = 默认展开哪个分组（支持 ?g=<key> 深链）。"""
    want = (request.GET.get("g") or "").strip()
    keys = [g.key for g in sk.GROUPS] + ["globals"]
    active = want if want in keys else keys[0]
    # 通知组的「未安装提示」：按后端区分，如实告知，不替用户装。
    # ★ 直接复用 core.notify 的 _backend()/_ready()，别再手写一份平台判断 ——
    #   mac 依赖 terminal-notifier（brew 装）；win 依赖随包 shim + pip 装的
    #   winotify（requirements.txt 已按 win32 标记自动带上）。
    if notify._backend() == "win":
        notify_ctx = {
            "bin": "winotify",
            "installed": notify._ready(),
            "hint": "pip install winotify（装完需重启 reader 进程）",
        }
    else:
        notify_bin = (getattr(settings, "NOTIFY_BIN", "terminal-notifier")
                      or "terminal-notifier")
        notify_ctx = {
            "bin": notify_bin,
            "installed": bool(shutil.which(notify_bin)),
            "hint": "brew install terminal-notifier",
        }
    return {
        "groups": _rows(),
        "files": _global_file_rows(),
        "active": active,
        "notify": notify_ctx,
    }


def index(request):
    """★ 同一份面板，两种外壳：带 HX-Request（openModal 打开）返回模态片段，
    直接访问 URL 返回整页兜底（书签 / 刷新不会掉进一个没有样式的裸片段）。"""
    if request.headers.get("HX-Request") == "true":
        return render(request, "prefs/_panel.html", _ctx(request))
    return render(request, "prefs/index.html", _ctx(request))


@require_POST
def save(request):
    key = (request.POST.get("key") or "").strip()
    if not key:
        return JsonResponse({"ok": False, "err": "缺少 key"})
    try:
        ok, err = sk.write(key, request.POST.get("value", ""))
    except KeyError:
        return JsonResponse({"ok": False, "err": "未知设置项"})
    if not ok:
        log.info("设置保存被拒：%s（%s）", key, err)
    # ★ 回带生效值：模态里不能 reload（重载会关掉弹窗），前端拿它回填输入框
    return JsonResponse({"ok": ok, "err": err, "value": sk.effective(key)})


@require_POST
def reset(request):
    key = (request.POST.get("key") or "").strip()
    if not key:
        return JsonResponse({"ok": False, "err": "缺少 key"})
    try:
        sk.reset(key)
    except KeyError:
        return JsonResponse({"ok": False, "err": "未知设置项"})
    return JsonResponse({"ok": True, "err": "", "value": sk.effective(key)})


@require_POST
def save_file(request):
    """保存全局层文件（画像 / 本体）。返回 {ok, err, chars, backup}。

    ★ 空内容一律拒绝：这里是「清空全局层」—— 画像空了分级就失去依据、本体空了
      编译就没有骨架。而「误提交」（textarea 没加载出来就点了保存 / 复制漏了）
      比「真想清空」常见得多；真要清空，直接删文件即可。
    ★ 先备份再原子替换：`os.replace` 保证要么旧版要么新版，不会留半截文件
      （半截本体喂给编译 = 结构被截断，比报错难查得多）。
    """
    key = (request.POST.get("key") or "").strip()
    spec = BY_FILE_KEY.get(key)
    if not spec:
        return JsonResponse({"ok": False, "err": "未知文件"})
    text = request.POST.get("value", "")
    if not text.strip():
        return JsonResponse({"ok": False, "err": "内容为空，已拒绝保存（要清空请直接删文件）"})

    # 统一成 LF + 单个结尾换行：编辑器/curl 都可能带来 CRLF 或没有结尾换行，
    # 让文件内容只取决于「你写了什么」，不取决于用什么工具提交的。
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"

    p = _meta_dir() / spec["file"]
    p.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(p)
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:  # noqa: BLE001  磁盘满 / 权限：如实回报，不假装成功
        log.warning("全局层文件写入失败：%s（%s）", p, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return JsonResponse({"ok": False, "err": f"写入失败：{e}"})

    log.info("全局层文件已保存：%s（%d 字，备份=%s）", p.name, len(text), backup or "无")
    return JsonResponse({"ok": True, "err": "", "chars": len(text), "backup": backup})
