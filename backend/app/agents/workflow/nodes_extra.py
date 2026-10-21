"""工作流扩展节点执行器（电商 / 办公场景）。

与 engine.py 的内置节点同签名：`async def exec_xxx(node, data, ctx) -> dict`，
返回的 dict 会写入 variables[node_id]，下游用 `{{node_id.xxx}}` 引用。

设计要点（勿凭直觉改动）：

1. **节点只做纯数据变换，不直接读写业务表**。电商/办公场景的真实数据源
   通常已经通过 HTTP 节点或知识库接入；节点负责的是「取到的数据怎么处理」。
   这样节点不绑定任何具体业务 schema，换行业也能用。

2. **输出必须包含 `output` 字段**。工作流的变量引用规约是 `[node_id].output`，
   所有扩展节点都保证有它，避免用户连线后引用不到。

3. **严格失败**：处理不了就抛 ValidationError，不返回半成品。
   工作流编排里静默降级比报错更难排查（engine 有 retry 机制兜底）。

4. **限制规模**：表格/数据集都设行数上限，防止一份大 Excel 撑爆内存。
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from app.core.errors import ValidationError

# 表格类节点的行数上限（超出直接报错，不静默截断）
MAX_ROWS = 5000


# ============================================================ 工具函数


def _as_list(v: Any, field: str = "items") -> list:
    """把输入规范成 list：接受真 list、JSON 字符串、换行分隔文本、单值。

    工作流变量常以字符串形式流转（LLM 输出、HTTP 响应），这里统一兜底。
    """
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    if isinstance(v, (int, float, bool)):
        return [v]
    s = str(v).strip()
    if not s:
        return []
    if s[0] in "[{":
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    # 换行分隔（CSV 文本或 LLM 输出的列表）
    return [line for line in s.splitlines() if line.strip()]


def _row_get(row: Any, path: str) -> Any:
    """按点路径取值：`a.b.0.c`。缺失返回 None（不抛错，便于表格容错）。"""
    cur = row
    for seg in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _pick_fields(rows: list, fields: list[dict]) -> list[dict]:
    """按字段映射把行投影成表：fields = [{name, from}]。

    from 支持点路径；不填则用同名键。取不到值时写空字符串（保持列对齐）。
    """
    specs = [(str(f.get("name") or ""), str(f.get("from") or f.get("name") or "")) for f in fields]
    specs = [(n, src) for n, src in specs if n]
    if not specs:
        # 没配字段映射：保留原对象
        return [r if isinstance(r, dict) else {"value": r} for r in rows]
    out = []
    for r in rows:
        item = {}
        for name, src in specs:
            v = _row_get(r, src)
            item[name] = "" if v is None else v
        out.append(item)
    return out


def _to_csv(rows: list[dict], columns: list[str] | None = None) -> str:
    if not rows:
        return ""
    cols = columns or list(rows[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in cols})
    return buf.getvalue()


def _agg(values: list, op: str) -> Any:
    """聚合：对可能是字符串的数字做宽松转换。"""
    nums: list[float] = []
    for v in values:
        if v is None or v == "":
            continue
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if op == "count":
        return len(values)
    if op == "count_non_empty":
        return len(nums)
    if op == "sum":
        return sum(nums)
    if op == "avg":
        return (sum(nums) / len(nums)) if nums else 0
    if op == "min":
        return min(nums) if nums else None
    if op == "max":
        return max(nums) if nums else None
    if op == "distinct":
        seen: list = []
        for v in values:
            if v not in seen:
                seen.append(v)
        return len(seen)
    # join / first / last
    if op == "join":
        return ", ".join(str(v) for v in values if v not in (None, ""))
    if op == "first":
        return values[0] if values else None
    if op == "last":
        return values[-1] if values else None
    raise ValidationError(f"未知聚合方式: {op}")


# ============================================================ 表格 / 电商


async def _exec_table(node: dict, data: dict, ctx) -> dict:
    """表格处理：把行数据做过滤 / 排序 / 聚合 / 投影 / 转 CSV。

    电商典型用法：订单列表 → 过滤未付款 → 按金额排序 → 汇总总额。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    rows = _as_list(data.get("rows") if "rows" in data else d.get("rows"))
    if not isinstance(rows, list):
        rows = []
    if len(rows) > MAX_ROWS:
        raise ValidationError(f"表格行数 {len(rows)} 超过上限 {MAX_ROWS}")

    # 1) 过滤：{field, op, value} 列表，全部满足（AND）
    for flt in d.get("filters") or []:
        if not isinstance(flt, dict):
            continue
        field = str(flt.get("field") or "")
        op = str(flt.get("op") or "eq")
        target = flt.get("value")
        if not field:
            continue
        kept = []
        for r in rows:
            v = _row_get(r, field)
            try:
                if op == "eq":
                    ok = str(v) == str(target)
                elif op == "ne":
                    ok = str(v) != str(target)
                elif op == "contains":
                    ok = str(target) in str(v)
                elif op == "gt":
                    ok = float(v) > float(target)
                elif op == "lt":
                    ok = float(v) < float(target)
                elif op == "not_empty":
                    ok = v not in (None, "", [])
                else:
                    ok = True
            except (TypeError, ValueError):
                ok = False
            if ok:
                kept.append(r)
        rows = kept

    # 2) 排序
    sort = d.get("sort") or {}
    if isinstance(sort, dict) and sort.get("field"):
        field = str(sort["field"])
        reverse = bool(sort.get("desc"))

        def _key(r):
            v = _row_get(r, field)
            # 数字优先按数值排，其余按字符串（避免 "10" < "9"）
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, str(v))

        rows = sorted(rows, key=_key, reverse=reverse)

    # 3) 字段投影
    fields = d.get("fields") or []
    rows = _pick_fields(rows, fields) if fields else [
        r if isinstance(r, dict) else {"value": r} for r in rows
    ]

    # 4) 去重
    if d.get("distinct") and rows:
        key_field = str(d.get("distinct_field") or "")
        seen_keys = set()
        uniq = []
        for r in rows:
            k = _row_get(r, key_field) if key_field else json.dumps(r, sort_keys=True, ensure_ascii=False, default=str)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            uniq.append(r)
        rows = uniq

    # 5) 限制行数
    limit = d.get("limit")
    if limit:
        rows = rows[: int(limit)]

    out: dict = {
        "rows": rows,
        "count": len(rows),
        "output": _to_csv(rows) if d.get("as_csv", True) else json.dumps(rows, ensure_ascii=False),
    }
    if d.get("as_csv", True):
        out["csv"] = out["output"]

    # 6) 聚合
    aggs = d.get("aggregates") or []
    if aggs:
        summary = {}
        for a in aggs:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "")
            field = str(a.get("field") or "")
            op = str(a.get("op") or "count")
            if not name:
                continue
            vals = [_row_get(r, field) for r in rows] if field else rows
            summary[name] = _agg(vals, op)
        out["summary"] = summary
    return out


# ============================================================ 数据映射 / 编排


async def _exec_mapping(node: dict, data: dict, ctx) -> dict:
    """数据映射：把上游输出按模板重塑成目标结构（对接第三方 API 的刚需）。

    expression 支持 `{{start.query}}` 这类引用，由 engine 预先渲染；
    这里只负责把映射结果组装成对象。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    result: dict = {}
    for m in d.get("mappings") or []:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or "").strip()
        if not key:
            continue
        result[key] = m.get("value")
    # 支持直接把上游对象透传并覆盖部分字段
    base = d.get("base")
    if isinstance(base, dict):
        merged = {**base, **result}
    else:
        merged = result
    # 同时提供扁平字段：用户会自然写 {{m.orderNo}}，而不是 {{m.data.orderNo}}。
    # 扁平字段放在 data/output 之后不冲突（若用户定义的同名列已占位则保留原值）。
    out: dict[str, Any] = {"data": merged, "output": json.dumps(merged, ensure_ascii=False)}
    for k, v in merged.items():
        if k not in out:
            out[k] = v
    return out


async def _exec_json_path(node: dict, data: dict, ctx) -> dict:
    """JSON 取值：从上游输出里按路径取字段（HTTP API 响应解析）。"""
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    source = data.get("source")
    if source is None:
        source = d.get("source")
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (json.JSONDecodeError, ValueError):
            pass
    path = str(d.get("path") or "").strip()
    val = _row_get(source, path) if path else source
    # 输出统一成字符串或结构化对象
    return {
        "value": val,
        "output": val if isinstance(val, (dict, list)) else ("" if val is None else str(val)),
    }


# ============================================================ 办公场景


async def _exec_text_template(node: dict, data: dict, ctx) -> dict:
    """文本模板：把多个变量拼成一段文本（生成邮件/报告/消息的正文）。

    engine 已对非 deferred 字段做过渲染，这里只需处理 section 拼接。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    parts: list[str] = []
    for sec in d.get("sections") or []:
        if isinstance(sec, str):
            parts.append(sec)
            continue
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "")
        body = sec.get("content")
        body = "" if body is None else str(body)
        if title:
            parts.append(f"{title}\n{body}")
        else:
            parts.append(body)
    if not parts:
        parts = [str(d.get("template") or "")]
    text = "\n\n".join(p for p in parts if p != "")
    return {"text": text, "output": text}


async def _exec_email(node: dict, data: dict, ctx) -> dict:
    """发送邮件（办公审批 / 报表推送）。

    复用 mail_service.send_from_account；未配置发送账号时报错而不是静默跳过。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    to = str(d.get("to") or "").strip()
    subject = str(d.get("subject") or "").strip()
    body = data.get("body") or d.get("body") or ""
    if not to:
        raise ValidationError("邮件节点缺少收件人")
    if not subject:
        raise ValidationError("邮件节点缺少主题")

    account_id = d.get("account_id")
    if not account_id:
        raise ValidationError("邮件节点未指定发送账号（请在属性面板选择个人邮箱账号）")

    from sqlalchemy import select

    from app.models import MailAccount
    from app.services.mail_service import send_from_account

    acc = (await ctx.db.execute(
        select(MailAccount).where(
            MailAccount.id == int(account_id), MailAccount.tenant_id == ctx.tenant_id
        )
    )).scalars().first()
    if not acc:
        raise ValidationError("发送账号不存在或不属于当前租户")

    addrs = [x.strip() for x in re.split(r"[,;]", to) if x.strip()]
    result = await send_from_account(
        ctx.db, acc, to=addrs, subject=subject,
        body_text=str(body), body_html=str(d["html_body"]) if d.get("html_body") else None,
    )
    if not result.get("ok"):
        raise ValidationError(f"邮件发送失败：{result.get('message') or '未知错误'}")
    return {"ok": True, "output": f"已发送邮件给 {to}", "result": result}


async def _exec_http_form(node: dict, data: dict, ctx) -> dict:
    """表单提交：application/x-www-form-urlencoded 或 multipart（电商下单/回填）。

    与 http 节点分开是因为表单编码、字段渲染规则都不同，混在一起容易配错。
    """
    import httpx

    from app.agents.tools.builtin import _check_url

    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    url = str(data.get("url") or d.get("url") or "")
    if not url:
        raise ValidationError("表单提交缺少 URL")
    err = _check_url(url, d.get("allow_hosts"))
    if err:
        raise ValidationError(f"表单提交被拒绝: {err}")

    fields = d.get("fields") or []
    form: dict[str, str] = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if name:
            form[name] = "" if f.get("value") is None else str(f.get("value"))

    timeout = httpx.Timeout(connect=10.0, read=float(d.get("timeout") or 30), write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        resp = await c.request(
            str(d.get("method") or "POST").upper(), url,
            data=form if d.get("encoding") != "json" else None,
            json=form if d.get("encoding") == "json" else None,
            headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()} or None,
        )
    if resp.status_code >= 400:
        raise ValidationError(f"表单提交失败 HTTP {resp.status_code}: {resp.text[:300]}")
    return {"output": resp.text[:65536], "status": resp.status_code, "ok": True}


async def _exec_batch(node: dict, data: dict, ctx) -> dict:
    """批量分批：把大列表切成 N 批，便于分页调用第三方接口（电商批量查单）。

    与 loop 的区别：loop 是逐项处理，batch 是分组，通常后接 loop 使用。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    items = _as_list(data.get("items") if "items" in data else d.get("items"))
    size = max(1, min(int(d.get("size") or 10), 500))
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    return {
        "batches": batches,
        "count": len(batches),
        "total": len(items),
        "output": json.dumps(batches, ensure_ascii=False),
    }


async def _exec_transform(node: dict, data: dict, ctx) -> dict:
    """数据转换：列表 ↔ 文本、中文数字提取、金额解析等常用清洗。

    电商场景的高频痛点：从一段自然语言里抽出订单号/金额/数量。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    val = data.get("value") if "value" in data else d.get("value")
    op = str(d.get("op") or "to_text")

    if op == "to_text":
        if isinstance(val, str):
            out = val
        elif isinstance(val, (list, dict)):
            out = json.dumps(val, ensure_ascii=False, indent=2)
        else:
            out = "" if val is None else str(val)
        return {"output": out, "value": out}

    if op == "split":
        sep = str(d.get("separator") or "\n")
        out = [x.strip() for x in str(val or "").split(sep) if x.strip()]
        return {"output": out, "value": out, "count": len(out)}

    if op == "join":
        sep = str(d.get("separator") or ",")
        out = sep.join(str(x) for x in _as_list(val))
        return {"output": out, "value": out}

    if op == "extract":
        # 正则提取，返回所有匹配（可指定 group）
        pattern = str(d.get("pattern") or "")
        if not pattern:
            raise ValidationError("提取转换缺少正则表达式")
        try:
            rgx = re.compile(pattern)
        except re.error as e:
            raise ValidationError(f"正则表达式无效: {e}")
        gi = int(d.get("group") or 0)
        found = []
        for m in rgx.finditer(str(val or "")):
            try:
                found.append(m.group(gi))
            except IndexError:
                found.append(m.group(0))
        out = found[0] if (len(found) == 1 and not d.get("all")) else found
        return {"output": out, "value": out, "count": len(found)}

    if op == "to_number":
        # 从文本里取第一个数字（支持千分位与货币符号）
        s = str(val or "").replace(",", "").replace("￥", "").replace("¥", "").replace("$", "")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        out = float(m.group(0)) if m else None
        return {"output": out, "value": out}

    raise ValidationError(f"未知转换方式: {op}")


async def _exec_collection(node: dict, data: dict, ctx) -> dict:
    """集合运算：合并 / 去重 / 差集 / 交集（电商多来源数据归并）。"""
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    op = str(d.get("op") or "merge")
    lists = d.get("lists") or []
    normalized: list[list] = [_as_list(x) for x in lists]

    def _k(v):
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)

    if op == "merge":
        out = [x for lst in normalized for x in lst]
    elif op == "union":
        seen, out = set(), []
        for lst in normalized:
            for x in lst:
                k = _k(x)
                if k not in seen:
                    seen.add(k)
                    out.append(x)
    elif op == "intersect":
        if not normalized:
            out = []
        else:
            sets = [{_k(x) for x in lst} for lst in normalized]
            common = set.intersection(*sets) if sets else set()
            seen, out = set(), []
            for x in normalized[0]:
                k = _k(x)
                if k in common and k not in seen:
                    seen.add(k)
                    out.append(x)
    elif op == "diff":
        if len(normalized) < 2:
            out = normalized[0] if normalized else []
        else:
            rest = {_k(x) for lst in normalized[1:] for x in lst}
            out = [x for x in normalized[0] if _k(x) not in rest]
    else:
        raise ValidationError(f"未知集合操作: {op}")

    return {"items": out, "count": len(out), "output": json.dumps(out, ensure_ascii=False, default=str)}


async def _exec_summarize(node: dict, data: dict, ctx) -> dict:
    """长文本摘要 / 结构化抽取：复用 LLM，但用统一提示词避免每次手写。

    与 llm 节点的区别：这里强制「只基于给定文本」并限定输出格式，
    适合电商评价汇总、工单归类、报表摘要。
    """
    from app.providers.base import ChatMessage
    from app.providers.registry import get_llm

    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    source = data.get("text") if "text" in data else d.get("text")
    source = str(source or "")
    if not source.strip():
        raise ValidationError("摘要节点没有输入文本")

    mode = str(d.get("mode") or "summary")
    style = {
        "summary": "用简洁的中文要点总结下面的内容，不超过 5 条，每条一行。",
        "extract": "从下面的内容中提取关键信息，以 JSON 对象输出，只输出 JSON 本身。",
        "classify": "判断下面内容属于哪个类别，只输出类别名称。",
        "sentiment": "判断下面内容的情感倾向，只输出「正面」「中性」「负面」之一。",
    }.get(mode, "用简洁的中文要点总结下面的内容。")
    if d.get("instruction"):
        style = str(d["instruction"])

    system = str(d.get("system") or ctx.default_system or style)
    model_config_id = d.get("model_config_id") or ctx.default_model_config_id
    llm, rm = await get_llm(ctx.db, tenant_id=ctx.tenant_id, config_id=model_config_id)
    msgs = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=f"{style}\n\n---\n{source[:20000]}"),
    ]
    temp = d.get("temperature")
    if temp is None:
        temp = 0.1
    res = await llm.chat(msgs, model=rm.model_name, temperature=float(temp))
    ctx.add_usage(res.usage)
    return {"output": res.content, "usage": res.usage, "mode": mode}


async def _exec_router(node: dict, data: dict, ctx) -> dict:
    """路由表：按「分类结果」把流程分发到不同分支（比 switch 更适合 AI 分类输出）。

    cases 形如 [{"handle": "售后", "values": ["退货", "换货", "退款"]}]，
    匹配方式为「分类值包含其中任一项」。
    """
    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    value = data.get("value") if "value" in data else d.get("value")
    text = str(value or "").strip()
    for i, c in enumerate(d.get("cases") or []):
        if not isinstance(c, dict):
            continue
        handle = str(c.get("handle") or f"case{i+1}")
        values = c.get("values") or []
        if isinstance(values, str):
            values = [v.strip() for v in values.split(",") if v.strip()]
        if any(str(v) and str(v) in text for v in values):
            return {"branch": handle, "matched": handle, "value": text}
    return {"branch": "default", "matched": None, "value": text}


async def _exec_webhook(node: dict, data: dict, ctx) -> dict:
    """出站 Webhook：把工作流结果回调给外部系统（电商 ERP / OA）。

    与 http 节点的区别：自动带 JSON 包装、失败重试由节点 retry 统一处理、
    并且强制 POST。适合「处理完结果通知外部」这类固定形态。
    """
    import httpx

    from app.agents.tools.builtin import _check_url

    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    url = str(data.get("url") or d.get("url") or "")
    if not url:
        raise ValidationError("Webhook 节点缺少 URL")
    err = _check_url(url, d.get("allow_hosts"))
    if err:
        raise ValidationError(f"Webhook 被拒绝: {err}")

    payload: dict = {}
    for f in d.get("fields") or []:
        if isinstance(f, dict) and f.get("name"):
            payload[str(f["name"])] = f.get("value")
    if not payload:
        payload = {"text": str(data.get("body") or d.get("body") or "")}

    headers = {str(k): str(v) for k, v in (d.get("headers") or {}).items()}
    timeout = httpx.Timeout(connect=10.0, read=float(d.get("timeout") or 30), write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        resp = await c.post(url, json=payload, headers=headers or None)
    if resp.status_code >= 400:
        raise ValidationError(f"Webhook 返回 HTTP {resp.status_code}: {resp.text[:300]}")
    return {"ok": True, "status": resp.status_code, "output": resp.text[:8192]}


async def _exec_delay(node: dict, data: dict, ctx) -> dict:
    """延时等待：限流、错峰、等待外部系统状态生效。

    上限 300 秒 —— 工作流是同步执行链，长等待会占住执行器。
    """
    import asyncio

    # data 已被 engine 渲染（{{var}} 已替换）；node["data"] 是原始模板，仅在缺失时回退
    d = data if isinstance(data, dict) and data else (node.get("data") or {})
    seconds = min(max(float(d.get("seconds") or 1), 0), 300)
    await asyncio.sleep(seconds)
    return {"output": f"已等待 {seconds} 秒", "seconds": seconds}


# ============================================================ 执行器注册表

EXTRA_EXECUTORS: dict[str, Any] = {
    # 数据处理
    "table": _exec_table,
    "mapping": _exec_mapping,
    "json_path": _exec_json_path,
    "transform": _exec_transform,
    "collection": _exec_collection,
    "batch": _exec_batch,
    # 办公
    "text_template": _exec_text_template,
    "email": _exec_email,
    "summarize": _exec_summarize,
    "router": _exec_router,
    # 集成
    "http_form": _exec_http_form,
    "webhook": _exec_webhook,
    "delay": _exec_delay,
}

# 扩展节点的分组与展示名（前端节点面板分类用；与 NODE_META 的 label/color 互补）
NODE_SPECS_EXTRA: dict[str, dict[str, str]] = {
    "table": {"group": "数据处理", "label": "表格处理", "icon": "▦", "color": "#08979c"},
    "mapping": {"group": "数据处理", "label": "数据映射", "icon": "⇄", "color": "#08979c"},
    "json_path": {"group": "数据处理", "label": "JSON 取值", "icon": "🔍", "color": "#08979c"},
    "transform": {"group": "数据处理", "label": "数据清洗", "icon": "✂", "color": "#08979c"},
    "collection": {"group": "数据处理", "label": "集合运算", "icon": "∪", "color": "#08979c"},
    "batch": {"group": "数据处理", "label": "分批", "icon": "▤", "color": "#08979c"},
    "text_template": {"group": "办公", "label": "文本模板", "icon": "📝", "color": "#d46b08"},
    "email": {"group": "办公", "label": "发送邮件", "icon": "✉", "color": "#d46b08"},
    "summarize": {"group": "办公", "label": "摘要抽取", "icon": "𖣯", "color": "#722ed1"},
    "router": {"group": "办公", "label": "分类路由", "icon": "⑃", "color": "#fa8c16"},
    "http_form": {"group": "集成", "label": "表单提交", "icon": "🧾", "color": "#13c2c2"},
    "webhook": {"group": "集成", "label": "出站 Webhook", "icon": "🔔", "color": "#13c2c2"},
    "delay": {"group": "集成", "label": "延时", "icon": "⏱", "color": "#8c8c8c"},
}
