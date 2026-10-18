/* 通用 UI 原语：转义、提示条、抽屉、确认框 */
window.UI = (function () {
  'use strict';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(msg, kind) {
    var host = document.getElementById('toasts');
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' toast-' + kind : '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(function () {
      el.classList.add('is-out');
      setTimeout(function () { el.remove(); }, 260);
    }, 2600);
  }

  var drawerEl = null;
  function drawer(title, bodyHtml, footHtml) {
    closeDrawer();
    var d = document.createElement('div');
    d.className = 'drawer';
    d.innerHTML = '<header><h3>' + esc(title) + '</h3><button class="x" type="button" aria-label="关闭">×</button></header>' +
      '<div class="drawer-body">' + bodyHtml + '</div>' + (footHtml ? '<footer class="drawer-foot">' + footHtml + '</footer>' : '');
    document.getElementById('layer').appendChild(d);
    var mask = document.createElement('div');
    mask.className = 'mask';
    document.getElementById('layer').appendChild(mask);
    drawerEl = { root: d, mask: mask };
    requestAnimationFrame(function () { d.classList.add('is-in'); mask.classList.add('is-in'); });
    d.querySelector('.x').addEventListener('click', closeDrawer);
    mask.addEventListener('click', closeDrawer);
    return d;
  }

  function closeDrawer() {
    if (!drawerEl) return;
    var e = drawerEl;
    drawerEl = null;
    e.root.classList.remove('is-in');
    e.mask.classList.remove('is-in');
    setTimeout(function () { e.root.remove(); e.mask.remove(); }, 240);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeDrawer(); closeConfirm(); }
  });

  var confirmEl = null;
  function confirmDialog(title, bodyHtml, onYes, yesLabel) {
    var box = document.createElement('div');
    box.className = 'modal';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.innerHTML = '<h3>' + esc(title) + '</h3><div class="modal-body">' + bodyHtml + '</div>' +
      '<div class="modal-actions"><button class="btn btn-line" data-no>取消</button><button class="btn btn-solid" data-yes>' + esc(yesLabel || '确认执行') + '</button></div>';
    var mask = document.createElement('div');
    mask.className = 'mask is-in';
    document.getElementById('layer').appendChild(mask);
    document.getElementById('layer').appendChild(box);
    confirmEl = { root: box, mask: mask };
    function done() { closeConfirm(); }
    box.querySelector('[data-no]').addEventListener('click', done);
    mask.addEventListener('click', done);
    box.querySelector('[data-yes]').addEventListener('click', function () { done(); onYes(); });
    box.querySelector('[data-yes]').focus();
  }

  function closeConfirm() {
    if (!confirmEl) return;
    var e = confirmEl;
    confirmEl = null;
    e.root.remove(); e.mask.remove();
  }

  function pct(v) { return (v * 100).toFixed(1) + '%'; }
  function bar(v, max) { return Math.max(2, Math.min(100, (v / (max || 1)) * 100)) + '%'; }

  return { esc: esc, toast: toast, drawer: drawer, closeDrawer: closeDrawer, confirm: confirmDialog, pct: pct, bar: bar };
})();
