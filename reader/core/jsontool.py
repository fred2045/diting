"""从模型输出里捞结构化数据（唯一实现）。

「模型输出不可信」的第一道防线：模型会给 JSON 加 markdown 围栏、在前后
裹说明文字、或把数组写成单个对象。此前 triage / translate / summary 各自
手抄一份「剥围栏 + 先试数组再试对象」，一处踩到新的包裹形态，其他处不会
跟着修 —— 同一类故障会在不同模块以不同面目反复出现。

· `strip_code_fence(text, *langs)` —— 剥整段代码围栏（```json … ``` /
  ```markdown … ```），只剥首尾各一道；
· `iter_json(text)` —— 依次产出**能解析**的 JSON 候选（先数组后对象），
  调用方对每个候选做自己的行级校验（校验不过可以继续要下一个）。
"""
import json
import re


def strip_code_fence(text: str, *langs: str) -> str:
    """剥掉整段的 markdown 代码围栏（```json … ``` / ```markdown … ```）。

    langs 留空 = 只剥无语言的围栏；传 "json" / "markdown", "md" 等按需组合。
    与原 triage/translate 的手写正则逐字同行为（含 `\\s*` 跨换行的边角）。
    """
    body = "|".join(re.escape(x) for x in langs if x)
    alt = f"(?:{body})?" if body else "?"
    return re.sub(rf"^```{alt}\s*|\s*```$", "", (text or "").strip(),
                  flags=re.I | re.M).strip()


def _decoded_at(text: str, opener: str):
    """从每个 opener 位置用 JSON 解码器读一个**完整**值，产出该值。

    ★ 不用非贪婪正则 `\\[[\\s\\S]*?\\]`：它会在嵌套数组的第一个 `]` 处截断，
      把合法 JSON 切成 `[{"a":[1]` 这种残片；`raw_decode` 自己数括号、认字符串转义。
    """
    dec = json.JSONDecoder()
    pos = 0
    while True:
        pos = text.find(opener, pos)
        if pos < 0:
            return
        try:
            obj, end = dec.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos += 1
            continue
        yield obj
        pos = max(end, pos + 1)


def iter_json(text: str):
    """按序产出可解析的 JSON 候选（先 `[...]` 后 `{...}`），没有了就结束。

    夹在说明文字里也能抓出来（`好的，结果如下：[...] 以上。`）；
    每种形态**先试贪婪整段**（保持「先数组后对象」的候选顺序，`next()` 语义不变），
    整段解析失败才逐段扫描 —— 救回「多段 JSON 夹说明」形态。
    ★ 2026-11-26 但斌 feed 一次批量分级输出 3123 token、HTTP 200 却零票：
      贪婪正则从第一个 `[` 吃到最后一个 `]` 整段吞掉，`json.loads` 失败即零票。
    """
    t = strip_code_fence(text, "json")
    for pat, opener in ((r"\[[\s\S]*\]", "["), (r"\{[\s\S]*\}", "{")):
        m = re.search(pat, t)
        parsed = False
        if m:
            try:
                yield json.loads(m.group(0))
                parsed = True
            except json.JSONDecodeError:
                pass
        if not parsed:
            for obj in _decoded_at(t, opener):
                yield obj
