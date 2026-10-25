(() => {
  function filterSector(root) {
    const search = (root.querySelector('[data-sr-search]')?.value || '').trim().toLowerCase();
    const state = root.querySelector('[data-sr-state]')?.value || '';
    let count = 0;
    root.querySelectorAll('[data-sr-row]').forEach(row => {
      row.hidden = !(row.dataset.search.toLowerCase().includes(search) && (!state || row.dataset.state === state));
      count += Number(!row.hidden);
    });
    root.querySelector('[data-sr-empty]').hidden = count > 0;
    root.querySelector('[data-sr-count]').textContent = `${count} 个主题`;
  }
  function filterLimit(root) {
    const search = (root.querySelector('[data-lr-search]')?.value || '').trim().toLowerCase();
    const onlyCons = root.querySelector('[data-lr-cons]').checked;
    const risk = root.querySelector('[data-lr-risk]').value;
    let count = 0, total = 0;
    root.querySelectorAll('[data-lr-tier]').forEach(tier => {
      let local = 0;
      tier.querySelectorAll('[data-lr-row]').forEach(row => {
        const raw = row.dataset.risk, breaks = raw === '' ? null : Number(raw);
        row.hidden = !(row.dataset.search.toLowerCase().includes(search) && (!onlyCons || Number(row.dataset.level) >= 2) && (!risk || risk === 'yes' && breaks > 0 || risk === 'no' && breaks === 0));
        local += Number(!row.hidden); total++;
      });
      tier.hidden = !local;
      tier.open = search || risk ? local > 0 : tier.dataset.defaultOpen === 'true';
      tier.querySelector('[data-lr-tier-count]').textContent = `${local} 只`;
      count += local;
    });
    root.querySelector('[data-lr-count]').textContent = `${count} / ${total} 只`;
    root.querySelector('[data-lr-empty]').hidden = count > 0;
  }
  for (const type of ['input', 'change']) document.addEventListener(type, event => {
    const sector = event.target.closest('[data-sector-review]');
    const limit = event.target.closest('[data-limitup-review]');
    if (sector) filterSector(sector);
    if (limit) filterLimit(limit);
  });
})();
