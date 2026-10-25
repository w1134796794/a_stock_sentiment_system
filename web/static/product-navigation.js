(() => {
  function init() {
    const button = document.getElementById('sidebar-collapse');
    if (!button) return;
    const desktop = matchMedia('(min-width:768px)');
    let collapsed = false;
    try { collapsed = localStorage.getItem('astock:sidebar-collapsed') === 'true'; } catch (_) {}
    function render() {
      const closed = desktop.matches && collapsed;
      document.body.classList.toggle('sidebar-collapsed', closed);
      const label = closed ? '展开侧栏' : '收起侧栏';
      button.setAttribute('aria-expanded', String(!closed));
      button.setAttribute('aria-label', label); button.title = label;
      const icon = document.createElement('i'); icon.dataset.lucide = closed ? 'panel-left-open' : 'panel-left-close';
      button.replaceChildren(icon);
      window.lucide?.createIcons();
    }
    button.addEventListener('click', () => {
      collapsed = !collapsed;
      try { localStorage.setItem('astock:sidebar-collapsed', String(collapsed)); } catch (_) {}
      render();
    });
    desktop.addEventListener('change', render);
    document.querySelectorAll('.module-tabs').forEach(nav => nav.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      const links = Array.from(nav.querySelectorAll('a'));
      const current = links.indexOf(document.activeElement);
      if (current < 0) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? links.length - 1 :
        (current + (event.key === 'ArrowRight' ? 1 : links.length - 1)) % links.length;
      links[index].focus();
    }));
    render();
    document.querySelector('.module-tab.active')?.scrollIntoView({block:'nearest',inline:'nearest'});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true});
  else init();
})();
