"""关键词检索（BM25 的轻量近似）。

- PostgreSQL: 可用 tsvector/zhparser（预留），此处统一走应用层打分以保持跨库一致。
- SQLite/MySQL: 应用层对候选集做 BM25 打分。
为控制内存，先在 SQL 层用 LIKE 粗召回（可选），再 BM25 精排。
"""
from __future__ import annotations

import math
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk
from app.retrieval.vector_store.store import VectorHit, _apply_permission, _dialect_of
from app.services.permission import PermissionFilter, chunk_visible_py

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]+")

# 字符覆盖率下限：命中片段要覆盖查询里至少这么多「实义字符」才算相关。
# 为什么按字符而不是按词：中文没有空格，二元组必然跨词边界
# （「怎么退货」切出 `怎么/么退/退货`，`么退` 是伪词），按词算分母会虚高，
# 把「退货流程…」这种完全正常的命中判成不相关。
# 0.3 的取法（实测三档 0.3 / 0.35 / 0.4）：
# - 单字碰运气最多覆盖一两个字，长查询里远达不到 30%，误命中被挡掉；
# - 正常命中通常在 50% 以上（「报销流程是什么」→「报销流程：…」覆盖 8/12），
#   留出 30% 的余量才不会被问法措辞差异误杀（0.35 就会开始误杀）。
# 定成常量而非配置项：它是「有没有依据」的底线，不是调优旋钮；
# 真正想放宽/收紧的是 retrieval_bm25_min / retrieval_vec_min 那两个分数阈值。
MIN_COVERAGE = 0.3

# 至少命中一个「双字单元」：防止查询里的字恰好散落在片段各处
# （覆盖率够但没有任何连续命中），那种情况同样谈不上依据。


def tokenize(text: str) -> list[str]:
    """中文按双字（bigram）+ 单字，英文数字按词。零依赖，比单字召回更准。"""
    out: list[str] = []
    for seg in _TOKEN_RE.findall(text):
        if seg.isascii():
            out.append(seg.lower())
        else:
            out.append(seg)  # 整段中文也保留，命中长词
            for ch in seg:
                out.append(ch)  # 单字（短词/召回兜底）
            for i in range(len(seg) - 1):
                out.append(seg[i:i + 2])  # 双字
    return out


def informative_units(text: str) -> list[str]:
    """查询的「证据单元」：用于判断命中是否有实质依据。

    只取双字（中文）与多字母词（英文数字），**不取单字**。
    理由：单字在中文里几乎人人都含（「的」「面」「行」），
    拿它当依据等于没依据——实测「继续完善智能问答界面」会因一个「面」字
    命中「知识库运营面验证行」并被当成引用来源展示给用户。

    两个中文串之间的 bigram 会被完整覆盖（len-1 个），所以 `继续完善` 会产出
    `继续/续完/完善`，任一处落空都能被算进分母。

    若整句没有任何可切分单元（单个汉字、单字母），退回单字本身——
    此时用户确实只查了这一个字，不能判成「无依据」。
    """
    units: list[str] = []
    for seg in _TOKEN_RE.findall(text):
        if seg.isascii():
            if len(seg) >= 2:
                units.append(seg.lower())
        elif len(seg) >= 2:
            units.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    if units:
        return units
    # 兜底：没有双字单元时用单字/单字母，避免把「只查一个字」误判为无依据
    return [s.lower() if s.isascii() else s for s in _TOKEN_RE.findall(text)]


def min_hits_for(units: list[str], min_coverage: float = MIN_COVERAGE) -> int:
    """该查询至少要覆盖多少「字符」才算相关。

    按字符而不是单元数：二元组会跨词边界产生伪词（见 `informative_units`），
    用它当分母会低估正常命中的覆盖率。
    """
    total = sum(len(u) for u in units)
    if not total:
        return 0
    return max(1, math.ceil(total * min_coverage))


def _hit_units(units: list[str], text: str) -> int:
    """文本覆盖了多少个查询字符（同一字符不重复计）。

    这里要回答的是「这段话里到底提没提到查询在问的内容」，
    提到与否是布尔事实，出现几次不影响结论，所以用集合去重而不是数词频。
    先 lower() 再比，与 tokenize 的英文小写口径保持一致。
    """
    low = text.lower()
    covered: set[str] = set()
    for u in units:
        if u in low:
            covered.update(u)
    return len(covered)


def _coverage(units: list[str], text: str) -> float:
    """查询字符覆盖率（0~1）。"""
    total = sum(len(u) for u in units)
    if not total:
        return 1.0
    return _hit_units(units, text) / total


def _has_evidence(units: list[str], need: int, bigrams: list[str], text: str) -> bool:
    """判据本体：覆盖率达标 **且** 至少一个双字组整体出现。

    只看覆盖率时，查询里的字可能恰好散落各处（「继续完善智能问答界面」的
    「面」与「智」分散在无关段落里），那种命中同样没有依据。

    单独抽出来是为了让「召回处（全库打分）」和「外部调用（逐条判断）」
    用同一个实现——判据一旦有两份，就会漂移成"BM25 挡住了、向量没挡"。
    """
    low = text.lower()
    if _hit_units(units, low) < need:
        return False
    return not bigrams or any(b in low for b in bigrams)


def has_lexical_evidence(query: str, text: str, *, min_coverage: float = MIN_COVERAGE) -> bool:
    """查询在文本里是否有**词面依据**（与 keyword_search 同一套判据）。

    为什么把判据抽出来公开：它原本只长在关键词召回里，于是同样的查询
    「打开必应并搜索吕不韦」——BM25 通路会因为覆盖率不足挡掉无关文档，
    向量通路却照样把「后续会开发企业内部聊天模块」送进 top_k（余弦不是 0，
    只说明"共享了几个字"）。两条通路判据不一致，用户看到的就是
    「问什么都挂着两份开发文档当参考资料」。

    空查询、或查询里没有可切分的双字单元（单个汉字/单字母）时返回 True：
    此时判据无从施加，不能一律判成"无依据"。
    """
    units = informative_units(query)
    if not units:
        return True
    need = min_hits_for(units, min_coverage)
    if need <= 0:
        return True
    return _has_evidence(units, need, [u for u in units if len(u) >= 2], text)


class BM25:
    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75,
                 counters: list[Counter] | None = None) -> None:
        self.k1 = k1
        self.b = b
        self.docs = corpus_tokens
        self.n = len(corpus_tokens)
        self.avgdl = (sum(len(d) for d in corpus_tokens) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for d in corpus_tokens:
            for term in set(d):
                self.df[term] += 1
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in self.df.items()
        }
        # 预存每行词频 Counter（避免每次打分重建）
        self.counters = counters if counters is not None else [Counter(d) for d in corpus_tokens]

    def score(self, query_tokens: list[str], idx: int) -> float:
        tf = self.counters[idx]
        dl = len(self.docs[idx]) or 1
        s = 0.0
        for term in query_tokens:
            freq = tf.get(term)
            if not freq:
                continue
            idf = self.idf.get(term, 0.0)
            s += idf * (freq * (self.k1 + 1)) / (freq + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
        return s


def build_index(rows: list[tuple]) -> BM25:
    """由行数据（含 content 在 r[3]）构建可复用的 BM25 索引。"""
    corpus = [tokenize(r[3] or "") for r in rows]
    return BM25(corpus)


async def keyword_search(
    db: AsyncSession, *, query: str, pf: PermissionFilter, top_k: int,
    index: BM25 | None = None, rows: list[tuple] | None = None,
    on_build: "callable | None" = None,
    min_coverage: float = MIN_COVERAGE,
) -> list[VectorHit]:
    """关键词检索。传入预构建的 index+rows（缓存）可避免每次全库重分词。

    on_build(index, rows)：本次**新建**了索引时回调，供调用方写入缓存。

    min_coverage：命中片段需覆盖查询字符的比例下限（见 `MIN_COVERAGE`）。
    BM25 > 0 只说明有共同词，中文里往往只是一个单字碰上了，这类命中排序可能靠前
    （短文档 BM25 归一化后分数不低），但没有任何回答依据——必须在召回处挡掉，
    而不是等阈值参数（默认 0 = 不过滤）来兜。
    传 0 表示关闭依据判据，退回纯 BM25 分数（仅供确实需要宽召回的库使用）。
    """
    if index is None or rows is None:
        dialect = _dialect_of(db)
        stmt = select(
            Chunk.id, Chunk.doc_id, Chunk.kb_id, Chunk.content, Chunk.page,
            Chunk.parent_content, Chunk.section,
            Chunk.vis_scope, Chunk.acl_allow, Chunk.acl_deny,
        )
        stmt = _apply_permission(stmt, pf, dialect)
        rows = (await db.execute(stmt)).all()
        if not rows:
            return []
        # 纵深防御：Python 再校验一次可见性
        pset = set(pf.principals)
        rows = [r for r in rows if chunk_visible_py(pset, r[7], r[8], r[9])]
        index = build_index(rows)
        if on_build is not None:
            try:
                on_build(index, rows)
            except Exception:  # noqa: BLE001
                pass

    if not rows:
        return []

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    units = informative_units(query)
    # 依据判据：覆盖率 + 至少一个连续命中。min_coverage=0 时整体关闭，
    # 退回「BM25 分数 > 0 即召回」的旧行为（部分库确实需要宽召回）。
    check_evidence = min_coverage > 0 and bool(units)
    need = min_hits_for(units, min_coverage) if check_evidence else 0
    bigrams = [u for u in units if len(u) >= 2] if check_evidence else []

    scored: list[tuple[float, tuple]] = []
    for i, row in enumerate(rows):
        sc = index.score(query_tokens, i)
        if sc <= 0:
            continue
        content = row[3] or ""
        if check_evidence and not _has_evidence(units, need, bigrams, content):
            continue
        scored.append((sc, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    # 归一化到 0-1（BM25 原始分保留在 raw_score）
    max_s = scored[0][0] if scored else 1.0
    hits: list[VectorHit] = []
    for sc, row in scored[:top_k]:
        hits.append(
            VectorHit(
                chunk_id=row[0], doc_id=row[1], kb_id=row[2], content=row[3],
                score=sc / max_s if max_s else 0.0, raw_score=sc, page=row[4],
                parent_content=row[5], section=row[6], source="bm25",
            )
        )
    return hits
