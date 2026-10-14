// 语音播报：轮询后台事件并用浏览器 TTS 朗读
// - 事件来源：/api/voice/pending（新订单 / 供应商发货）
// - 播报参数（音量、语速、音调、语言、文案）在系统设置页配置，服务端下发
// - 浏览器要求先有用户交互才能出声，右下角按钮点一次即解锁（状态记在 localStorage）
(function () {
  var POLL_MS = 15000;
  var LAST_ID_KEY = 'oh_voice_last_id';
  var ON_KEY = 'oh_voice_on';

  var cfg = null;
  var lastId = 0;
  var timer = null;
  var beat = null;
  var fab = null;
  var toastTimer = null;

  // 同一个浏览器常开着好几个标签页：每个页面都在轮询，同一条事件就会被念好几遍
  // （听到的「2 次或多次」就是这么来的）。用 localStorage 心跳选一个「出声」的页面，
  // 其余页面只跟着把水位往前推，不再出声。
  var LEADER_KEY = 'oh_voice_leader';
  var LEADER_TTL = 9000;   // 心跳间隔的两倍多：标签页被冻结后别的页面能接上
  var BEAT_MS = 4000;
  var leaderId = Math.random().toString(36).slice(2) + Date.now().toString(36);

  function supported() {
    return typeof window.speechSynthesis !== 'undefined' &&
      typeof window.SpeechSynthesisUtterance !== 'undefined';
  }

  function voices() {
    try {
      return window.speechSynthesis.getVoices() || [];
    } catch (err) {
      return [];
    }
  }

  function pickVoice() {
    var list = voices();
    if (!list.length) return null;
    var lang = (cfg && cfg.lang) || 'zh-CN';
    var wanted = (cfg && cfg.voice_name) || '';
    if (wanted) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].name === wanted) return list[i];
      }
    }
    for (var j = 0; j < list.length; j++) {
      if ((list[j].lang || '').toLowerCase().indexOf(lang.toLowerCase()) === 0) return list[j];
    }
    for (var k = 0; k < list.length; k++) {
      if ((list[k].lang || '').toLowerCase().indexOf('zh') === 0) return list[k];
    }
    return null;
  }

  function utter(text) {
    var u = new SpeechSynthesisUtterance(text);
    u.lang = (cfg && cfg.lang) || 'zh-CN';
    u.volume = cfg ? cfg.volume : 1;
    u.rate = cfg ? cfg.rate : 1;
    u.pitch = cfg ? cfg.pitch : 1;
    var voice = pickVoice();
    if (voice) u.voice = voice;
    return u;
  }

  function cfgOf() {
    return cfg || {};
  }

  function clipUrl(kind) {
    var clips = cfgOf().clips || {};
    return (kind && clips[kind]) || '';
  }

  function repeatTimes() {
    return parseInt(cfgOf().repeat, 10) || 1;
  }

  // 自定义录音按顺序播放：多条事件接连到来时不互相打断
  var audio = null;
  var audioQueue = [];
  var audioPlaying = false;
  var audioVolume = 1;

  function drainAudio() {
    if (audioPlaying || !audioQueue.length) return;
    var job = audioQueue.shift();
    audioPlaying = true;
    try {
      if (!audio) audio = new Audio();
      audio.volume = job.volume;
      audio.src = job.url;
      audio.currentTime = 0;
    } catch (err) {
      audioPlaying = false;
      if (job.fallback) speak(job.fallback);
      return;
    }
    var finish = function (ok) {
      audioPlaying = false;
      if (!ok && job.fallback) speak(job.fallback);  // 播放器异常（格式不支持等）回落到合成音
      drainAudio();
    };
    audio.onended = function () { finish(true); };
    audio.onerror = function () { finish(false); };
    var played = audio.play();
    if (played && typeof played.catch === 'function') {
      played.catch(function () {
        // 浏览器自动播放限制：页面还没被点过一次，先提示去点右下角的开启按钮
        audioPlaying = false;
        if (job.fallback) speak(job.fallback);
        toast('音频被浏览器拦住了：点一次右下角「🔇 语音播报未开启」按钮解锁后再来', true);
        drainAudio();
      });
    }
  }

  /** 播放自定义录音（重复 N 遍）；失败或没配录音时回落到浏览器朗读 */
  function playClip(url, text, times) {
    if (!url) return false;
    var repeat = times || repeatTimes();
    var volume = cfg ? cfg.volume : 1;
    for (var i = 0; i < repeat; i++) {
      audioQueue.push({ url: url, fallback: i === 0 ? text : '', volume: volume });
    }
    toast('🔊 ' + (text || '自定义录音'));
    drainAudio();
    return true;
  }

  function speak(text, times, force) {
    if (!text) return false;
    if (!supported()) {
      toast('当前浏览器不支持语音播报（请用 Chrome / Edge 新版）', true);
      return false;
    }
    if (!force && !isOn()) return false;
    var repeat = times || (cfg ? cfg.repeat : 1) || 1;
    try {
      window.speechSynthesis.cancel();
      for (var i = 0; i < repeat; i++) {
        window.speechSynthesis.speak(utter(text));
      }
      toast('🔊 ' + text);
      return true;
    } catch (err) {
      return false;
    }
  }

  /** 事件播报：优先自己的录音 -> 系统自带人声 -> 浏览器合成音（保底） */
  function announce(kind, text, times, force) {
    var cfgObj = cfgOf();
    var clips = cfgObj.clips || {};
    var defaults = cfgObj.default_clips || {};
    var url = (kind && (clips[kind] || defaults[kind])) || '';
    if (url && playClip(url, text, times)) return true;
    return speak(text, times, force);
  }

  function isOn() {
    try {
      return window.localStorage.getItem(ON_KEY) === '1';
    } catch (err) {
      return false;
    }
  }

  function setOn(flag) {
    try {
      window.localStorage.setItem(ON_KEY, flag ? '1' : '0');
    } catch (err) {
      /* 隐私模式下忽略 */
    }
    renderFab();
  }

  function renderFab() {
    if (!fab) return;
    var on = isOn();
    fab.classList.toggle('on', on);
    fab.textContent = on ? '🔊 语音播报已开启' : '🔇 语音播报未开启';
    fab.title = on ? '点击关闭语音播报' : '点击开启语音播报（浏览器要求先点击一次才允许出声）';
  }

  function buildFab() {
    if (fab || !supported()) return;
    fab = document.createElement('button');
    fab.type = 'button';
    fab.id = 'voice-fab';
    fab.className = 'voice-fab';
    fab.addEventListener('click', function () {
      if (isOn()) {
        setOn(false);
        try { window.speechSynthesis.cancel(); } catch (err) { /* 忽略 */ }
        toast('已关闭语音播报');
        return;
      }
      setOn(true);
      // 借这次点击解锁音频，并给一句确认音
      speak('语音播报已开启', 1);
    });
    document.body.appendChild(fab);
    renderFab();
  }

  function toast(text, warn) {
    var box = document.getElementById('voice-toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'voice-toast';
      box.className = 'voice-toast';
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.classList.toggle('warn', !!warn);
    box.classList.add('on');
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      box.classList.remove('on');
    }, 6000);
  }

  function storedLastId() {
    try {
      return parseInt(window.localStorage.getItem(LAST_ID_KEY) || '0', 10) || 0;
    } catch (err) {
      return 0;
    }
  }

  function saveLastId(value) {
    try {
      window.localStorage.setItem(LAST_ID_KEY, String(value || 0));
    } catch (err) {
      /* 忽略 */
    }
  }

  function leaderOf() {
    try {
      var raw = window.localStorage.getItem(LEADER_KEY) || '';
      var parts = raw.split('|');
      return { id: parts[0] || '', at: parseInt(parts[1] || '0', 10) || 0 };
    } catch (err) {
      return { id: '', at: 0 };
    }
  }

  function claimLeader() {
    try {
      window.localStorage.setItem(LEADER_KEY, leaderId + '|' + Date.now());
    } catch (err) { /* 隐私模式下忽略 */ }
  }

  /** 抢/续「出声位」：自己就是主、或上一个主已经过期（页面关了 / 被冻结）就接管。 */
  function refreshLeader() {
    var cur = leaderOf();
    if (cur.id === leaderId || !cur.id || Date.now() - cur.at > LEADER_TTL) {
      claimLeader();
      return true;
    }
    return false;
  }

  // 兜底：10 秒内同一句话不重复念（极端情况下两个页面同时抢到出声位）
  var saidAt = {};

  function justSaid(text) {
    if (!text) return true;
    var last = saidAt[text] || 0;
    if (Date.now() - last < 10000) return true;
    saidAt[text] = Date.now();
    return false;
  }

  function pendingUrl(since) {
    return '/api/voice/pending?since=' + encodeURIComponent(since || 0) + '&limit=20';
  }

  function drain(events, latestId, firstLoad) {
    if (!events || !events.length) {
      if (latestId) {
        lastId = latestId;
        saveLastId(lastId);
      }
      return;
    }
    if (firstLoad) {
      // 首次进页面：只把水位推到最新，不补播历史事件，避免进页面就一阵狂读
      lastId = Math.max(latestId || 0, events[events.length - 1].id || 0);
      saveLastId(lastId);
      return;
    }
    var speaker = refreshLeader();
    events.forEach(function (evt) {
      lastId = Math.max(lastId, evt.id || 0);
      // 先落水位再出声：别的标签页这一轮就不会再拿到这条（水位是各标签页共享的）
      saveLastId(lastId);
      if (speaker && !justSaid(evt.text)) announce(evt.kind, evt.text);
    });
    saveLastId(lastId);
  }

  function poll(firstLoad) {
    // 别的标签页可能已经念过：先把水位对齐再拉，否则会把别人念过的再念一遍
    lastId = Math.max(lastId || 0, storedLastId());
    fetch(pendingUrl(lastId), { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.enabled) {
          if (fab) fab.remove();
          fab = null;
          return;
        }
        cfg = data.config || cfg;
        buildFab();
        drain(data.events, data.latest_id, firstLoad);
      })
      .catch(function () { /* 网络异常下一轮再试 */ });
  }

  function init() {
    if (!supported()) return;
    lastId = storedLastId();
    poll(true);
    if (timer) window.clearInterval(timer);
    timer = window.setInterval(function () { poll(false); }, POLL_MS);
    // 心跳续「出声位」；关页面时立刻让出来，别的标签页下一轮就能接上
    if (beat) window.clearInterval(beat);
    beat = window.setInterval(refreshLeader, BEAT_MS);
    window.addEventListener('beforeunload', function () {
      if (leaderOf().id === leaderId) {
        try { window.localStorage.removeItem(LEADER_KEY); } catch (err) { /* 忽略 */ }
      }
    });
    if (typeof window.speechSynthesis.onvoiceschanged !== 'undefined') {
      window.speechSynthesis.onvoiceschanged = function () { pickVoice(); };
    }
  }

  // 供页面「试听」按钮调用
  window.OrderVoice = {
    // 试听：force=true 绕过「未开启」状态，方便配置页直接听效果
    speak: function (text, times) { return speak(text, times, true); },
    // 试听某场景：有录音播录音，没有就播合成音
    play: function (kind, text, clipUrl) {
      return (clipUrl && playClip(clipUrl, text, 1)) || speak(text, 1, true);
    },
    toast: toast
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
