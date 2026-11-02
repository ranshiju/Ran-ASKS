/* Progressive enhancement: all works and downloads remain readable without JS. */
(() => {
  'use strict';
  document.documentElement.classList.add('js');
  const form = document.querySelector('.search-bar');
  if (form) {
    const cards = [...document.querySelectorAll('[data-work]')];
    const filters = [...document.querySelectorAll('[data-capability]')];
    const search = form.elements.q;
    const kind = form.elements.kind;
    const status = document.querySelector('.result-count');
    const empty = document.querySelector('.empty-state');
    let capability = 'all';
    const normalize = value => value.normalize('NFKC').toLocaleLowerCase().trim();
    function readURL() {
      const params = new URLSearchParams(location.search);
      search.value = params.get('q') || '';
      capability = filters.some(button => button.dataset.capability === params.get('capability')) ? params.get('capability') : 'all';
      kind.value = [...kind.options].some(option => option.value === params.get('kind')) ? params.get('kind') : 'all';
    }
    function render(updateURL = true) {
      const words = normalize(search.value).split(/\s+/).filter(Boolean);
      let count = 0;
      for (const card of cards) {
        const visible = (capability === 'all' || card.dataset.capabilities.split(' ').includes(capability)) &&
          (kind.value === 'all' || kind.value === card.dataset.kind) &&
          words.every(word => normalize(card.dataset.search).includes(word));
        card.hidden = !visible;
        if (visible) count += 1;
      }
      for (const button of filters) {
        const active = button.dataset.capability === capability;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      }
      status.textContent = `显示 ${count} / ${cards.length} 件作品`;
      empty.hidden = count !== 0;
      if (updateURL) {
        const url = new URL(location.href);
        for (const [key, value] of [['q', search.value.trim()], ['capability', capability], ['kind', kind.value]]) {
          if (!value || value === 'all') url.searchParams.delete(key);
          else url.searchParams.set(key, value);
        }
        try { history.replaceState(null, '', url); } catch (_) { /* file preview still filters */ }
      }
    }
    for (const button of filters) button.addEventListener('click', () => {
      capability = button.dataset.capability;
      render();
    });
    search.addEventListener('input', () => render());
    kind.addEventListener('change', () => render());
    form.addEventListener('submit', event => { event.preventDefault(); render(); });
    document.querySelector('[data-reset]').addEventListener('click', () => {
      search.value = ''; kind.value = 'all'; capability = 'all'; render(); search.focus();
    });
    window.addEventListener('popstate', () => { readURL(); render(false); });
    readURL(); render(false);
  }
  const copy = document.querySelector('[data-copy]');
  if (copy) copy.addEventListener('click', async () => {
    const source = document.getElementById(copy.dataset.copy);
    const status = document.querySelector('.copy-status');
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(source.textContent);
      status.textContent = '已复制，可以向 ASKS 提问了。';
    } catch (_) {
      const range = document.createRange(); range.selectNodeContents(source);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      status.textContent = '已选中指令，请手动复制。';
    }
  });
})();
