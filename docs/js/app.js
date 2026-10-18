/* 应用外壳：菜单、路由、全局交互 */
(function () {
  'use strict';
  var S = window.STATE, D = window.DEMO, esc = UI.esc;

  var MENU = [
    { g: '工作台' },
    { t: '工作台', h: '#/workbench' },
    { t: '智能问答', h: '#/chat' },
    { t: '企业聊天', soon: true },
    { g: '业务' },
    { t: '知识库', h: '#/kb' },
    { t: '检索调试台', h: '#/retrieval' },
    { t: '客服工单', h: '#/tickets' },
    { t: '智能录单', soon: true },
    { t: '待办日程', soon: true },
    { t: '审批中心', soon: true },
    { g: '智能体' },
    { t: '智能体', h: '#/agents' },
    { t: '工作流编排', h: '#/workflow' },
    { t: '技能市场', h: '#/agents?tab=skills' },
    { t: '工具库', h: '#/agents?tab=tools' },
    { t: 'MCP 与外部集成', h: '#/admin?tab=sso' },
    { t: '定时任务', soon: true },
    { t: '外部渠道', soon: true },
    { g: '管理' },
    { t: '组织权限', h: '#/admin' },
    { t: '安全与审计', h: '#/admin?tab=audit' },
    { t: '用量统计', h: '#/admin?tab=usage' },
    { t: '系统设置', soon: true },
    { g: '其他' },
    { t: '源码仓库', ext: 'https://github.com/241793/RAG-Agent' }
  ];

  var TITLES = {
    '/workbench': '工作台', '/chat': '智能问答', '/kb': '知识库', '/retrieval': '检索调试台',
    '/agents': '智能体平台', '/workflow': '工作流编排', '/tickets': '客服工作台', '/admin': '组织与权限'
  };

  var side = document.getElementById('side');
  var menu = document.getElementById('menu');

  menu.innerHTML = MENU.map(function (m) {
    if (m.g) return '<li class="menu-group">' + esc(m.g) + '</li>';
    if (m.soon) return '<li><button type="button" class="soon" title="演示站未覆盖该页">' + esc(m.t) + '<span>未覆盖</span></button></li>';
    if (m.ext) return '<li><a href="' + m.ext + '" target="_blank" rel="noopener">' + esc(m.t) + '<span>↗</span></a></li>';
    return '<li><a href="' + m.h + '" data-h="' + m.h + '">' + esc(m.t) + '</a></li>';
  }).join('');

  menu.querySelectorAll('.soon').forEach(function (b) {
    b.addEventListener('click', function () {
      UI.toast('「' + b.textContent.replace('未覆盖', '') + '」在演示站内未实现，真实产品已提供该模块');
    });
  });

  function route() {
    var raw = (location.hash || '#/workbench').slice(1);
    var path = raw.split('?')[0];
    var q = (raw.split('?')[1] || '').replace('tab=', '');
    var view = TITLES[path] ? path : '/workbench';
    var el = document.getElementById('view');

    UI.closeDrawer();
    el.innerHTML = '';
    el.scrollTop = 0;
    el.classList.toggle('is-chat', view === '/chat');
    var v = window.VIEWS[view.slice(1)];
    if (!v) return;
    if (q && view === '/admin') S.adminTab = q;
    if (q && view === '/agents') S.agentTab = q;
    el.innerHTML = v.html();
    v.init(el);

    document.getElementById('crumb').textContent = '工作台 / ' + TITLES[view];
    menu.querySelectorAll('a[data-h]').forEach(function (a) {
      a.classList.toggle('is-on', a.getAttribute('data-h') === '#/' + raw);
    });
    side.classList.remove('is-mobile-open');
  }
  window.addEventListener('hashchange', route);

  document.getElementById('collapseBtn').addEventListener('click', function () {
    var on = side.classList.toggle('is-collapsed');
    document.getElementById('app').classList.toggle('is-collapsed', on);
    this.textContent = on ? '»' : '收起侧边栏';
  });
  document.getElementById('hamb').addEventListener('click', function () {
    side.classList.toggle('is-mobile-open');
  });

  document.getElementById('newsBar').addEventListener('click', showChangelog);
  document.getElementById('newsBar').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showChangelog(); }
  });
  document.getElementById('updateBtn').addEventListener('click', showChangelog);

  function showChangelog() {
    UI.drawer('关于 / 检查更新 · 当前 v1.0.1',
      '<p class="hint">更新内容来源：<code>CHANGELOG.md</code> → CI 提取版本段落 → GitHub Release 正文 → 应用内弹窗与顶部滚动条，三处自动一致。</p>' +
      D.changelog.map(function (c) {
        return '<div class="cl-item"><header><b class="mono">' + esc(c.ver) + '</b><span class="hint mono">' + esc(c.date) + '</span>' +
          (c.ver === 'v1.0.1' ? badgeNow() : '') + '</header><ul class="ticks tight">' +
          c.items.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('') + '</ul></div>';
      }).join('') +
      '<p class="hint mt">桌面版：下载 → sha256 校验 → 换盘 → 自动重启，失败还原备份并启动旧版。<br>Docker：覆盖 <code>/app</code> 后 <code>pip install</code>，由 compose 拉起，数据目录不受影响。</p>');
  }
  function badgeNow() { return '<span class="badge-g">当前版本</span>'; }

  document.getElementById('bellBtn').addEventListener('click', function () {
    UI.drawer('通知中心', '<ul class="noti">' +
      '<li><b>工单 TK-2086 已超时 18 分钟</b><span class="hint">紧急 · 设备科</span></li>' +
      '<li><b>财务与报销库 2 个文档解析失败</b><span class="hint">可重灌或改用递归分块</span></li>' +
      '<li><b>定时任务「日报汇总」执行成功</b><span class="hint">产物 report.xlsx 已随邮件附件发出</span></li>' +
      '</ul>');
  });

  route();
})();
