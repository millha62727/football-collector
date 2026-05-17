// lock.js — session lock + disguise logic
// Included on protected pages. Requires the lock overlay HTML to already be present.

(function () {
  // ── Disguise (redirect to /market) ──────────────────────────────────────
  window.goDisguise = function () {
    window.location.href = '/market';
  };

  // ── Inactivity lock ─────────────────────────────────────────────────────
  const LOCK_TIMEOUT = 15 * 60 * 1000; // 15 minutes
  const LOCK_KEY = 'fbc_locked';
  let _lockTimer = null;

  function showLock() {
    sessionStorage.setItem(LOCK_KEY, '1');
    const el = document.getElementById('lockOverlay');
    if (el) {
      el.style.display = 'flex';
      el.style.alignItems = 'center';
      el.style.justifyContent = 'center';
      el.style.flexDirection = 'column';
      const pin = document.getElementById('lockPin');
      if (pin) { pin.value = ''; pin.focus(); }
    }
  }

  function hideLock() {
    sessionStorage.removeItem(LOCK_KEY);
    const el = document.getElementById('lockOverlay');
    if (el) el.style.display = 'none';
  }

  function resetTimer() {
    clearTimeout(_lockTimer);
    _lockTimer = setTimeout(showLock, LOCK_TIMEOUT);
  }

  window.lockVerify = async function () {
    const pin = document.getElementById('lockPin');
    const err = document.getElementById('lockErr');
    if (!pin) return;
    const pw = pin.value.trim();
    if (!pw) { if (err) err.textContent = 'Nhập mật khẩu'; return; }
    try {
      const r = await fetch('/api/lock-verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      });
      const data = await r.json();
      if (data.ok) {
        hideLock();
        resetTimer();
        if (err) err.textContent = '';
      } else {
        if (err) err.textContent = 'Mật khẩu không đúng';
        pin.value = '';
        pin.focus();
      }
    } catch (e) {
      if (err) err.textContent = 'Lỗi kết nối';
    }
  };

  // Start timer on page load, reset on activity
  ['mousemove', 'keydown', 'click', 'touchstart', 'scroll'].forEach(ev => {
    document.addEventListener(ev, resetTimer, { passive: true });
  });

  // If already locked in this session, show immediately
  if (sessionStorage.getItem(LOCK_KEY)) {
    showLock();
  } else {
    resetTimer();
  }
})();
