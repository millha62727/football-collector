// lock.js — session lock (idle-timeout) + password unlock.
// Included on protected pages.

(function () {
  window.goDisguise = function () {
    window.location.href = '/market';
  };

  let LOCK_TIMEOUT_MS = 5 * 60 * 1000;
  const LOCK_KEY = 'fbc_locked';
  let _lockTimer = null;
  const _debugLock = (() => {
    try { return new URL(window.location.href).searchParams.get('debugLock') === '1'; }
    catch (e) { return false; }
  })();

  function $(id) { return document.getElementById(id); }

  window.showLock = function showLock() {
    sessionStorage.setItem(LOCK_KEY, '1');
    const el = $('lockOverlay');
    if (!el) return;
    el.style.display = 'flex';
    el.style.alignItems = 'center';
    el.style.justifyContent = 'center';
    el.style.flexDirection = 'column';
    const pass = $('lockPass');
    if (pass) { pass.value = ''; pass.disabled = false; pass.focus(); }
    const err = $('lockErr'); if (err) err.textContent = '';
    const msg = $('lockMsg'); if (msg) msg.textContent = 'Nhap mat khau de mo khoa.';
    const btnVer = $('lockBtnVerify'); if (btnVer) { btnVer.disabled = false; btnVer.textContent = 'Mo khoa'; }
  };
  const showLock = window.showLock;

  function hideLock() {
    sessionStorage.removeItem(LOCK_KEY);
    const el = $('lockOverlay');
    if (el) el.style.display = 'none';
  }

  let _lastReset = 0;
  let _trailingPending = false;
  function _doReset() {
    clearTimeout(_lockTimer);
    _lockTimer = setTimeout(showLock, LOCK_TIMEOUT_MS);
  }
  function resetTimer() {
    if (sessionStorage.getItem(LOCK_KEY)) return;
    const now = Date.now();
    if (now - _lastReset < 250) {
      if (!_trailingPending) {
        _trailingPending = true;
        setTimeout(() => {
          _trailingPending = false;
          if (sessionStorage.getItem(LOCK_KEY)) return;
          _lastReset = Date.now();
          _doReset();
        }, 260);
      }
      return;
    }
    _lastReset = now;
    _doReset();
  }

  window.lockVerify = async function () {
    const pass = $('lockPass');
    const err = $('lockErr');
    const btnVer = $('lockBtnVerify');
    if (!pass) return;
    const pwd = (pass.value || '').trim();
    if (!pwd) { if (err) err.textContent = 'Nhap mat khau'; return; }
    if (btnVer) { btnVer.disabled = true; btnVer.textContent = 'Dang kiem tra...'; }
    if (err) err.textContent = '';
    try {
      const r = await fetch('/api/lock/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pwd }),
      });
      if (r.status === 401) {
        if (err) err.textContent = 'Sai mat khau';
        pass.value = '';
        pass.focus();
        if (btnVer) { btnVer.disabled = false; btnVer.textContent = 'Mo khoa'; }
        return;
      }
      const j = await r.json();
      if (j.ok) {
        hideLock();
        resetTimer();
      } else {
        if (err) err.textContent = j.error || 'Sai mat khau';
        pass.value = '';
        pass.focus();
        if (btnVer) { btnVer.disabled = false; btnVer.textContent = 'Mo khoa'; }
      }
    } catch (e) {
      if (err) err.textContent = 'Loi ket noi';
      if (btnVer) { btnVer.disabled = false; btnVer.textContent = 'Mo khoa'; }
    }
  };

  fetch('/api/lock/config').then(r => r.ok ? r.json() : null).then(cfg => {
    if (cfg && cfg.idle_seconds) {
      LOCK_TIMEOUT_MS = Math.max(60, cfg.idle_seconds) * 1000;
    }
    clearTimeout(_lockTimer);
    if (sessionStorage.getItem(LOCK_KEY)) showLock();
    else _doReset();
  }).catch(() => {
    clearTimeout(_lockTimer);
    if (sessionStorage.getItem(LOCK_KEY)) showLock();
    else _doReset();
  });

  const ACTIVITY_EVENTS = [
    'mousemove', 'mousedown', 'pointermove', 'pointerdown',
    'keydown', 'keyup', 'click',
    'touchstart', 'touchmove',
    'wheel', 'scroll',
    'input', 'change', 'focusin'
  ];
  ACTIVITY_EVENTS.forEach(ev => {
    document.addEventListener(ev, resetTimer, { capture: true, passive: true });
  });
  window.addEventListener('scroll', resetTimer, { capture: true, passive: true });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') resetTimer();
  });
})();
