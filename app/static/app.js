// 通用交互：全选、批量操作确认、解析测试、异步同步
document.addEventListener('DOMContentLoaded', function () {
  // 数据总览：指标数字滚动动画
  initCounters();

  // 移动端：点击侧边栏导航后自动收起抽屉
  document.querySelectorAll('.sidebar nav a').forEach(function (link) {
    link.addEventListener('click', function () {
      var toggle = document.getElementById('navToggle');
      if (toggle) toggle.checked = false;
    });
  });

  // 汉堡按钮：Enter / Space 同样可开合抽屉
  var burgerBtn = document.querySelector('.topbar-burger');
  if (burgerBtn) {
    burgerBtn.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      var toggle = document.getElementById('navToggle');
      if (toggle) toggle.checked = !toggle.checked;
    });
  }

  // 抽屉支持 Escape 关闭
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    var toggle = document.getElementById('navToggle');
    if (toggle && toggle.checked) {
      toggle.checked = false;
      var burger = document.querySelector('.topbar-burger');
      if (burger) burger.focus();
    }
  });

  // 订单列表：鼠标移入 / 键盘聚焦缩略图时，旁边弹出该订单的截图原图
  document.querySelectorAll('.shot').forEach(function (box) {
    var preview = box.querySelector('.shot-preview');
    if (!preview) return;

    function place() {
      var rect = box.getBoundingClientRect();
      var left = rect.right + 12;
      if (left + preview.offsetWidth > window.innerWidth - 8) {
        left = Math.max(rect.left - preview.offsetWidth - 12, 8);
      }
      var top = Math.min(rect.top - 8, window.innerHeight - preview.offsetHeight - 12);
      preview.style.left = left + 'px';
      preview.style.top = Math.max(top, 8) + 'px';
    }
    function show() {
      preview.classList.add('on');
      place();
    }
    function hide() {
      preview.classList.remove('on');
    }

    box.addEventListener('mouseenter', show);
    box.addEventListener('mouseleave', hide);
    box.addEventListener('focusin', show);
    box.addEventListener('focusout', hide);
    window.addEventListener('scroll', hide, { passive: true });
  });

  document.querySelectorAll('[data-toggle-all]').forEach(function (master) {
    master.addEventListener('change', function () {
      var scope = document.querySelector(master.dataset.toggleAll) || document;
      scope.querySelectorAll('input[name="order_ids"]').forEach(function (box) {
        box.checked = master.checked;
      });
    });
  });

  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (!confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  var testForm = document.getElementById('parse-test-form');
  if (testForm) {
    testForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      var out = document.getElementById('parse-test-result');
      out.textContent = '解析中...';
      var body = JSON.stringify({
        text: testForm.querySelector('[name=text]').value,
        platform: testForm.querySelector('[name=platform]').value || ''
      });
      const resp = await fetch('/api/parse-test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body
      });
      const data = await resp.json();
      out.innerHTML = renderParse(data);
    });
  }

  // 「是否转账」切换后立即保存并刷新推单文案，保证复制出去的是最新文案
  var transferSel = document.getElementById('transfer-select');
  if (transferSel && transferSel.dataset.transferUrl) {
    transferSel.addEventListener('change', async function () {
      var hint = document.getElementById('push-hint');
      var original = transferSel.dataset.transferLabel || '';
      transferSel.disabled = true;
      if (hint) hint.textContent = '正在更新推单文案...';
      try {
        var body = new URLSearchParams();
        body.append('transfer_status', transferSel.value);
        const resp = await fetch(transferSel.dataset.transferUrl, {
          method: 'POST',
          headers: {
            'X-Requested-With': 'fetch',
            'Content-Type': 'application/x-www-form-urlencoded'
          },
          body: body
        });
        const data = await resp.json();
        if (!data.ok) throw new Error(data.error || '更新失败');
        var target = document.getElementById('push-text');
        if (target) target.textContent = data.text;
        var chip = document.getElementById('transfer-chip');
        if (chip) chip.textContent = '是否转账：' + data.transfer_label;
        transferSel.dataset.transferLabel = data.transfer_label;
        if (hint) {
          hint.textContent = '已更新为「' + data.transfer_label + '」，上面的推单文案就是最新版本，直接复制即可。';
        }
      } catch (err) {
        if (hint) hint.textContent = '更新失败：' + err.message + '（已还原为「' + original + '」）';
        transferSel.value = original === '已转账' ? 'paid' : 'unpaid';
      } finally {
        transferSel.disabled = false;
      }
    });
  }

  // 推单信息复制：复制后（可选）把订单标记为已推单
  // 说明：http 下 navigator.clipboard 不可用，统一走 textarea + execCommand 兜底
  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      var target = document.querySelector(btn.dataset.copy);
      if (!target) return;
      if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
      var sel = document.getElementById('transfer-select');
      var text = target.textContent;
      if (sel) {
        text = text.replace(
          /是否转账：(已转账|未转账)/g,
          '是否转账：' + sel.options[sel.selectedIndex].text
        );
      }
      if (!copyToClipboard(text)) {
        alert('复制失败，请手动选中推单信息后复制。');
        return;
      }
      var url = btn.dataset.pushUrl;
      if (url) {
        try {
          var body = new URLSearchParams();
          if (sel) body.append('transfer_status', sel.value);
          const resp = await fetch(url, {
            method: 'POST',
            headers: {
              'X-Requested-With': 'fetch',
              'Content-Type': 'application/x-www-form-urlencoded'
            },
            body: body
          });
          const data = await resp.json();
          if (!data.ok) throw new Error(data.error || '标记失败');
        } catch (err) {
          alert('已复制，但「标记已推单」失败：' + err.message);
          return;
        }
      }
      var original = btn.textContent;
      btn.textContent = url ? '已复制并标记已推单' : '已复制';
      btn.disabled = true;
      setTimeout(function () { location.reload(); }, 600);
      setTimeout(function () { btn.textContent = original; btn.disabled = false; }, 2000);
    });
  });

  document.querySelectorAll('[data-sync]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      btn.disabled = true;
      var original = btn.textContent;
      btn.textContent = '同步中...';
      try {
        const resp = await fetch('/api/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ days: Number(btn.dataset.days || 1) })
        });
        const data = await resp.json();
        alert(JSON.stringify(data.results || data, null, 2).slice(0, 1200));
        location.reload();
      } catch (err) {
        alert('同步失败: ' + err);
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  });
});

// 指标数字滚动：<span data-count="1234" data-money="1">
function initCounters() {
  var nodes = document.querySelectorAll('[data-count]');
  if (!nodes.length) return;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  nodes.forEach(function (el) {
    var target = parseFloat(el.getAttribute('data-count'));
    if (isNaN(target)) return;
    var money = el.getAttribute('data-money') === '1';
    var decimals = money ? 2 : Number(el.getAttribute('data-decimals') || 0);
    var prefix = money ? '¥' : (el.getAttribute('data-prefix') || '');
    var suffix = el.getAttribute('data-suffix') || '';
    function fmt(value) {
      var text = value.toLocaleString('zh-CN', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
      return prefix + text + suffix;
    }
    if (reduce || target === 0) {
      el.textContent = fmt(target);
      return;
    }
    var duration = 900;
    var start = null;
    el.textContent = fmt(0);
    function step(ts) {
      if (start === null) start = ts;
      var p = Math.min((ts - start) / duration, 1);
      var eased = 1 - Math.pow(1 - p, 3);   // easeOutCubic
      el.textContent = fmt(target * eased);
      if (p < 1) requestAnimationFrame(step);
      else el.textContent = fmt(target);
    }
    requestAnimationFrame(step);
  });
}

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(function () { legacyCopy(text); });
    return true;
  }
  return legacyCopy(text);
}

function legacyCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

function renderParse(data) {
  if (!data || data.error) return '<div class="note danger">解析失败：' + (data && data.error) + '</div>';
  var rows = Object.keys(data.fields || {}).map(function (k) {
    return '<tr><td>' + k + '</td><td>' + data.fields[k] + '</td><td class="muted">' +
      ((data.sources || {})[k] || '') + '</td></tr>';
  }).join('');
  return '<table><thead><tr><th>字段</th><th>值</th><th>来源</th></tr></thead><tbody>' + rows +
    '</tbody></table><div class="muted" style="margin-top:8px">平台：' + (data.platform || '-') +
    ' · 置信度：' + (data.confidence ?? '-') + ' · 缺失必填：' +
    ((data.missing_required || []).join('、') || '无') + '</div>';
}

/* --------------------------------------------------------------- 自动刷新
   停在「数据总览」/「订单归档」时定时拉取最新数据，默认 60 秒（系统设置页可改）。
   三层保护，尽量不给服务器添堵、也不打断人干活：
     1 切到别的标签页（页面不可见）不发请求，切回来才继续；
     2 正在输入 / 已经勾了行（比如明细里勾选商品准备合并）不刷新，15 秒后再看；
     3 上一次加载超过 1.5 秒就把间隔翻倍（最多 5 分钟一次）：
       数据变多、服务器吃力时前端自己退让。 */
(function () {
  var body = document.querySelector('body[data-auto-refresh]');
  if (!body) return;
  var base = parseInt(body.getAttribute('data-auto-refresh'), 10) || 0;
  if (base < 10) return;

  var MAX_GAP = 300;   // 自适应放慢的上限：5 分钟
  var SLOW_MS = 1500;  // 超过这个耗时算「服务器有点扛不住」
  var RETRY = 15;      // 正在填东西时，多久后再看一眼
  var gap = Math.min(base, MAX_GAP);
  var timer = null, counter = null;
  var label = document.getElementById('autoRefreshText');

  function say(text) { if (label) label.textContent = text; }

  function busy() {
    var el = document.activeElement;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) {
      return true;
    }
    return !!document.querySelector('input[name="ids"]:checked, input[name="members"]:checked');
  }

  function again(sec) {
    clearTimeout(timer);
    clearInterval(counter);
    var left = Math.round(sec);
    say(left + 's 后刷新');
    counter = setInterval(function () {
      left -= 1;
      if (left > 0) say(left + 's 后刷新');
    }, 1000);
    timer = setTimeout(tick, sec * 1000);
  }

  function tick() {
    if (document.hidden) { say('已暂停'); return again(30); }  // 不在前台：不刷新
    if (busy()) { say('稍后刷新'); return again(RETRY); }
    say('刷新中…');
    location.reload();
  }

  var nav = performance.getEntriesByType ? performance.getEntriesByType('navigation')[0] : null;
  if (nav && nav.duration > SLOW_MS) gap = Math.min(MAX_GAP, gap * 2);

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) again(gap);  // 回到前台：立刻按正常节奏来
  });
  again(gap);
})();

// 通用弹窗（流程说明等）：data-open-modal="弹窗 id" 打开，data-close-modal 关闭
// 用原生 <dialog>：自带 ESC 关闭与焦点管理，不支持的浏览器退回 open 属性
(function () {
  function open(id) {
    var d = document.getElementById(id);
    if (!d) return;
    if (typeof d.showModal === 'function') { d.showModal(); } else { d.setAttribute('open', ''); }
  }
  function close(d) { if (!d) return; if (typeof d.close === 'function') { d.close(); } else { d.removeAttribute('open'); } }

  document.addEventListener('click', function (e) {
    var opener = e.target.closest ? e.target.closest('[data-open-modal]') : null;
    if (opener) { open(opener.getAttribute('data-open-modal')); return; }
    var closer = e.target.closest ? e.target.closest('[data-close-modal]') : null;
    if (closer) { close(closer.closest('dialog')); return; }
    // 点弹窗外的阴影区 = 关闭
    if (e.target.tagName === 'DIALOG') { close(e.target); }
  });
})();
