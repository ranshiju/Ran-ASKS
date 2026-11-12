/* Optional public-site analytics. Core Gallery functionality never depends on it. */
(() => {
  'use strict';
  try {
    const meta = name => document.querySelector(`meta[name="${name}"]`)?.content || '';
    const base = new URL(meta('gallery-site-url'));
    if (location.protocol !== 'https:' || location.origin !== base.origin ||
        !location.pathname.startsWith(base.pathname)) return;
    const choice = new URLSearchParams(location.search).get('analytics');
    const key = `asks-gallery-analytics:${base.pathname}`;
    let excluded = choice === 'off';
    try {
      if (choice === 'off') localStorage.setItem(key, 'off');
      if (choice === 'on') localStorage.removeItem(key);
      excluded = excluded || localStorage.getItem(key) === 'off';
    } catch (_) { /* A current-page opt-out still works without local storage. */ }
    const token = meta('gallery-cloudflare-token');
    const status = document.querySelector('[data-analytics-status]');
    if (status) status.textContent = excluded ? '此浏览器已停用统计。' :
      token ? '此浏览器允许统计；实际采集仍受网络及浏览器设置影响。' : '本站尚未启用统计。';
    if (excluded || !/^[a-f0-9]{32}$/i.test(token) || document.getElementById('gallery-beacon')) return;
    const script = document.createElement('script');
    script.id = 'gallery-beacon';
    script.type = 'module';
    script.src = 'https://static.cloudflareinsights.com/beacon.min.js';
    script.defer = true;
    script.setAttribute('data-cf-beacon', JSON.stringify({token, spa: false}));
    document.head.appendChild(script);
  } catch (_) { /* Missing configuration or blocked analytics leaves the site usable. */ }
})();
