/* 对话编排：ReAct 计划 + 基于本地检索结果的回答拼装（演示用，不调用任何模型） */
window.CHAT = (function () {
  'use strict';

  var WRITE = { doc_generate: 1, todo_manage: 1, ticket_create: 1, email_send: 1 };

  var intents = [
    {
      id: 'mail', match: /(邮件|发信|邮箱|发给|回复给)/,
      label: '邮件', tools: ['find_tools', 'email_send'],
      intro: '邮件草稿已生成，发信需要你确认后执行：'
    },
    {
      id: 'doc', match: /(生成|起草|撰写|写|做|整理|导出|转成)[^。，]{0,8}(纪要|周报|日报|月报|公文|通知|报告|文档|PPT|表格)/,
      label: '办公文书', tools: ['find_tools', 'kb_search', 'doc_generate'],
      intro: '已按模板生成文档，排版含封面、页眉页脚与目录：'
    },
    {
      id: 'ticket', match: /(工单|报修|投诉|转人工|客户)/,
      label: '客服工单', tools: ['find_tools', 'kb_search', 'ticket_create'],
      intro: '已按渠道消息生成工单并分派：'
    },
    {
      id: 'todo', match: /(待办|日程|提醒|安排|约个会)/,
      label: '待办日程', tools: ['find_tools', 'todo_manage'],
      intro: '已创建待办并设置到期提醒：'
    }
  ];

  function detect(q) {
    for (var i = 0; i < intents.length; i++) if (intents[i].match.test(q)) return intents[i];
    return null;
  }

  function firstSentence(text) {
    var m = text.split(/。/);
    return (m[0] || text).trim();
  }

  function plan(query, opts, index) {
    query = (query || '').trim();
    var out = { query: query, steps: [], answer: '', citations: [], tokens: 0, kind: 'qa' };

    if (/^\/lang/.test(query)) {
      out.kind = 'command';
      out.steps = [{ kind: 'command', name: '/lang', detail: '已将本会话回答语言切换为 English（按渠道默认语言优先）' }];
      out.answer = 'Language mode switched. This session will answer in English; channel default language still takes precedence.';
      out.tokens = 180;
      return out;
    }

    var intent = detect(query);
    var aiOn = opts.ai !== false;
    var tools = intent && aiOn ? intent.tools : ['kb_search'];

    if (intent && !aiOn) {
      out.kind = 'blocked';
      out.steps = [{ kind: 'route', name: 'find_tools', detail: 'AI 操作已关闭，写操作类工具未装配' }];
    } else if (intent) {
      out.steps.push({ kind: 'route', name: 'find_tools', detail: '命中「' + intent.label + '」意图，装配 ' + intent.tools.length + ' 个工具' });
    }

    var res = SEARCH.run(index, query, { topK: 6, mmr: opts.mmr !== false });
    var all = res.results;
    var max = all.reduce(function (a, r) { return Math.max(a, r.vec); }, 0);
    var hits = max >= 0.16 ? all.filter(function (r) { return !r.dropped && r.vec >= max * 0.62; }).slice(0, 3) : [];

    out.steps.push({
      kind: 'tool', name: 'kb_search',
      args: { query: query, top_k: 6, fusion: 'vector+bm25+rrf', rerank: !!opts.rerank },
      detail: hits.length ? '召回 ' + hits.length + ' 条高相关分块（已按权限过滤）' : '无高相关命中，将转用模型自身知识',
      ms: 260 + Math.round(res.results.length * 12)
    });

    var parts = [];
    hits.forEach(function (h, i) {
      out.citations.push({ n: i + 1, doc: h.chunk.doc, kb: h.chunk.kb, text: h.chunk.text, vec: h.vec, kw: h.kw, rrf: h.rrf });
      parts.push(firstSentence(h.chunk.text) + '。[' + (i + 1) + ']');
    });

    if (intent && aiOn) {
      parts.unshift(opts.autoConfirm ? intent.intro.replace('需要你确认后执行', '已按免确认策略直接执行') : intent.intro);
      var extra = {
        doc: '文件：会议纪要_2026W42.docx（2 页 · 含封面、页眉页脚与目录）',
        ticket: '工单号 TK-2088 · 优先级 high · SLA 4h · 已通知客服组长',
        todo: '待办：跟进上述事项 · 提醒 09:30 · 重复规则 每周一至周五',
        mail: '收件人 陈经理 · 抄送 财务部 · 主题 关于含税报价与开票安排'
      }[intent.id];
      out.steps.push({
        kind: 'tool', name: intent.tools[intent.tools.length - 1],
        args: { source: 'conversation', format: intent.id === 'doc' ? 'docx' : 'record' },
        detail: extra, ms: intent.id === 'doc' ? 1860 : 320,
        confirm: !!WRITE[intent.tools[intent.tools.length - 1]] && !opts.autoConfirm
      });
      parts.push('\n' + extra);
      if (!hits.length) parts.push('\n本次未检索到知识库资料，内容取自模板默认值与模型自身知识。');
    } else if (!hits.length) {
      out.citations = [];
      out.kind = 'miss';
      out.tokens = 240;
      out.answer = (intent && !aiOn)
        ? '「' + intent.label + '」需要调用写操作工具，但当前 AI 操作已关闭。打开问答页顶部的「AI 操作」开关后重试；关闭状态下只保留知识库检索与回答。'
        : '知识库中没有检索到与该问题高相关的内容。为避免幻觉，这里给出的是模型自身知识而非企业资料，建议补充对应文档或用「检索调试台」确认召回情况。\n\n可以试试问我：年假怎么算、差旅住宿标准是多少、私有化部署怎么报价、E12 报警怎么处理。';
      return out;
    }

    if (intent && !aiOn) parts.push('\n（AI 操作已关闭，本次未执行「' + intent.label + '」相关写操作。）');
    if (hits.length) parts.push('\n以上结论来自企业知识库资料（点击编号查看原文分块），非模型自身知识。');
    out.answer = parts.join('\n');
    out.tokens = 900 + query.length * 6 + hits.reduce(function (a, h) { return a + h.chunk.text.length; }, 0);
    return out;
  }

  return { plan: plan, intents: intents };
})();
