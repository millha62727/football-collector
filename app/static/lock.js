// lock.js — session lock (idle-timeout) + Telegram-delivered unlock OTP.
// Included on protected pages. Requires the lock overlay HTML to be present
// with these IDs: lockOverlay, lockOtp, lockErr, lockMsg, lockBtnReq, lockBtnVerify.

(function () {
  // ── Disguise (redirect to /market) ──────────────────────────────────────
  window.goDisguise = function () {
    window.location.href = '/market';
  };

  // ── Idle-lock config (fallback 5 min, overridden by /api/lock/config) ───
  let LOCK_TIMEOUT_MS = 5 * 60 * 1000;
  const LOCK_KEY = 'fbc_locked';
  let _lockTimer = null;
  let _otpRequested = false;

  function $(id) { return document.getElementById(id); }

  function showLock() {
    sessionStorage.setItem(LOCK_KEY, '1');
    const el = $('lockOverlay');
    if (!el) return;
    el.style.display = 'flex';
    el.style.alignItems = 'center';
    el.style.justifyContent = 'center';
    el.style.flexDirection = 'column';
    _otpRequested = false;
    const otp = $('lockOtp');
    if (otp) { otp.value = ''; otp.disabled = true; }
    const err = $('lockErr'); if (err) err.textContent = '';
    const msg = $('lockMsg'); if (msg) msg.textContent = 'Bấm "Xin OTP mới" để nhận mã qua Telegram.';
    const btnReq = $('lockBtnReq');    if (btnReq) { btnReq.disabled = false; btnReq.textContent = 'Xin OTP mới'; }
    const btnVer = $('lockBtnVerify'); if (btnVer) btnVer.disabled = true;
  }

  function hideLock() {
    sessionStorage.removeItem(LOCK_KEY);
    const el = $('lockOverlay');
    if (el) el.style.display = 'none';
  }

  function resetTimer() {
    if (sessionStorage.getItem(LOCK_KEY)) return; // already locked, no reset
    clearTimeout(_lockTimer);
    _lockTimer = setTimeout(showLock, LOCK_TIMEOUT_MS);
  }

  window.lockRequestOtp = async function () {
    const btnReq = $('lockBtnReq');
    const err = $('lockErr');
    const msg = $('lockMsg');
    const otp = $('lockOtp');
    const btnVer = $('lockBtnVerify');
    if (err) err.textContent = '';
    if (btnReq) { btnReq.disabled = true; btnReq.textContent = 'Đang gửi...'; }
    try {
      const r = await fetch('/api/lock/request-otp', { method: 'POST' });
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json();
      if (!r.ok || !j.ok) {
        if (err) err.textContent = j.error || 'Không gửi được OTP';
        if (btnReq) { btnReq.disabled = false; btnReq.textContent = 'Xin OTP mới'; }
        return;
      }
      _otpRequested = true;
      if (msg) msg.textContent = 'OTP đã gửi qua Telegram. Hết hạn sau ' + Math.round((j.ttl_seconds||300)/60) + ' phút.';
      if (otp) { otp.disabled = false; otp.focus(); }
      if (btnVer) btnVer.disabled = false;
      if (btnReq) { btnReq.disabled = false; btnReq.textContent = 'Gửi lại OTP'; }
    } catch (e) {
      if (err) err.textContent = 'Lỗi kết nối';
      if (btnReq) { btnReq.disabled = false; btnReq.textContent = 'Xin OTP mới'; }
    }
  };

  window.lockVerify = async function () {
    const otp = $('lockOtp');
    const err = $('lockErr');
    const btnVer = $('lockBtnVerify');
    if (!otp) return;
    if (!_otpRequested) {
      if (err) err.textContent = 'Hãy bấm "Xin OTP mới" trước.';
      return;
    }
    const code = (otp.value || '').trim();
    if (!code) { if (err) err.textContent = 'Nhập OTP'; return; }
    if (btnVer) btnVer.disabled = true;
    if (err) err.textContent = '';
    try {
      const r = await fetch('/api/lock/verify-otp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ otp: code }),
      });
      if (r.status === 401) {
        const j = await r.json().catch(() => ({}));
        if (err) err.textContent = j.error || 'OTP không đúng';
        otp.value = '';
        otp.focus();
        if (btnVer) btnVer.disabled = false;
        return;
      }
      const j = await r.json();
      if (j.ok) {
        hideLock();
        resetTimer();
      } else {
        if (err) err.textContent = j.error || 'OTP không đúng';
        otp.value = '';
        otp.focus();
        if (btnVer) btnVer.disabled = false;
      }
    } catch (e) {
      if (err) err.textContent = 'Lỗi kết nối';
      if (btnVer) btnVer.disabled = false;
    }
  };

  // Pull idle timeout from server (.env-driven), then start the timer.
  fetch('/api/lock/config').then(r => r.ok ? r.json() : null).then(cfg => {
    if (cfg && cfg.idle_seconds) {
      LOCK_TIMEOUT_MS = Math.max(60, cfg.idle_seconds) * 1000;
    }
    if (sessionStorage.getItem(LOCK_KEY)) showLock();
    else resetTimer();
  }).catch(() => {
    if (sessionStorage.getItem(LOCK_KEY)) showLock();
    else resetTimer();
  });

  // Activity resets the timer.
  ['mousemove', 'keydown', 'click', 'touchstart', 'scroll'].forEach(ev => {
    document.addEventListener(ev, resetTimer, { passive: true });
  });
})();
