(() => {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-dp-filter]');
    const root = button?.closest('[data-decision-pool]');
    if (!root) return;
    let strategy = button.dataset.dpFilter || '';
    if (strategy && button.getAttribute('aria-pressed') === 'true') strategy = '';
    root.querySelectorAll('[data-dp-filter]').forEach(item => item.setAttribute('aria-pressed', String(item.dataset.dpFilter === strategy)));
    let total = 0, visible = 0;
    root.querySelectorAll('.dp-group').forEach(group => {
      const cards = Array.from(group.querySelectorAll('[data-dp-strategies]'));
      let count = 0;
      cards.forEach(card => {
        let names = [];
        try { names = JSON.parse(card.dataset.dpStrategies); } catch (_) {}
        const matches = !strategy || Array.isArray(names) && names.includes(strategy);
        card.hidden = !matches;
        if (matches) count++;
      });
      total += cards.length; visible += count;
      const counter = group.querySelector('[data-dp-count]');
      if (counter) counter.textContent = strategy ? `${count} / ${cards.length}` : String(cards.length);
      const empty = group.querySelector('[data-dp-empty]');
      if (empty) empty.hidden = !strategy || count > 0;
    });
    const status = root.querySelector('[data-dp-status]');
    if (status) status.textContent = strategy ? `${strategy} · 匹配 ${visible} / ${total} 只` : '';
  });
})();
