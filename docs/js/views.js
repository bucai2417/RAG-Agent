/* 各功能页视图 */
window.VIEWS = (function () {
  'use strict';
  var D = window.DEMO, esc = UI.esc;
  var IDX = SEARCH.build(D.chunks);

  /* 演示站会话级状态（刷新即重置，不落任何后端） */
  var S = window.STATE = {
    ai: true, autoConfirm: false, rerank: false, mmr: true,
    model: '17/deepseek-v4.1-flash', scope: '自动（全部有权库）',
    convs: [{ title: '新对话', msgs: [] }], active: 0, tokens: 51.7,
    kbOpen: 'kb-hr', agentTab: 'agents', adminTab: 'members', tkStatus: 'all'
  };

  function sw(on, label, key) {
    return '<button class="sw' + (on ? ' on' : '') + '" role="switch" aria-checked="' + (on ? 'true' : 'false') + '" data-sw="' + key + '" type="button"><i></i>' + esc(label) + '</button>';
  }
  function badge(t, kind) { return '<span class="badge-' + (kind || 'n') + '">' + esc(t) + '</span>'; }
  function cardStat(k, v, sub) {
    return '<div class="kpi"><span>' + esc(k) + '</span><b>' + esc(v) + '</b>' + (sub ? '<em>' + esc(sub) + '</em>' : '') + '</div>';
  }

  /* ---------------- 工作台 ---------------- */
  var workbench = {
    html: function () {
      var maxTok = Math.max.apply(null, D.usage.series[0].values);
      return '' +
      '<div class="page-head"><div><h2>工作台</h2><p>今天有 128 次问答、12 次文档生成、4 张新工单。演示站内所有数字均为本地样本。</p></div>' +
        '<div class="row-gap"><a class="btn btn-solid" href="#/chat">开始提问</a><a class="btn btn-line" href="#/retrieval">打开检索调试台</a></div></div>' +

      '<div class="kpis">' +
        cardStat('今日问答', '128', '较昨日 +21') +
        cardStat('今日 token', '12,056', '3 次调用') +
        cardStat('知识库', D.kbs.length + ' 个', '5,966 个分块') +
        cardStat('待处理工单', '4', '1 张已超时') +
        cardStat('工具成功率', '97.8%', '近 7 天') +
      '</div>' +

      '<div class="cols c2-1">' +
        '<section class="panel"><h3>高频未命中 query</h3><p class="hint">这些提问没有召回到足够相关的内容，可直接补进对应知识库。</p>' +
          '<table class="tbl"><thead><tr><th>提问</th><th>次数</th><th>建议入库</th><th></th></tr></thead><tbody>' +
          D.missQueries.map(function (q) {
            return '<tr><td>' + esc(q.q) + '</td><td class="num">' + q.hits + '</td><td>' + esc(q.kb) + '</td>' +
              '<td class="ta-r"><button class="link" data-goto="#/kb">补内容</button></td></tr>';
          }).join('') + '</tbody></table></section>' +

        '<section class="panel"><h3>工具调用统计</h3><p class="hint">次数 / 成功率 / 平均耗时</p>' +
          '<ul class="bars">' + D.toolStats.map(function (t) {
            return '<li><span class="bar-name mono">' + esc(t.name) + '</span>' +
              '<span class="bar-track"><i style="width:' + (t.calls / 8421 * 100) + '%"></i></span>' +
              '<span class="bar-val mono ' + (t.ok < 95 ? 'warn' : '') + '">' + t.ok + '% · ' + t.ms + 'ms</span></li>';
          }).join('') + '</ul>' +
          '<p class="hint mt">script_run 与 http_request 成功率偏低，多为外部依赖超时；脚本运行受资源限额与环境变量白名单约束。</p>' +
        '</section>' +
      '</div>' +

      '<div class="cols c2-1">' +
        '<section class="panel"><h3>最近工单</h3>' +
          '<table class="tbl"><thead><tr><th>工单</th><th>来源</th><th>优先级</th><th>SLA</th><th>状态</th></tr></thead><tbody>' +
          D.tickets.slice(0, 5).map(function (t) {
            return '<tr class="clickable" data-goto="#/tickets"><td class="mono">' + esc(t.id) + ' · ' + esc(t.title) + '</td>' +
              '<td>' + esc(t.channel) + '</td><td>' + prio(t.priority) + '</td>' +
              '<td class="mono ' + (/超时/.test(t.sla) ? 'warn' : '') + '">' + esc(t.sla) + '</td>' +
              '<td>' + statusBadge(t.status) + '</td></tr>';
          }).join('') + '</tbody></table>' +
          '<p class="hint mt"><button class="link" data-goto="#/tickets">进入客服工作台 →</button></p></section>' +

        '<section class="panel"><h3>近 7 天 token 消耗</h3>' +
          '<div class="chart" role="img" aria-label="按模型统计的近 7 天 token 消耗柱状图">' +
            D.usage.days.map(function (day, i) {
              var total = D.usage.series.reduce(function (a, s) { return a + s.values[i]; }, 0);
              return '<div class="chart-col"><div class="chart-stack" title="' + esc(day) + ' 共 ' + total + 'k token">' +
                D.usage.series.map(function (s) {
                  return '<i style="height:' + (s.values[i] / (maxTok * 3) * 100) + '%;background:' + s.color + '"></i>';
                }).join('') + '</div><span>' + esc(day.slice(3)) + '</span></div>';
            }).join('') +
          '</div>' +
          '<ul class="legend">' + D.usage.series.map(function (s) {
            return '<li><i style="background:' + s.color + '"></i>' + esc(s.name) + '</li>';
          }).join('') + '</ul></section>' +
      '</div>';
    },
    init: function (root) {
      root.querySelectorAll('[data-goto]').forEach(function (b) {
        b.addEventListener('click', function () { location.hash = b.getAttribute('data-goto'); });
      });
    }
  };

  function prio(p) {
    return { urgent: badge('紧急', 'u'), high: badge('高', 'h'), medium: badge('中', 'n'), low: badge('低', 'l') }[p] || p;
  }
  function statusBadge(s) {
    return { open: badge('待处理', 'h'), pending: badge('处理中', 'n'), resolved: badge('已解决', 'g'), closed: badge('已关闭', 'l') }[s] || s;
  }

  /* ---------------- 智能问答 ---------------- */
  var chat = {
    html: function () {
      return '' +
      '<div class="chat-top">' +
        '<label class="fld">模型<select id="selModel">' +
          ['17/deepseek-v4.1-flash', 'claude-sonnet', 'gpt-4.1', 'ollama/qwen2.5', 'local_bge + local_hash（离线自测）'].map(function (m) { return '<option>' + esc(m) + '</option>'; }).join('') +
        '</select></label>' +
        '<label class="fld">检索范围<select id="selScope">' +
          ['自动（全部有权库）', '人事制度库', '财务与报销库', '产品与售前资料', '设备维修图文库', '集团 Dify 外部库'].map(function (m) { return '<option>' + esc(m) + '</option>'; }).join('') +
        '</select></label>' +
        '<div class="sw-group">' + sw(S.ai, 'AI 操作', 'ai') + sw(S.autoConfirm, '免确认', 'autoConfirm') + sw(S.rerank, '重排', 'rerank') + '</div>' +
        '<div class="row-gap grow"></div>' +
        '<button class="btn btn-line sm" id="btnOutline" type="button">☰ 目录</button>' +
        '<button class="btn btn-line sm" id="btnClear" type="button">清空</button>' +
      '</div>' +
      '<div class="chat-body">' +
        '<div class="convs"><button class="btn btn-solid block" id="btnNewConv" type="button">+ 新对话</button>' +
          '<ul class="conv-list" id="convList"></ul></div>' +
        '<div class="msgs" id="msgs" aria-live="polite"></div>' +
        '<div class="composer"><div class="composer-box">' +
          '<select id="selIngest" class="ingest" aria-label="文档入库到">' + D.kbs.map(function (k) { return '<option>' + esc(k.name) + '</option>'; }).join('') + '</select>' +
          '<textarea id="askInput" rows="1" placeholder="输入问题，Enter 发送，Shift+Enter 换行" aria-label="提问"></textarea>' +
          '<button class="btn btn-solid" id="btnSend" type="button">发送</button>' +
        '</div>' +
        '<div class="suggests" id="suggests"></div></div>' +
      '</div>';
    },
    init: function (root) {
      renderConvs();
      renderMsgs();
      var input = root.querySelector('#askInput');
      root.querySelector('#btnSend').addEventListener('click', function () { send(input.value); });
      root.querySelector('#btnNewConv').addEventListener('click', newConv);
      root.querySelector('#btnClear').addEventListener('click', clearConv);
      root.querySelector('#btnOutline').addEventListener('click', outline);
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input.value); input.value = ''; }
      });
      input.addEventListener('input', function () {
        input.style.height = 'auto';
        input.style.height = Math.min(140, input.scrollHeight) + 'px';
      });
      root.querySelectorAll('[data-sw]').forEach(function (b) {
        b.addEventListener('click', function () {
          var k = b.getAttribute('data-sw');
          S[k] = !S[k];
          b.classList.toggle('on', S[k]);
          b.setAttribute('aria-checked', String(S[k]));
          UI.toast((k === 'ai' ? 'AI 操作' : k === 'autoConfirm' ? '免确认' : '重排') + '已' + (S[k] ? '开启' : '关闭'));
        });
      });
      root.querySelector('#selScope').addEventListener('change', function (e) {
        S.scope = e.target.value;
        UI.toast('检索范围：' + S.scope + '（权限条件由 permission 服务唯一入口生成）');
      });
      root.querySelector('#selModel').addEventListener('change', function (e) {
        S.model = e.target.value;
        UI.toast('本会话模型已切换为 ' + e.target.value);
      });
    }
  };

  function renderConvs() {
    var host = document.getElementById('convList');
    if (!host) return;
    host.innerHTML = S.convs.map(function (c, i) {
      return '<li class="' + (i === S.active ? 'is-on' : '') + '"><button type="button" data-conv="' + i + '">' + esc(c.title) +
        '<em>' + c.msgs.filter(function (m) { return m.role === 'user'; }).length + ' 问</em></button></li>';
    }).join('');
    host.querySelectorAll('[data-conv]').forEach(function (b) {
      b.addEventListener('click', function () { S.active = +b.getAttribute('data-conv'); renderConvs(); renderMsgs(); });
    });
  }

  function newConv() {
    S.convs.unshift({ title: '新对话', msgs: [] });
    S.active = 0;
    renderConvs(); renderMsgs();
    document.getElementById('askInput').focus();
  }
  function clearConv() {
    S.convs[S.active] = { title: '新对话', msgs: [] };
    renderConvs(); renderMsgs();
    UI.toast('已清空当前会话');
  }
  function outline() {
    var c = S.convs[S.active];
    var qs = c.msgs.filter(function (m) { return m.role === 'user'; });
    UI.drawer('会话目录 · ' + c.title, qs.length ? '<ol class="outline">' + qs.map(function (m, i) {
      return '<li>' + esc(m.text) + '</li>';
    }).join('') + '</ol>' : '<p class="hint">本会话还没有提问。</p>');
  }

  var SUGGEST = ['请假流程是什么？', '出差上海两晚住宿最多报多少', '帮我生成一份本周会议纪要', '空压机报 E12 怎么处理', '私有化部署怎么报价', '合同验收期是多久'];

  function renderMsgs() {
    var host = document.getElementById('msgs');
    if (!host) return;
    var c = S.convs[S.active];
    if (!c.msgs.length) {
      host.innerHTML = '<div class="empty"><h3>问点什么</h3><p>演示站内置 6 个知识库、' + D.chunks.length + ' 个分块样本，回答由本地检索结果拼装，不调用任何模型。</p>' +
        '<div class="suggests">' + SUGGEST.map(function (s) { return '<button type="button" data-sug="' + esc(s) + '">' + esc(s) + '</button>'; }).join('') + '</div></div>';
      host.querySelectorAll('[data-sug]').forEach(function (b) {
        b.addEventListener('click', function () { send(b.getAttribute('data-sug')); });
      });
      document.getElementById('suggests').innerHTML = '';
      return;
    }
    host.innerHTML = c.msgs.map(bubble).join('');
    host.querySelectorAll('[data-cite]').forEach(function (b) {
      b.addEventListener('click', function () {
        var cits = (c.msgs[+b.getAttribute('data-msg')].plan || {}).citations || [];
        var hit = cits[+b.getAttribute('data-cite') - 1];
        if (!hit) return;
        UI.drawer('引用 [' + hit.n + '] · ' + hit.doc,
          '<p class="cite-meta">所属知识库：' + esc(D.kbs.filter(function (k) { return k.id === hit.kb; })[0].name) + '</p>' +
          '<div class="chunk-box">' + esc(hit.text) + '</div>' +
          '<table class="tbl mini"><tbody><tr><th>向量相似度</th><td class="mono">' + hit.vec.toFixed(4) + '</td></tr>' +
          '<tr><th>BM25 得分</th><td class="mono">' + hit.kw.toFixed(3) + '</td></tr>' +
          '<tr><th>RRF 融合</th><td class="mono">' + hit.rrf.toFixed(5) + '</td></tr></tbody></table>');
      });
    });
    host.querySelectorAll('[data-confirm]').forEach(function (b) {
      b.addEventListener('click', function () {
        var step = c.msgs[+b.getAttribute('data-msg')].plan.steps[+b.getAttribute('data-confirm')];
        UI.confirm('确认执行写操作？', '<p class="mono">' + esc(step.name) + '</p><p>' + esc(step.detail) + '</p>', function () {
          step.confirmed = true;
          b.outerHTML = '<span class="done-tag">已执行 · 已记入审计日志</span>';
          UI.toast('操作已执行并写入审计日志');
        });
      });
    });
    host.scrollTop = host.scrollHeight;
  }

  function bubble(m, mi) {
    if (m.role === 'user') {
      return '<div class="msg me"><div class="bubble">' + esc(m.text) + '</div><span class="ava">我</span></div>';
    }
    var plan = m.plan || { steps: [], citations: [] };
    var steps = plan.steps.map(function (s, i) {
      if (s.kind === 'command') return '<div class="cmd-tag">' + esc(s.name) + ' · ' + esc(s.detail) + '</div>';
      if (s.kind === 'route') return '<div class="route-tag">find_tools → ' + esc(s.detail) + '</div>';
      return '<details class="tool-card" open><summary><span class="tool-dot"></span><b class="mono">' + esc(s.name) + '</b>' +
        '<span class="tool-ms mono">' + s.ms + 'ms</span></summary>' +
        '<div class="tool-body"><div class="kv"><span>args</span><code>' + esc(JSON.stringify(s.args)) + '</code></div>' +
        '<div class="kv"><span>result</span><code>' + esc(s.detail) + '</code></div>' +
        (s.confirm ? (s.confirmed ? '<span class="done-tag">已执行 · 已记入审计日志</span>' : '<button class="btn btn-line sm" data-confirm="' + i + '" data-msg="' + mi + '" type="button">需要确认 · 点击执行</button>') : '') +
        '</div></details>';
    }).join('');
    var think = '<details class="think"><summary>思考过程</summary><div class="think-body">' +
      (plan.citations.length
        ? '1. 识别意图：' + esc(plan.kind === 'miss' ? '知识库无命中' : '知识问答') + '<br>2. 装配工具：' + esc(plan.steps.map(function (s) { return s.name; }).join(' → ') || 'kb_search') + '<br>' +
          '3. 召回 ' + plan.citations.length + ' 条分块，RRF 融合后取前 3 条作为上下文<br>4. 生成答案并标注引用编号'
        : '1. 三路召回最高得分低于相关阈值 0.16<br>2. 判定为知识库未命中，按防幻觉策略改用模型自身知识并说明来源') +
      '</div></details>';
    var tk = m.tokens || S.tokens;
    var foot = '本会话累计 ' + tk.toFixed(1) + 'k tokens · 命中缓存 ' + Math.round((tk % 7) * 912) + ' · 110963 ↓ 1093';
    return '<div class="msg"><span class="ava">AI</span><div class="bubble wide">' + think + steps +
      '<div class="answer">' + inline(m.text, mi) + '</div>' +
      '<div class="msg-foot mono">' + foot + '</div>' +
      '</div></div>';
  }

  function inline(text, mi) {
    return esc(text)
      .replace(/\[(\d)\]/g, function (_, n) { return '<button class="cite" data-cite="' + n + '" data-msg="' + mi + '" type="button">[' + n + ']</button>'; })
      .replace(/\n/g, '<br>');
  }

  var typing = false;
  function send(text) {
    text = (text || '').trim();
    if (!text || typing) return;
    var c = S.convs[S.active];
    var input = document.getElementById('askInput');
    input.value = '';
    input.style.height = 'auto';
    if (c.msgs.length === 0) c.title = text.slice(0, 14);
    c.msgs.push({ role: 'user', text: text });
    var plan = CHAT.plan(text, S, IDX);
    S.tokens += Math.round(plan.tokens / 100) / 10;
    typing = true;
    renderConvs();
    var host = document.getElementById('msgs');
    host.innerHTML += '<div class="msg"><span class="ava">AI</span><div class="bubble wide" id="live"><div class="typing"><i></i><i></i><i></i></div></div></div>';
    host.scrollTop = host.scrollHeight;

    var live = document.getElementById('live');
    var mi = c.msgs.length;
    var seq = plan.steps.map(function (s, i) { return i; });
    var n = 0;
    var msg = { role: 'ai', text: '', plan: plan, tokens: S.tokens };
    function commit() {
      if (c.msgs[c.msgs.length - 1] !== msg) c.msgs.push(msg);
      msg.text = plan.answer;
      typing = false;
      renderMsgs();
    }
    (function next() {
      if (!document.getElementById('live')) { commit(); return; }
      if (n < seq.length) {
        var s = plan.steps[seq[n]];
        live.innerHTML += s.kind === 'tool'
          ? '<details class="tool-card" open><summary><span class="tool-spin"></span><b class="mono">' + esc(s.name) + '</b><span class="tool-ms mono">运行中…</span></summary></details>'
          : (s.kind === 'command' ? '<div class="cmd-tag">' + esc(s.name) + ' · ' + esc(s.detail) + '</div>' : '<div class="route-tag">find_tools → ' + esc(s.detail) + '</div>');
        host.scrollTop = host.scrollHeight;
        n++;
        setTimeout(function () {
          var open = live.querySelectorAll('.tool-card');
          var last = open[open.length - 1];
          if (last) {
            last.querySelector('.tool-spin').outerHTML = '<span class="tool-dot"></span>';
            last.querySelector('.tool-ms').textContent = s.ms + 'ms';
            last.querySelector('summary').insertAdjacentHTML('afterend',
              '<div class="tool-body"><div class="kv"><span>args</span><code>' + esc(JSON.stringify(s.args || {})) + '</code></div>' +
              '<div class="kv"><span>result</span><code>' + esc(s.detail) + '</code></div>' +
              (s.confirm ? '<button class="btn btn-line sm" data-confirm="' + seq[n - 1] + '" data-msg="' + mi + '" type="button">需要确认 · 点击执行</button>' : '') +
              '</div>');
            last.querySelector('[data-confirm]') && last.querySelector('[data-confirm]').addEventListener('click', function () {
              UI.confirm('确认执行写操作？', '<p class="mono">' + esc(s.name) + '</p><p>' + esc(s.detail) + '</p>', function () {
                last.querySelector('[data-confirm]').outerHTML = '<span class="done-tag">已执行 · 已记入审计日志</span>';
                UI.toast('操作已执行并写入审计日志');
              });
            });
          }
          host.scrollTop = host.scrollHeight;
          setTimeout(next, 220);
        }, s.kind === 'tool' ? Math.min(700, 180 + (s.ms || 200) / 4) : 260);
        return;
      }
      c.msgs.push(msg);
      live.innerHTML = '<div class="answer" id="liveAnswer"></div>';
      var el = document.getElementById('liveAnswer');
      var full = plan.answer, i = 0;
      (function type() {
        if (!document.getElementById('liveAnswer')) { commit(); return; }
        i = Math.min(full.length, i + 6);
        el.innerHTML = inline(full.slice(0, i), mi);
        host.scrollTop = host.scrollHeight;
        if (i < full.length) { setTimeout(type, 16); } else { commit(); }
      })();
    })();
  }

  /* ---------------- 知识库 ---------------- */
  var kb = {
    html: function () {
      var k = D.kbs.filter(function (x) { return x.id === S.kbOpen; })[0] || D.kbs[0];
      return '' +
      '<div class="page-head"><div><h2>知识库</h2><p>三类知识库同级并存，建库时选类型。点击卡片查看文档与分块。</p></div>' +
        '<div class="row-gap"><button class="btn btn-line" id="btnHealth" type="button">内容巡检</button><button class="btn btn-solid" id="btnNewKb" type="button">+ 新建知识库</button></div></div>' +
      '<div class="kb-grid">' + D.kbs.map(function (x) {
        return '<article class="kb-card' + (x.id === k.id ? ' is-on' : '') + '" data-kb="' + x.id + '" tabindex="0">' +
          '<header><span class="kb-type t-' + x.type + '">' + esc(D.typeName[x.type]) + '</span>' +
          '<span class="kb-vis">' + esc(D.visName[x.visibility]) + '</span></header>' +
          '<h3>' + esc(x.name) + '</h3>' +
          '<p class="hint">' + esc(x.embedding) + '</p>' +
          '<dl class="kb-meta"><div><dt>文档</dt><dd>' + esc(x.docs) + '</dd></div><div><dt>分块</dt><dd>' + esc(x.chunks) + '</dd></div>' +
          '<div><dt>容量</dt><dd>' + esc(x.size) + '</dd></div><div><dt>更新</dt><dd>' + esc(x.updated) + '</dd></div></dl>' +
          (x.health.length ? '<p class="warn-line">⚠ ' + esc(x.health.join('；')) + '</p>' : '<p class="ok-line">✓ 健康检查通过</p>') +
          '</article>';
      }).join('') + '</div>' +
      '<section class="panel" id="kbDetail"></section>';
    },
    init: function (root) {
      function drawDetail() {
        var k = D.kbs.filter(function (x) { return x.id === S.kbOpen; })[0] || D.kbs[0];
        var list = D.docs[k.id] || [];
        var cs = D.chunks.filter(function (c) { return c.kb === k.id; });
        root.querySelector('#kbDetail').innerHTML =
          '<div class="panel-head"><h3>' + esc(k.name) + ' · 文档与分块</h3><span class="hint">' + esc(D.typeName[k.type]) + ' · 权限：' + esc(D.visName[k.visibility]) + ' · 负责人 ' + esc(k.owner) + '</span></div>' +
          (list.length ? '<table class="tbl"><thead><tr><th>文档</th><th>分块</th><th>token</th><th>标签</th><th>状态</th><th></th></tr></thead><tbody>' +
            list.map(function (d) {
              return '<tr><td>' + esc(d.name) + '</td><td class="num">' + d.chunks + '</td><td class="num">' + d.tokens.toLocaleString() + '</td>' +
                '<td>' + d.tags.map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('') + '</td>' +
                '<td>' + (d.status === 'ok' ? badge('已完成', 'g') : d.status === 'fail' ? badge('解析失败', 'u') : badge('内容陈旧', 'h')) + '</td>' +
                '<td class="ta-r"><button class="link" data-reingest="' + esc(d.name) + '" type="button">重灌</button></td></tr>';
            }).join('') + '</tbody></table>'
            : '<p class="hint">外部知识库不落分块，检索时实时联邦调用并参与融合排序。</p>') +
          (cs.length ? '<h4 class="sub">分块预览（可编辑 / 拆分 / 删除）</h4><div class="chunk-list">' + cs.map(function (c, i) {
            return '<div class="chunk"><span class="mono hint">#' + (i + 1) + '</span><p>' + esc(c.text) + '</p><span class="hint">' + esc(c.doc) + '</span></div>';
          }).join('') + '</div>' : '');
        root.querySelectorAll('[data-reingest]').forEach(function (b) {
          b.addEventListener('click', function () { UI.toast('已重新入队：' + b.getAttribute('data-reingest')); });
        });
      }
      root.querySelectorAll('[data-kb]').forEach(function (el) {
        function open() { S.kbOpen = el.getAttribute('data-kb'); root.querySelectorAll('.kb-card').forEach(function (c) { c.classList.toggle('is-on', c === el); }); drawDetail(); }
        el.addEventListener('click', open);
        el.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
      });
      drawDetail();
      root.querySelector('#btnNewKb').addEventListener('click', function () {
        UI.drawer('新建知识库', '<label class="fld col">名称<input placeholder="如：生产工艺库"></label>' +
          '<label class="fld col">类型<select><option>向量知识库</option><option>图文知识库</option><option>外部知识库</option></select></label>' +
          '<label class="fld col">可见性<select><option>公开（全租户）</option><option selected>内部（按成员）</option><option>私有（仅显式成员）</option></select></label>' +
          '<label class="fld col">向量模型<select><option>bge-m3 · 1024</option><option>text-embedding-3-small · 1536</option><option>不启用（图文库）</option></select></label>' +
          '<p class="hint">分块策略：父子分块 / 定长分块 / 递归分块。切换 embedding 模型需重建该库向量。</p>');
      });
      root.querySelector('#btnHealth').addEventListener('click', function () {
        var issues = [];
        D.kbs.forEach(function (k) { k.health.forEach(function (h) { issues.push(k.name + '：' + h); }); });
        UI.drawer('内容巡检结果', issues.length ? '<ul class="ticks">' + issues.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('') + '</ul>' : '<p class="hint">全部知识库健康检查通过。</p>' +
          '<p class="hint mt">检查项：空库 / 解析失败 / 无分块 / 未标签 / 陈旧文档；另聚合高频未命中 query。</p>');
      });
    }
  };

  /* ---------------- 检索调试台 ---------------- */
  var retrieval = {
    html: function () {
      return '' +
      '<div class="page-head"><div><h2>检索调试台</h2><p>观察三路召回的命中与分数。这里跑的是本地简化实现（中文二元组 + BM25 + 词袋余弦），用于演示链路与参数影响。</p></div></div>' +
      '<div class="dbg-bar">' +
        '<input id="dbgQ" class="dbg-q" placeholder="输入检索问题，例如：年假 审批 提前几天" value="年假 审批 提前几天">' +
        '<label class="rng">向量权重<input type="range" id="wVec" min="0" max="100" value="60"><output class="mono">0.60</output></label>' +
        '<label class="rng">关键词权重<input type="range" id="wKw" min="0" max="100" value="40"><output class="mono">0.40</output></label>' +
        '<label class="rng">top_k<input type="range" id="dbgK" min="1" max="10" value="6"><output class="mono">6</output></label>' +
        '<div class="sw-group">' + sw(true, 'MMR 去冗余', 'mmr') + sw(false, '重排', 'rerank') + '</div>' +
        '<button class="btn btn-solid" id="dbgGo" type="button">检索</button>' +
      '</div>' +
      '<div class="cols c1-1" id="dbgOut"></div>';
    },
    init: function (root) {
      var q = root.querySelector('#dbgQ'), wv = root.querySelector('#wVec'), wk = root.querySelector('#wKw'), tk = root.querySelector('#dbgK');
      function sync() {
        wv.nextElementSibling.value = (wv.value / 100).toFixed(2);
        wk.nextElementSibling.value = (wk.value / 100).toFixed(2);
        tk.nextElementSibling.value = tk.value;
      }
      function run() {
        sync();
        var query = q.value.trim() || '年假';
        var res = SEARCH.run(IDX, query, { wVec: wv.value / 100, wKw: wk.value / 100, topK: +tk.value, mmr: S.mmr });
        var kbName = function (id) { return D.kbs.filter(function (k) { return k.id === id; })[0].name; };
        root.querySelector('#dbgOut').innerHTML =
          '<section class="panel"><h3>融合结果</h3><p class="hint">RRF = Σ weight / (60 + rank)，命中权限过滤后剩余 ' + res.results.length + ' 条。</p>' +
          '<table class="tbl"><thead><tr><th>#</th><th>分块</th><th>所属库</th><th class="num">RRF</th><th class="num">向量</th><th class="num">BM25</th></tr></thead><tbody>' +
          res.results.map(function (r, i) {
            return '<tr class="' + (r.dropped ? 'is-dropped' : '') + '"><td class="num">' + (i + 1) + '</td>' +
              '<td><span class="doc-line">' + esc(r.chunk.text.slice(0, 58)) + '…</span><span class="hint">' + esc(r.chunk.doc) + (r.dropped ? ' · 与已选结果相似，MMR 剔除' : '') + '</span></td>' +
              '<td>' + esc(kbName(r.chunk.kb)) + '</td><td class="num mono">' + r.rrf.toFixed(5) + '</td>' +
              '<td class="num mono">' + r.vec.toFixed(4) + '<em class="rk">' + (r.vecRank || '—') + '</em></td>' +
              '<td class="num mono">' + r.kw.toFixed(3) + '<em class="rk">' + (r.kwRank || '—') + '</em></td></tr>';
          }).join('') + '</tbody></table></section>' +
          '<div class="stack">' +
            '<section class="panel"><h3>向量召回 Top 6</h3>' + rankTable(res.vector, kbName, 'cos') + '</section>' +
            '<section class="panel"><h3>BM25 关键词召回 Top 6</h3>' + rankTable(res.keyword, kbName, 'bm25') + '</section>' +
          '</div>';
      }
      [wv, wk, tk].forEach(function (i) { i.addEventListener('input', function () { sync(); run(); }); });
      q.addEventListener('keydown', function (e) { if (e.key === 'Enter') run(); });
      root.querySelector('#dbgGo').addEventListener('click', run);
      root.querySelectorAll('[data-sw]').forEach(function (b) {
        b.addEventListener('click', function () {
          var k = b.getAttribute('data-sw');
          S[k] = !S[k];
          b.classList.toggle('on', S[k]);
          b.setAttribute('aria-checked', String(S[k]));
          run();
        });
      });
      sync(); run();
    }
  };
  function rankTable(list, kbName, kind) {
    return '<table class="tbl mini"><tbody>' + list.slice(0, 6).map(function (r, i) {
      var c = IDX.corpus[r.i];
      return '<tr><td class="num">' + (i + 1) + '</td><td><span class="doc-line">' + esc(c.text.slice(0, 40)) + '…</span><span class="hint">' + esc(kbName(c.kb)) + '</span></td>' +
        '<td class="num mono">' + (kind === 'cos' ? r.score.toFixed(4) : r.score.toFixed(3)) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }

  /* ---------------- 智能体 ---------------- */
  var agents = {
    html: function () {
      return '' +
      '<div class="page-head"><div><h2>智能体平台</h2><p>ReAct 工具循环：模型自主多轮调用工具直到得出答案，运行过程可视化。</p></div>' +
      '<div class="row-gap"><a class="btn btn-line" href="#/workflow">工作流编排</a><button class="btn btn-solid" id="btnNewAgent" type="button">+ 新建智能体</button></div></div>' +
      '<div class="tabs-line" role="tablist">' +
        ['agents:智能体', 'tools:工具', 'skills:技能市场'].map(function (t) {
          var p = t.split(':');
          return '<button role="tab" aria-selected="' + (S.agentTab === p[0]) + '" class="' + (S.agentTab === p[0] ? 'is-on' : '') + '" data-tab="' + p[0] + '" type="button">' + esc(p[1]) + '</button>';
        }).join('') + '</div>' +
      '<div id="agentBody"></div>';
    },
    init: function (root) {
      function draw() {
        var body = root.querySelector('#agentBody');
        if (S.agentTab === 'agents') {
          body.innerHTML = '<div class="agent-grid">' + D.agents.map(function (a, i) {
            return '<article class="agent-card"><header><h3>' + esc(a.name) + '</h3>' + badge(a.mode, 'n') + '</header>' +
              '<p class="hint">' + esc(a.desc) + '</p>' +
              '<div class="kv"><span>模型</span><code class="mono">' + esc(a.model) + '</code></div>' +
              '<div class="kv"><span>工具</span><code class="mono">' + esc(a.tools.join(', ')) + '</code></div>' +
              '<div class="kv"><span>温度</span><code class="mono">' + a.temp + '</code></div>' +
              '<footer><span class="hint mono">' + a.runs.toLocaleString() + ' 次运行 · 成功率 ' + a.ok + '%</span>' +
              '<button class="btn btn-line sm" data-run="' + i + '" type="button">运行一次</button></footer></article>';
          }).join('') + '</div><section class="panel" id="trace"><h3>运行轨迹</h3><p class="hint">点击任意智能体的「运行一次」查看 ReAct 循环。</p></section>';
          body.querySelectorAll('[data-run]').forEach(function (b) {
            b.addEventListener('click', function () { runTrace(D.agents[+b.getAttribute('data-run')]); });
          });
        } else if (S.agentTab === 'tools') {
          body.innerHTML = '<div class="row-gap mb"><input id="toolQ" class="grow" placeholder="搜索工具" style="max-width:320px"></div><div class="tool-grid" id="toolGrid"></div>' +
            '<p class="hint mt">常驻内置工具 15 个，管理运维工具 96 个；问答页通过 find_tools 按需装配，减少上下文占用。</p>';
          function drawTools() {
            var kw = (body.querySelector('#toolQ').value || '').toLowerCase();
            body.querySelector('#toolGrid').innerHTML = D.tools.filter(function (t) {
              return !kw || (t.name + t.desc + t.group).toLowerCase().indexOf(kw) >= 0;
            }).map(function (t) {
              return '<div class="tool-item"><b class="mono">' + esc(t.name) + '</b><span>' + esc(t.desc) + '</span>' +
                '<em>' + (t.builtin ? '内置' : '运维') + ' · ' + esc(t.group) + '</em></div>';
            }).join('') || '<p class="hint">没有匹配的工具。</p>';
          }
          body.querySelector('#toolQ').addEventListener('input', drawTools);
          drawTools();
        } else {
          body.innerHTML = '<div class="row-gap mb"><input id="skillQ" class="grow" placeholder="从 GitHub 搜索技能包，如 report" style="max-width:360px">' +
            '<button class="btn btn-line" id="skillGo" type="button">find_skill</button></div><div id="skillList"></div>';
          function drawSkills(kw) {
            var list = D.skills.filter(function (s) { return !kw || (s.name + s.desc + s.repo).toLowerCase().indexOf(kw.toLowerCase()) >= 0; });
            body.querySelector('#skillList').innerHTML = list.length ? '<div class="skill-grid">' + list.map(function (s, i) {
              return '<article class="skill-card"><h3 class="mono">' + esc(s.name) + '</h3><p class="hint mono">' + esc(s.repo) + '</p>' +
                '<p>' + esc(s.desc) + '</p><footer><span class="hint">' + esc(s.lang) + ' 运行时 · ' + s.installs + ' 次安装</span>' +
                '<button class="btn btn-solid sm" data-inst="' + i + '" data-kw="' + esc(kw || '') + '" type="button">安装</button></footer></article>';
            }).join('') + '</div>' : '<p class="hint">没有匹配的技能包。</p>';
            body.querySelectorAll('[data-inst]').forEach(function (b) {
              b.addEventListener('click', function () {
                var s = list[+b.getAttribute('data-inst')];
                b.outerHTML = '<span class="done-tag">依赖安装中 · pip install -r requirements.txt</span>';
                UI.toast('已导入 ' + s.name + '，脚本按扩展名识别运行时');
                setTimeout(function () { drawSkills(body.querySelector('#skillQ').value); }, 1200);
              });
            });
          }
          body.querySelector('#skillGo').addEventListener('click', function () { drawSkills(body.querySelector('#skillQ').value); UI.toast('find_skill：已在本地索引中检索'); });
          body.querySelector('#skillQ').addEventListener('keydown', function (e) { if (e.key === 'Enter') drawSkills(e.target.value); });
          drawSkills('');
        }
      }
      function runTrace(agent) {
        var host = root.querySelector('#trace');
        var plan = CHAT.plan('帮我生成本周会议纪要并同步给团队', { ai: true, autoConfirm: true, rerank: true }, IDX);
        host.innerHTML = '<div class="panel-head"><h3>运行轨迹 · ' + esc(agent.name) + '</h3><span class="hint mono">' + esc(agent.model) + '</span></div><ol class="trace" id="traceList"></ol>';
        var list = host.querySelector('#traceList');
        var steps = [{ t: '用户输入', d: '帮我生成本周会议纪要并同步给团队' }].concat(
          plan.steps.map(function (s) { return { t: s.name, d: s.detail, ms: s.ms }; }),
          [{ t: 'final_answer', d: '已生成 docx 并推送到企业微信群，共 4 轮工具调用。', ms: 1240 }]);
        var i = 0;
        (function step() {
          if (i >= steps.length) {
            list.insertAdjacentHTML('beforeend', '<li class="trace-end">✓ 循环结束 · 总耗时 ' + steps.reduce(function (a, s) { return a + (s.ms || 200); }, 0) + 'ms · 消耗 4,213 tokens</li>');
            return;
          }
          var s = steps[i];
          list.insertAdjacentHTML('beforeend', '<li class="is-run"><b class="mono">' + esc(s.t) + '</b><span>' + esc(s.d) + '</span></li>');
          var li = list.lastElementChild;
          setTimeout(function () {
            li.classList.remove('is-run');
            li.querySelector('span').insertAdjacentHTML('afterend', '<em class="mono">' + (s.ms || 200) + 'ms</em>');
            i++; step();
          }, 520);
        })();
      }
      root.querySelectorAll('[data-tab]').forEach(function (b) {
        b.addEventListener('click', function () {
          S.agentTab = b.getAttribute('data-tab');
          root.querySelectorAll('[data-tab]').forEach(function (x) { x.classList.toggle('is-on', x === b); x.setAttribute('aria-selected', String(x === b)); });
          draw();
        });
      });
      root.querySelector('#btnNewAgent').addEventListener('click', function () {
        UI.drawer('新建智能体', '<label class="fld col">名称<input placeholder="如：合同审查助手"></label>' +
          '<label class="fld col">基座模型<select><option>claude-sonnet</option><option>deepseek-v4.1-flash</option><option>gpt-4.1</option></select></label>' +
          '<label class="fld col">装配工具<select multiple size="6">' + D.tools.map(function (t) { return '<option>' + esc(t.name) + '</option>'; }).join('') + '</select></label>' +
          '<label class="fld col">行为模式<input placeholder="同一智能体可配置多套行为，运行时切换"></label>');
      });
      draw();
    }
  };

  /* ---------------- 工作流 ---------------- */
  var workflow = {
    html: function () {
      return '' +
      '<div class="page-head"><div><h2>工作流编排</h2><p>自绘 DAG 画布，15 类节点，支持变量引用、并行网关、人工审批与版本回滚。点击节点查看配置。</p></div>' +
      '<div class="row-gap"><button class="btn btn-line" id="wfSave" type="button">保存草稿</button><button class="btn btn-line" id="wfPub" type="button">发布 v3</button><button class="btn btn-solid" id="wfRun" type="button">▶ 试运行</button></div></div>' +
      '<div class="wf-wrap"><div class="wf-canvas" id="wfCanvas"><svg class="wf-svg" id="wfSvg" aria-hidden="true"></svg></div>' +
      '<aside class="wf-insp" id="wfInsp"></aside></div>' +
      '<div class="wf-log mono" id="wfLog">等待试运行…</div>';
    },
    init: function (root) {
      var canvas = root.querySelector('#wfCanvas'), svg = root.querySelector('#wfSvg');
      var W = 1400, H = 340;
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      var pos = {};
      D.wfNodes.forEach(function (n) { pos[n.id] = n; });
      svg.innerHTML = D.wfEdges.map(function (e) {
        var a = pos[e[0]], b = pos[e[1]];
        var x1 = a.x + 150, y1 = a.y + 26, x2 = b.x, y2 = b.y + 26;
        return '<path class="edge" data-edge="' + e.join('-') + '" d="M' + x1 + ' ' + y1 + ' C' + (x1 + 60) + ' ' + y1 + ',' + (x2 - 60) + ' ' + y2 + ',' + x2 + ' ' + y2 + '"/>';
      }).join('');
      canvas.insertAdjacentHTML('beforeend', D.wfNodes.map(function (n) {
        return '<button class="node n-' + n.type + '" data-node="' + n.id + '" style="left:' + n.x + 'px;top:' + n.y + 'px" type="button">' +
          '<span class="node-type mono">' + esc(n.type) + '</span><b>' + esc(n.label) + '</b></button>';
      }).join(''));
      function inspect(id) {
        var n = pos[id];
        root.querySelectorAll('.node').forEach(function (el) { el.classList.toggle('is-on', el.getAttribute('data-node') === id); });
        root.querySelector('#wfInsp').innerHTML = '<h3>' + esc(n.label) + '</h3><span class="badge-n mono">' + esc(n.type) + '</span>' +
          '<dl class="cfg">' + Object.keys(n.cfg).map(function (k) {
            return '<div><dt class="mono">' + esc(k) + '</dt><dd>' + esc(Array.isArray(n.cfg[k]) ? n.cfg[k].join(' / ') : n.cfg[k]) + '</dd></div>';
          }).join('') + '</dl><p class="hint">节点间通过变量引用传递，上游输出以 <code>' + esc(n.id) + '.output</code> 形式可选。</p>';
      }
      canvas.querySelectorAll('[data-node]').forEach(function (b) {
        b.addEventListener('click', function () { inspect(b.getAttribute('data-node')); });
      });
      inspect('n2');
      var path = ['n1', 'n2', 'n3', 'n4', 'n8', 'n9', 'n10'];
      var busy = false;
      root.querySelector('#wfRun').addEventListener('click', function () {
        if (busy) return;
        busy = true;
        canvas.querySelectorAll('.node,.edge').forEach(function (el) { el.classList.remove('is-run', 'is-done'); });
        root.querySelector('#wfLog').innerHTML = '';
        var i = 0;
        (function go() {
          if (i > 0) {
            var prev = canvas.querySelector('[data-node="' + path[i - 1] + '"]');
            prev.classList.remove('is-run'); prev.classList.add('is-done');
          }
          if (i >= path.length) {
            busy = false;
            root.querySelector('#wfLog').insertAdjacentHTML('beforeend', '<div>✓ 运行完成 · 7 节点 · 3.4s · 输出 ticket_id=TK-2089</div>');
            return;
          }
          var id = path[i], n = pos[id];
          canvas.querySelector('[data-node="' + id + '"]').classList.add('is-run');
          if (i > 0) {
            var edge = svg.querySelector('[data-edge="' + path[i - 1] + '-' + id + '"]');
            if (edge) { edge.classList.add('is-run'); setTimeout(function () { edge.classList.remove('is-run'); edge.classList.add('is-done'); }, 420); }
          }
          root.querySelector('#wfLog').insertAdjacentHTML('beforeend', '<div>→ ' + esc(n.label) + ' <span class="hint">' + esc(JSON.stringify(n.cfg).slice(0, 60)) + '…</span></div>');
          i++;
          setTimeout(go, n.type === 'human' ? 900 : 520);
        })();
      });
      root.querySelector('#wfSave').addEventListener('click', function () { UI.toast('草稿已保存'); });
      root.querySelector('#wfPub').addEventListener('click', function () {
        UI.confirm('发布 v3？', '<p>发布后新会话按最新版本执行，历史版本可一键回滚。</p>', function () { UI.toast('已发布 v3，v2 保留为可回滚版本'); });
      });
    }
  };

  /* ---------------- 客服工单 ---------------- */
  var tickets = {
    html: function () {
      return '' +
      '<div class="page-head"><div><h2>客服工作台</h2><p>外部渠道转人工自动建单，坐席在系统内回复会原路回发到 QQ / 微信 / 企微 / 飞书。</p></div>' +
      '<div class="row-gap"><button class="btn btn-line" id="tkBatch" type="button">批量操作</button><button class="btn btn-solid" id="tkNew" type="button">+ 新建工单</button></div></div>' +
      '<div class="filter-bar">' +
        ['all:全部', 'open:待处理', 'pending:处理中', 'resolved:已解决', 'closed:已关闭'].map(function (t) {
          var p = t.split(':');
          return '<button class="chip-btn' + (S.tkStatus === p[0] ? ' is-on' : '') + '" data-st="' + p[0] + '" type="button">' + esc(p[1]) + '</button>';
        }).join('') +
        '<select id="tkPrio" class="sm-sel"><option value="">全部优先级</option><option value="urgent">紧急</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select>' +
        '<select id="tkCh" class="sm-sel"><option value="">全部渠道</option>' + ['企业微信', 'QQ 机器人', '站内', '飞书', '微信'].map(function (c) { return '<option>' + esc(c) + '</option>'; }).join('') + '</select>' +
        '<input id="tkQ" placeholder="搜索工单标题 / 客户" class="grow">' +
      '</div>' +
      '<section class="panel nopad"><table class="tbl"><thead><tr><th class="ck"><input type="checkbox" id="tkAll" aria-label="全选"></th><th>工单</th><th>客户</th><th>渠道</th><th>优先级</th><th>SLA</th><th>状态</th><th>负责人</th></tr></thead><tbody id="tkBody"></tbody></table></section>' +
      '<p class="hint mt" id="tkSel"></p>';
    },
    init: function (root) {
      var sel = {};
      function draw() {
        var kw = (root.querySelector('#tkQ').value || '').toLowerCase();
        var pr = root.querySelector('#tkPrio').value, ch = root.querySelector('#tkCh').value;
        var list = D.tickets.filter(function (t) {
          return (S.tkStatus === 'all' || t.status === S.tkStatus) && (!pr || t.priority === pr) && (!ch || t.channel === ch) &&
            (!kw || (t.title + t.customer + t.id).toLowerCase().indexOf(kw) >= 0);
        });
        root.querySelector('#tkBody').innerHTML = list.map(function (t) {
          return '<tr><td class="ck"><input type="checkbox" data-id="' + t.id + '" aria-label="选择 ' + esc(t.id) + '"' + (sel[t.id] ? ' checked' : '') + '></td>' +
            '<td><b>' + esc(t.title) + '</b><span class="hint mono">' + esc(t.id) + ' · ' + esc(t.created) + '</span></td>' +
            '<td>' + esc(t.customer) + '</td><td>' + esc(t.channel) + '</td><td>' + prio(t.priority) + '</td>' +
            '<td class="mono ' + (/超时/.test(t.sla) ? 'warn' : '') + '">' + esc(t.sla) + '</td><td>' + statusBadge(t.status) + '</td><td>' + esc(t.assignee) + '</td></tr>';
        }).join('') || '<tr><td colspan="8" class="hint pad">没有符合条件的工单。</td></tr>';
        root.querySelectorAll('#tkBody [data-id]').forEach(function (cb) {
          cb.addEventListener('change', function () { sel[cb.getAttribute('data-id')] = cb.checked; count(); });
          cb.closest('tr').addEventListener('click', function (e) {
            if (e.target.tagName === 'INPUT') return;
            open(list.filter(function (t) { return t.id === cb.getAttribute('data-id'); })[0]);
          });
        });
        count();
        function count() {
          var n = Object.keys(sel).filter(function (k) { return sel[k]; }).length;
          root.querySelector('#tkSel').textContent = n ? '已选 ' + n + ' 条，可批量关单 / 改派 / 打标签。' : '共 ' + list.length + ' 条工单。点击任意一行查看会话详情并回复。';
        }
      }
      function open(t) {
        var body = '<div class="tk-meta"><span>' + esc(t.customer) + '</span><span class="mono">' + esc(t.channel) + '</span>' + prio(t.priority) + statusBadge(t.status) + '<span class="mono">SLA ' + esc(t.sla) + '</span></div>' +
          '<div class="thread">' + t.msgs.map(function (m) {
            return '<div class="th th-' + m.who + '"><b>' + esc(m.name) + '</b><p>' + esc(m.text) + '</p></div>';
          }).join('') + '</div>';
        var d = UI.drawer(t.title, body,
          '<div class="reply"><textarea id="tkReply" rows="2" placeholder="回复将原路回发到' + esc(t.channel) + '"></textarea>' +
          '<div class="row-gap"><select id="tkQuick" class="sm-sel"><option>引用话术：请提供订单号，我为您查询</option><option>话术：已为您升级至技术组，2 小时内回复</option><option>话术：附件为操作指引，请按步骤处理</option></select>' +
          '<button class="btn btn-line" id="tkAttach" type="button">发附件</button><button class="btn btn-solid" id="tkSend" type="button">回复</button></div></div>');
        d.querySelector('#tkQuick').addEventListener('change', function (e) { d.querySelector('#tkReply').value = e.target.value.split('：')[1] || e.target.value; });
        d.querySelector('#tkAttach').addEventListener('click', function () { UI.toast('附件已随回复发出（演示）'); });
        d.querySelector('#tkSend').addEventListener('click', function () {
          var v = d.querySelector('#tkReply').value.trim();
          if (!v) { UI.toast('回复内容不能为空', 'warn'); return; }
          t.msgs.push({ who: 'agent', name: '系统管理员', text: v });
          t.status = 'pending';
          UI.closeDrawer(); draw(); UI.toast('已回复并回发至' + t.channel);
        });
      }
      root.querySelectorAll('[data-st]').forEach(function (b) {
        b.addEventListener('click', function () {
          S.tkStatus = b.getAttribute('data-st');
          root.querySelectorAll('[data-st]').forEach(function (x) { x.classList.toggle('is-on', x === b); });
          draw();
        });
      });
      ['#tkPrio', '#tkCh', '#tkQ'].forEach(function (id) {
        root.querySelector(id).addEventListener('input', draw);
      });
      root.querySelector('#tkAll').addEventListener('change', function (e) {
        root.querySelectorAll('#tkBody [data-id]').forEach(function (cb) { cb.checked = e.target.checked; sel[cb.getAttribute('data-id')] = e.target.checked; });
        draw();
      });
      root.querySelector('#tkNew').addEventListener('click', function () {
        UI.drawer('新建工单', '<label class="fld col">标题<input placeholder="如：客户咨询私有化部署价格"></label>' +
          '<label class="fld col">来源渠道<select><option>站内</option><option>企业微信</option><option>QQ 机器人</option><option>飞书</option><option>微信</option></select></label>' +
          '<label class="fld col">优先级<select><option>紧急</option><option selected>高</option><option>中</option><option>低</option></select></label>' +
          '<label class="fld col">分派给<select><option>李伟</option><option>客服组长</option><option>AI 自动处理</option></select></label>' +
          '<p class="hint">渠道转人工时会自动建单，也可由 ticket_create 工具代建。</p>');
      });
      root.querySelector('#tkBatch').addEventListener('click', function () {
        var n = Object.keys(sel).filter(function (k) { return sel[k]; }).length;
        if (!n) { UI.toast('先勾选要处理的工单', 'warn'); return; }
        UI.confirm('批量处理 ' + n + ' 条工单', '<p>将执行：关单 → 打标签「批量处理」→ 通知负责人。</p>', function () {
          Object.keys(sel).forEach(function (k) {
            var t = D.tickets.filter(function (x) { return x.id === k; })[0];
            if (t && sel[k]) t.status = 'closed';
          });
          sel = {};
          draw(); UI.toast('已批量关单 ' + n + ' 条');
        }, '执行');
      });
      draw();
    }
  };

  /* ---------------- 组织权限 ---------------- */
  var admin = {
    html: function () {
      return '' +
      '<div class="page-head"><div><h2>组织与权限</h2><p>用户 / 角色 / 部门 / 用户组四级授权，知识库可见性之上还有文档级例外授权。</p></div></div>' +
      '<div class="tabs-line" role="tablist">' +
        ['members:成员', 'roles:角色与 ACL', 'audit:审计日志', 'usage:用量统计', 'sso:SSO 与集成'].map(function (t) {
          var p = t.split(':');
          return '<button role="tab" aria-selected="' + (S.adminTab === p[0]) + '" class="' + (S.adminTab === p[0] ? 'is-on' : '') + '" data-tab="' + p[0] + '" type="button">' + esc(p[1]) + '</button>';
        }).join('') + '</div><div id="adminBody"></div>';
    },
    init: function (root) {
      function draw() {
        var b = root.querySelector('#adminBody');
        if (S.adminTab === 'members') {
          b.innerHTML = '<section class="panel nopad"><table class="tbl"><thead><tr><th>成员</th><th>部门</th><th>角色</th><th>principal 编码</th><th>登录次数</th><th>状态</th><th></th></tr></thead><tbody>' +
            D.members.map(function (m, i) {
              return '<tr><td>' + esc(m.name) + '</td><td>' + esc(m.dept) + '</td><td>' + esc(m.role) + '</td>' +
                '<td class="mono">' + esc(m.principal) + '</td><td class="num">' + m.logins + '</td>' +
                '<td>' + (m.status === 'active' ? badge('正常', 'g') : m.status === 'locked' ? badge('已锁定', 'u') : badge('待审核', 'n')) + '</td>' +
                '<td class="ta-r"><button class="link" data-m="' + i + '" type="button">' + (m.status === 'locked' ? '解锁' : m.status === 'pending' ? '通过审核' : '锁定') + '</button></td></tr>';
            }).join('') + '</tbody></table></section>' +
            '<p class="hint mt">自助注册默认关闭；开启后注册仍需管理员审核。登录限流、连续失败锁定阈值与锁定时长均可在系统设置热改。</p>';
          b.querySelectorAll('[data-m]').forEach(function (btn) {
            btn.addEventListener('click', function () {
              var m = D.members[+btn.getAttribute('data-m')];
              m.status = m.status === 'locked' ? 'active' : m.status === 'pending' ? 'active' : 'locked';
              UI.toast(m.name + ' 当前状态：' + (m.status === 'active' ? '正常' : '已锁定'));
              draw();
            });
          });
        } else if (S.adminTab === 'roles') {
          b.innerHTML = '<section class="panel nopad"><table class="tbl"><thead><tr><th>角色</th><th>人数</th><th>数据范围</th><th>写权限</th><th>说明</th></tr></thead><tbody>' +
            D.roles.map(function (r) {
              return '<tr><td>' + esc(r.name) + '</td><td class="num">' + r.users + '</td><td>' + esc(r.scope) + '</td>' +
                '<td>' + (r.write ? badge('可写', 'g') : badge('只读', 'l')) + '</td><td class="hint">' + esc(r.note) + '</td></tr>';
            }).join('') + '</tbody></table></section>' +
            '<section class="panel"><h3>知识库可见性矩阵</h3><div class="table-wrap"><table class="tbl matrix"><thead><tr><th>角色</th><th>产品与售前资料<br><span class="hint">公开</span></th><th>人事制度库<br><span class="hint">内部</span></th><th>合同与合规模板<br><span class="hint">私有</span></th></tr></thead><tbody>' +
            [['超级管理员', 1, 1, 1], ['运维管理员', 1, 1, 1], ['客服坐席', 1, 1, 0], ['普通成员', 1, 1, 0], ['外部客户', 1, 0, 0]].map(function (row) {
              return '<tr><th>' + esc(row[0]) + '</th>' + row.slice(1).map(function (v) { return '<td class="' + (v ? 'yes' : 'no') + '">' + (v ? '✓ 可检索' : '× 无权') + '</td>'; }).join('') + '</tr>';
            }).join('') + '</tbody></table></div>' +
            '<p class="hint mt">文档级例外授权（allow / deny）叠加在库可见性之上，支持部门向下继承；所有检索的权限条件由 <code>services/permission.py</code> 唯一入口生成。</p></section>';
        } else if (S.adminTab === 'audit') {
          b.innerHTML = '<div class="row-gap mb"><input id="auQ" class="grow" placeholder="按用户 / 动作 / 对象搜索" style="max-width:320px">' +
            '<button class="chip-btn" data-au="all" type="button">全部</button><button class="chip-btn is-on" data-au="deny" type="button">仅拦截</button></div>' +
            '<section class="panel nopad"><table class="tbl"><thead><tr><th>时间</th><th>主体</th><th>动作</th><th>对象</th><th>来源</th><th>结果</th></tr></thead><tbody id="auBody"></tbody></table></section>' +
            '<p class="hint mt">写操作全量审计；检索越权请求同样记录，便于定位配置缺口。</p>';
          var mode = 'all';
          function drawAudit() {
            var kw = (b.querySelector('#auQ').value || '').toLowerCase();
            b.querySelector('#auBody').innerHTML = D.audit.filter(function (a) {
              return (mode === 'all' || a.result === mode) && (!kw || (a.user + a.action + a.target).toLowerCase().indexOf(kw) >= 0);
            }).map(function (a) {
              return '<tr><td class="mono">' + esc(a.time) + '</td><td>' + esc(a.user) + '</td><td class="mono">' + esc(a.action) + '</td>' +
                '<td>' + esc(a.target) + '</td><td class="mono hint">' + esc(a.ip) + '</td>' +
                '<td>' + (a.result === 'ok' ? badge('通过', 'g') : badge('拦截', 'u')) + '</td></tr>';
            }).join('') || '<tr><td colspan="6" class="hint pad">没有匹配记录。</td></tr>';
          }
          b.querySelectorAll('[data-au]').forEach(function (btn) {
            btn.addEventListener('click', function () {
              mode = btn.getAttribute('data-au');
              b.querySelectorAll('[data-au]').forEach(function (x) { x.classList.toggle('is-on', x === btn); });
              drawAudit();
            });
          });
          b.querySelector('#auQ').addEventListener('input', drawAudit);
          drawAudit();
        } else if (S.adminTab === 'usage') {
          var max = Math.max.apply(null, D.usage.series[0].values);
          b.innerHTML = '<div class="kpis">' +
            cardStat('近 7 天 token', '4,419k', '3 个模型') + cardStat('问答次数', '1,583', '平均 2.8 轮') +
            cardStat('工具调用', '11,548', '成功率 97.8%') + cardStat('缓存命中', '31.4%', '节省 1,389k') + '</div>' +
            '<section class="panel"><h3>按模型聚合</h3><ul class="bars">' + D.usage.series.map(function (s) {
              var total = s.values.reduce(function (a, v) { return a + v; }, 0);
              return '<li><span class="bar-name mono">' + esc(s.name) + '</span><span class="bar-track"><i style="width:' + (total / 1400 * 100) + '%;background:' + s.color + '"></i></span><span class="bar-val mono">' + total + 'k</span></li>';
            }).join('') + '</ul><p class="hint mt">max=' + max + 'k（单日峰值）。embedding 消耗与入库量正相关，重灌会重复计费。</p></section>' +
            '<div class="cols c1-1"><section class="panel"><h3>API 密钥</h3><p class="hint">OpenAI 兼容接口，供外部系统调用平台问答与检索。</p>' +
            '<div class="code-box"><code class="mono">POST /v1/chat/completions</code><code class="mono">Authorization: Bearer sk-rag-****…****</code></div>' +
            '<button class="btn btn-line sm mt" id="keyNew" type="button">生成新密钥</button></section>' +
            '<section class="panel"><h3>定时任务</h3><ul class="ticks tight"><li>cron：每日 09:00 汇总昨日工单并邮件推送</li><li>依赖链：文档入库成功 → 自动触发评测集回归</li><li>事件触发：新建工单 → 通知客服组长（HMAC 签名回调）</li></ul>' +
            '<p class="hint mt">失败自动重试，执行产物随通知附带（如报表作为邮件附件）。</p></section></div>';
          b.querySelector('#keyNew').addEventListener('click', function () { UI.toast('新密钥已生成，完整值仅展示一次'); });
        } else {
          b.innerHTML = '<section class="panel"><h3>单点登录</h3><p class="hint">配置并启用后，登录页自动出现对应按钮；未配置时该区域不显示，不影响账号密码登录。</p>' +
            '<div class="sso-grid">' + [['通用 OIDC', '已启用', 'g'], ['企业微信', '未配置', 'l'], ['钉钉（OIDC + PKCE）', '已启用', 'g'], ['飞书（OIDC + PKCE）', '未配置', 'l']].map(function (x) {
              return '<div class="sso-item"><b>' + esc(x[0]) + '</b>' + badge(x[1], x[2]) + '<button class="link" type="button">配置</button></div>';
            }).join('') + '</div>' +
            '<p class="hint mt">首次登录可自动建号（JIT）并按配置分配默认角色。</p></section>' +
            '<div class="cols c1-1"><section class="panel"><h3>安全开关</h3>' +
            sw(true, '开放自助注册', 'x1') + '<br>' + sw(true, '注册后需管理员审核', 'x2') + '<br>' + sw(true, '内容安全检测', 'x3') +
            '<div class="kv mt"><span>登录限流</span><code class="mono">10 次/分钟/IP</code></div>' +
            '<div class="kv"><span>连续失败锁定</span><code class="mono">5 次 → 锁定 30 分钟</code></div></section>' +
            '<section class="panel"><h3>MCP 服务器</h3><p class="hint">支持 http / sse / stdio 三类，接入后其工具自动注册为 AI 工具。</p>' +
            '<table class="tbl mini"><tbody><tr><td class="mono">filesystem-mcp</td><td>stdio</td><td>' + badge('已连接', 'g') + '</td></tr>' +
            '<tr><td class="mono">amap-mcp</td><td>sse</td><td>' + badge('已连接', 'g') + '</td></tr>' +
            '<tr><td class="mono">internal-crm</td><td>http</td><td>' + badge('认证失败', 'u') + '</td></tr></tbody></table>' +
            '<button class="btn btn-line sm mt" id="mcpAdd" type="button">+ 添加服务器</button></section></div>';
          b.querySelectorAll('[data-sw]').forEach(function (x) {
            x.addEventListener('click', function () { x.classList.toggle('on'); x.setAttribute('aria-checked', x.classList.contains('on') ? 'true' : 'false'); });
          });
          b.querySelector('#mcpAdd').addEventListener('click', function () { UI.toast('MCP 配置需填写服务器命令与鉴权信息'); });
        }
      }
      root.querySelectorAll('[data-tab]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          S.adminTab = btn.getAttribute('data-tab');
          root.querySelectorAll('[data-tab]').forEach(function (x) { x.classList.toggle('is-on', x === btn); x.setAttribute('aria-selected', String(x === btn)); });
          draw();
        });
      });
      draw();
    }
  };

  return {
    workbench: workbench, chat: chat, kb: kb, retrieval: retrieval, agents: agents,
    workflow: workflow, tickets: tickets, admin: admin
  };
})();
