(function () {
  const configNode = document.getElementById('session-timeout-config');
  if (!configNode) return;

  let config = {};
  try {
    config = JSON.parse(configNode.textContent || '{}');
  } catch (error) {
    return;
  }

  if (!config.enabled || !config.timeout_seconds) return;

  let timeoutMs = Number(config.timeout_seconds) * 1000;
  let warningMs = Number(config.warning_seconds || 0) * 1000;
  let keepaliveIntervalMs = Number(config.keepalive_interval_seconds || 300) * 1000;

  let lastActivityAt = Date.now();
  let lastKeepaliveAt = Date.now();
  let keepaliveInFlight = false;
  let expired = false;
  let warningVisible = false;
  let activityThrottleAt = 0;

  const overlay = document.createElement('div');
  overlay.style.cssText = [
    'position:fixed',
    'inset:0',
    'background:rgba(15,23,42,.45)',
    'display:none',
    'align-items:center',
    'justify-content:center',
    'padding:1rem',
    'z-index:9999',
  ].join(';');

  const card = document.createElement('div');
  card.style.cssText = [
    'width:min(440px,100%)',
    'background:#fff',
    'border-radius:16px',
    'box-shadow:0 24px 80px rgba(15,23,42,.25)',
    'padding:1.25rem 1.25rem 1rem',
    'font-family:inherit',
    'color:#1f2937',
  ].join(';');

  const title = document.createElement('h5');
  title.textContent = 'Sessao a expirar';
  title.style.cssText = 'margin:0 0 .5rem;font-size:1.1rem;font-weight:700;';

  const body = document.createElement('p');
  body.style.cssText = 'margin:0 0 1rem;color:#475569;line-height:1.5;';

  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:.75rem;justify-content:flex-end;flex-wrap:wrap;';

  const continueBtn = document.createElement('button');
  continueBtn.type = 'button';
  continueBtn.textContent = 'Continuar sessao';
  continueBtn.style.cssText = [
    'border:0',
    'border-radius:10px',
    'padding:.7rem 1rem',
    'background:#0ea5e9',
    'color:#fff',
    'font-weight:600',
    'cursor:pointer',
  ].join(';');

  const logoutBtn = document.createElement('button');
  logoutBtn.type = 'button';
  logoutBtn.textContent = 'Terminar sessao';
  logoutBtn.style.cssText = [
    'border:1px solid #cbd5e1',
    'border-radius:10px',
    'padding:.7rem 1rem',
    'background:#fff',
    'color:#334155',
    'font-weight:600',
    'cursor:pointer',
  ].join(';');

  actions.appendChild(logoutBtn);
  actions.appendChild(continueBtn);
  card.appendChild(title);
  card.appendChild(body);
  card.appendChild(actions);
  overlay.appendChild(card);
  document.body.appendChild(overlay);

  function getCookie(name) {
    const cookies = document.cookie ? document.cookie.split(';') : [];
    for (let index = 0; index < cookies.length; index += 1) {
      const cookie = cookies[index].trim();
      if (cookie.startsWith(`${name}=`)) {
        return decodeURIComponent(cookie.substring(name.length + 1));
      }
    }
    return '';
  }

  function formatRemaining(ms) {
    const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    if (minutes <= 0) {
      return `${seconds}s`;
    }
    return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  }

  function hideWarning() {
    overlay.style.display = 'none';
    warningVisible = false;
  }

  function showWarning(remainingMs) {
    body.textContent = `A tua sessao vai expirar por inatividade em ${formatRemaining(remainingMs)}.`;
    overlay.style.display = 'flex';
    warningVisible = true;
  }

  function redirectToLogout() {
    if (expired) return;
    expired = true;
    window.location.href = `${config.logout_url || '/logout/'}?reason=timeout`;
  }

  function handleExpiredResponse(payload) {
    if (payload && payload.login_url) {
      window.location.href = payload.login_url;
      return;
    }
    redirectToLogout();
  }

  async function sendKeepalive() {
    if (keepaliveInFlight || expired || !config.keepalive_url) return;
    keepaliveInFlight = true;
    try {
      const response = await fetch(config.keepalive_url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'X-CSRFToken': getCookie('csrftoken'),
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        handleExpiredResponse(payload);
        return;
      }
      if (payload.timeout_seconds) {
        config.timeout_seconds = Number(payload.timeout_seconds);
        timeoutMs = config.timeout_seconds * 1000;
      }
      if (payload.warning_seconds) {
        config.warning_seconds = Number(payload.warning_seconds);
        warningMs = config.warning_seconds * 1000;
      }
      if (payload.keepalive_interval_seconds) {
        config.keepalive_interval_seconds = Number(payload.keepalive_interval_seconds);
        keepaliveIntervalMs = config.keepalive_interval_seconds * 1000;
      }
      lastKeepaliveAt = Date.now();
    } catch (error) {
      // Ignore transient network errors; backend timeout still protects the session.
    } finally {
      keepaliveInFlight = false;
    }
  }

  function registerActivity(forceKeepalive) {
    if (expired) return;
    const now = Date.now();
    lastActivityAt = now;
    if (warningVisible) {
      hideWarning();
    }
    if (forceKeepalive || now - lastKeepaliveAt >= keepaliveIntervalMs) {
      sendKeepalive();
    }
  }

  function throttledActivity() {
    const now = Date.now();
    if (now - activityThrottleAt < 15000) return;
    activityThrottleAt = now;
    registerActivity(false);
  }

  function tick() {
    if (expired) return;
    const remainingMs = timeoutMs - (Date.now() - lastActivityAt);
    if (remainingMs <= 0) {
      redirectToLogout();
      return;
    }
    if (warningMs > 0 && remainingMs <= warningMs) {
      showWarning(remainingMs);
      return;
    }
    if (warningVisible) {
      hideWarning();
    }
  }

  ['click', 'keydown', 'touchstart', 'mousedown'].forEach((eventName) => {
    document.addEventListener(eventName, () => registerActivity(false), { passive: true });
  });
  ['mousemove', 'scroll', 'touchmove'].forEach((eventName) => {
    document.addEventListener(eventName, throttledActivity, { passive: true });
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      registerActivity(false);
    }
  });

  continueBtn.addEventListener('click', () => {
    registerActivity(true);
  });
  logoutBtn.addEventListener('click', redirectToLogout);

  window.setInterval(tick, 1000);
  tick();
}());
