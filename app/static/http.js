/* 统一请求包装（2026-10-09）
 *
 * **为什么要有**：后端一出错（500 / 网关 502 / 上传被截断），响应可能是一页 HTML，
 * 而页面上到处都是 `r.json()` —— 一解析就抛 `Unexpected token '<'`，
 * 用户看到的只有「上传失败，请重试」，真正的原因（HTTP 状态码、响应原文）全丢了。
 *
 * 这里做三件事：
 *   1. 先 `text()`，再按 content-type 决定是否 `JSON.parse`（不盲目 `r.json()`）；
 *   2. 不是 JSON 时把「状态码 + 响应前 200 字」**上报后端**（带账号与 IP，可查）；
 *   3. 抛出人话 Error（`HTTP 500：服务器处理出错…`），调用方 toast 出来即可。
 *
 * 用法：`fetchJson(url, { method:'POST', body: fd })`
 *   → 成功 `{ ok:true, status:200, data:{...} }`；失败抛 Error（`err.status` / `err.data`）
 */
(function () {
  var MAX_SNIPPET = 200;
  var MAX_REPORTS = 5;          // 一页最多上报 5 条，避免脚本连环报错把日志刷爆
  var reports = 0;

  function stripHtml(text) {
    return String(text || '')
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]*>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function snippet(text) {
    var t = stripHtml(text);
    return t.length > MAX_SNIPPET ? t.slice(0, MAX_SNIPPET) + '…' : t;
  }

  function report(info) {
    if (reports >= MAX_REPORTS) return;
    reports += 1;
    try {
      fetch('/api/client-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(info),
        keepalive: true
      });
    } catch (e) { /* 上报本身失败就算了，不能因为上报再崩一次 */ }
  }

  function fetchJson(url, options) {
    var opts = options || {};
    var where = String(opts.where || url);
    return fetch(url, opts).then(function (resp) {
      return resp.text().then(function (text) {
        var type = (resp.headers.get('content-type') || '').toLowerCase();
        var data = null;
        if (type.indexOf('json') >= 0 && text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!data) {
          var why = snippet(text) || '（响应是空的）';
          report({
            url: where,
            status: resp.status,
            body: String(text || '').slice(0, 600),
            message: '页面收到非 JSON 响应（HTTP ' + resp.status + '）'
          });
          var err = new Error(
            (resp.status === 502 || resp.status === 504)
              ? '服务器正在重启或网关超时（HTTP ' + resp.status + '），等十几秒再试一次'
              : '服务器返回了意外内容（HTTP ' + resp.status + '）：' + why
          );
          err.status = resp.status;
          err.data = { url: where, status: resp.status, body: why };
          throw err;
        }
        return { ok: resp.ok, status: resp.status, data: data };
      });
    });
  }

  // 页面脚本自己抛的错也报一条：不然「点了没反应」只能靠猜
  window.addEventListener('error', function (e) {
    if (!e || !e.message) return;
    report({
      url: window.location.pathname + window.location.search,
      status: 0,
      body: (e.message || '') + ' @ ' + (e.filename || '') + ':' + (e.lineno || 0),
      message: '页面脚本报错：' + e.message
    });
  });

  window.fetchJson = fetchJson;
  window.reportClientError = report;
})();
