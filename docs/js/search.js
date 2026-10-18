/* 本地简化的混合检索：中文二元组 + 英文分词，BM25 与向量余弦为演示算法，非真实模型输出 */
window.SEARCH = (function () {
  'use strict';

  var STOP = '的了是在有和与及对于中个们她他你我请问怎么如何几多少点地得着过这那就要会可以吧吗啊嗯什么把被从为或该此之一二上下前后';

  function tokenize(text) {
    var out = [];
    (String(text).toLowerCase().match(/[a-z0-9_]+/g) || []).forEach(function (w) {
      if (w.length > 1) out.push(w);
    });
    (String(text).match(/[一-龥]+/g) || []).forEach(function (run) {
      if (run.length === 1) { if (STOP.indexOf(run) < 0) out.push(run); return; }
      for (var i = 0; i < run.length - 1; i++) {
        var g = run.slice(i, i + 2);
        if (STOP.indexOf(g.charAt(0)) >= 0 || STOP.indexOf(g.charAt(1)) >= 0) continue;
        out.push(g);
      }
    });
    return out;
  }

  function bag(tokens) {
    var m = Object.create(null);
    tokens.forEach(function (t) { m[t] = (m[t] || 0) + 1; });
    return m;
  }

  function build(corpus) {
    var docsList = corpus.map(function (d) { return tokenize(d.text); });
    var df = Object.create(null);
    docsList.forEach(function (tk) {
      Object.keys(bag(tk)).forEach(function (t) { df[t] = (df[t] || 0) + 1; });
    });
    var n = docsList.length;
    var idf = Object.create(null);
    var maxIdf = 1;
    Object.keys(df).forEach(function (t) {
      idf[t] = Math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5));
      if (idf[t] > maxIdf) maxIdf = idf[t];
    });
    var avg = docsList.reduce(function (a, b) { return a + b.length; }, 0) / n;
    return { corpus: corpus, tokens: docsList, df: df, idf: idf, maxIdf: maxIdf, avg: avg, n: n };
  }

  function bm25(index, query, k1, b) {
    k1 = k1 || 1.5; b = b || 0.75;
    var q = tokenize(query);
    var qbag = bag(q);
    return index.tokens.map(function (tk, i) {
      var d = bag(tk);
      var len = tk.length;
      var score = 0;
      Object.keys(qbag).forEach(function (t) {
        if (!d[t]) return;
        var tf = d[t] * (k1 + 1) / (d[t] + k1 * (1 - b + b * len / index.avg));
        score += (index.idf[t] || 0) * tf * qbag[t];
      });
      return { i: i, score: score };
    }).sort(function (a, b2) { return b2.score - a.score; });
  }

  /* 演示用「向量」：IDF 词袋余弦 + 查询词覆盖率（语料中不存在的词按最高 idf 计，未命中会显著拉低分数） */
  function cosine(index, query) {
    var q = bag(tokenize(query));
    var qKeys = Object.keys(q);
    var w = function (k) { return index.idf[k] === undefined ? index.maxIdf : index.idf[k]; };
    var qn = Math.sqrt(qKeys.reduce(function (a, k) { var x = w(k) * q[k]; return a + x * x; }, 0)) || 1;
    var qIdf = qKeys.reduce(function (a, k) { return a + w(k) * q[k]; }, 0) || 1;
    return index.tokens.map(function (tk, i) {
      var d = bag(tk);
      var dot = 0, dn = 0;
      Object.keys(d).forEach(function (k) {
        var x = w(k) * d[k];
        dn += x * x;
        if (q[k]) dot += x * w(k) * q[k];
      });
      dn = Math.sqrt(dn) || 1;
      var cover = qKeys.reduce(function (a, k) { return a + (d[k] ? w(k) * q[k] : 0); }, 0) / qIdf;
      return { i: i, score: 0.55 * (dot / (qn * dn)) + 0.45 * cover };
    }).sort(function (a, b) { return b.score - a.score; });
  }

  function rrf(vectorRank, keywordRank, wVec, wKw) {
    var fused = Object.create(null);
    vectorRank.forEach(function (r, pos) {
      fused[r.i] = (fused[r.i] || 0) + wVec / (60 + pos + 1);
    });
    keywordRank.forEach(function (r, pos) {
      fused[r.i] = (fused[r.i] || 0) + wKw / (60 + pos + 1);
    });
    return Object.keys(fused).map(function (k) {
      return { i: +k, score: fused[k] };
    }).sort(function (a, b) { return b.score - a.score; });
  }

  function jaccard(a, b) {
    var A = bag(tokenize(a)), B = bag(tokenize(b));
    var inter = 0, uni = 0;
    Object.keys(A).forEach(function (k) { if (B[k]) inter += Math.min(A[k], B[k]); });
    Object.keys(A).forEach(function (k) { uni += A[k]; });
    Object.keys(B).forEach(function (k) { uni += B[k]; });
    return uni ? (2 * inter) / uni : 0;
  }

  /* MMR：与已选结果过于相似的标记为被去冗余剔除 */
  function mmr(corpus, picked) {
    var kept = [];
    return picked.map(function (p) {
      var dupOf = null;
      for (var j = 0; j < kept.length; j++) {
        if (jaccard(corpus[p.i].text, corpus[kept[j]].text) > 0.42) { dupOf = kept[j]; break; }
      }
      if (dupOf === null) kept.push(p.i);
      return { i: p.i, score: p.score, dropped: dupOf !== null, dupOf: dupOf };
    });
  }

  function run(index, query, opts) {
    opts = opts || {};
    var wVec = opts.wVec === undefined ? 0.6 : opts.wVec;
    var wKw = opts.wKw === undefined ? 0.4 : opts.wKw;
    var topK = opts.topK || 6;
    var vec = cosine(index, query).slice(0, 12);
    var kw = bm25(index, query).slice(0, 12);
    var fused = rrf(vec, kw, wVec, wKw).slice(0, topK);
    fused.forEach(function (f, pos) { f.pos = pos; });
    var final = opts.mmr === false ? fused : mmr(index.corpus, fused);
    var byIdx = {};
    vec.forEach(function (r, p) { byIdx[r.i] = { vec: r.score, vecRank: p + 1 }; });
    kw.forEach(function (r, p) { if (byIdx[r.i]) { byIdx[r.i].kw = r.score; byIdx[r.i].kwRank = p + 1; } else byIdx[r.i] = { kw: r.score, kwRank: p + 1 }; });
    return {
      vector: vec, keyword: kw,
      results: final.map(function (f) {
        var s = byIdx[f.i] || {};
        return {
          chunk: index.corpus[f.i],
          rrf: f.score,
          vec: s.vec || 0, vecRank: s.vecRank,
          kw: s.kw || 0, kwRank: s.kwRank,
          dropped: f.dropped
        };
      })
    };
  }

  return { build: build, run: run, tokenize: tokenize };
})();
