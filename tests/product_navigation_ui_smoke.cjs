const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..'), output = path.join(root, 'outputs/product-navigation');
(async () => {
  let fragmentRequests = 0;
  const server = http.createServer((req, res) => {
    const url = new URL(req.url,'http://localhost');
    let file;
    if (url.pathname.startsWith('/static/')) file = path.join(root,'web',url.pathname);
    else if (url.pathname.startsWith('/report/')) { fragmentRequests++; file=path.join(output,(req.headers.referer||'').includes('/data/sector')?'hotspot.html':'concept.html'); }
    else file=path.join(output,`${url.pathname.startsWith('/data/sector')?'sector':'limitup'}-${url.searchParams.get('role')==='viewer'?'viewer':'admin'}.html`);
    if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_BROWSER_EXE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    for (const width of [1440,390]) {
      const requestsBefore = fragmentRequests;
      const page=await browser.newPage({viewport:{width,height:1000}}), errors=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('https://**',route=>route.abort());
      await page.goto(`http://127.0.0.1:${server.address().port}/data/limitup/20261023`,{waitUntil:'domcontentloaded'});
      await page.waitForFunction(()=>document.querySelector('#side-nav svg.lucide'));
      assert.equal(await page.locator('#side-nav .nav-link').count(),6);
      assert.equal(await page.locator('#side-nav .nav-link.active').count(),1);
      assert.equal(await page.locator('#side-nav .active').getAttribute('data-module'),'market');
      assert.equal(await page.locator('.module-tabs .active').count(),1);
      assert.equal(await page.locator('.module-tab').count(),3);
      assert.equal(await page.getByText('最新概览',{exact:true}).count(),0);
      assert.match(await page.textContent('.data-focus-summary'),/最高 7 板/);
      assert.match(await page.textContent('#section-panel'),/海洋王/);
      assert.equal(await page.locator('[data-lr-tier][open]').count(),3);
      await page.locator('[data-lr-search]').fill('南威');
      assert.equal(await page.locator('[data-lr-row]:visible').count(),1);
      assert.match(await page.locator('[data-lr-row]:visible').getAttribute('href'),/stock/);
      await page.locator('[data-lr-search]').fill('');
      await page.locator('[data-lr-risk]').selectOption('yes');
      assert.equal(await page.locator('[data-lr-row]:visible').count(),1);
      assert.match(await page.locator('[data-lr-row]:visible').textContent(),/东安动力/);
      await page.locator('[data-lr-risk]').selectOption('no');
      assert.equal(await page.locator('[data-lr-row]:visible').count(),3);
      await page.locator('[data-lr-risk]').selectOption('');
      await page.locator('[data-lr-cons]').check();
      assert.equal(await page.locator('[data-lr-row]:visible').count(),3);
      await page.locator('[data-lr-cons]').uncheck();
      assert.equal(fragmentRequests, requestsBefore);
      assert.equal(await page.locator('.module-tab').nth(1).getAttribute('href'),'/data/sector/20261023');
      await page.screenshot({path:path.join(output,`market-${width}.png`),fullPage:true});
      await page.locator('#data-tabs button').nth(1).click();
      await page.waitForFunction(()=>document.querySelector('#section-panel').textContent.includes('机器人概念'));
      assert.equal(await page.locator('#data-tabs button[aria-pressed=true]').count(),1);
      if (width===1440) {
        await page.click('#sidebar-collapse');
        await page.waitForFunction(()=>Math.abs(document.querySelector('#app-sidebar').getBoundingClientRect().width-68)<1);
        assert.equal(await page.locator('#sidebar-collapse').getAttribute('aria-label'),'展开侧栏');
        await page.screenshot({path:path.join(output,'market-collapsed.png'),fullPage:true});
        await page.reload({waitUntil:'domcontentloaded'});
        await page.waitForFunction(()=>document.body.classList.contains('sidebar-collapsed'));
        await page.click('#sidebar-collapse');
      } else {
        await page.click('#mobile-nav-button');
        assert.equal(await page.locator('#app-sidebar').evaluate(el=>el.classList.contains('open')),true);
        await page.screenshot({path:path.join(output,'mobile-menu.png'),fullPage:true});
        await page.click('#mobile-nav-backdrop',{position:{x:300,y:400}});
      }
      await page.locator('.module-tab').nth(1).click();
      await page.waitForURL('**/data/sector/20261023');
      assert.match(await page.textContent('.data-focus-summary'),/机器人概念/);
      await page.locator('[data-sr-state]').selectOption('数据不足');
      assert.equal(await page.locator('[data-sr-row]:visible').count(),1);
      assert.match(await page.locator('[data-sr-row]:visible').textContent(),/人工智能/);
      await page.locator('[data-sr-state]').selectOption('');
      await page.locator('[data-sr-search]').fill('886001');
      assert.equal(await page.locator('[data-sr-row]:visible').count(),1);
      await page.locator('[data-sr-search]').fill('无匹配');
      assert.equal(await page.locator('[data-sr-empty]:visible').count(),1);
      await page.locator('[data-sr-search]').fill('');
      await page.screenshot({path:path.join(output,`sector-${width}.png`),fullPage:true});
      await page.locator('#data-tabs button').nth(1).click();
      await page.waitForFunction(()=>document.querySelector('#section-panel').textContent.includes('热点概念'));
      await page.locator('[data-sr-search]').fill('无匹配');
      assert.equal(await page.locator('[data-sr-empty]:visible').count(),1);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.deepEqual(errors,[]);
      await page.goto(`http://127.0.0.1:${server.address().port}/data/limitup/20261023?role=viewer`,{waitUntil:'domcontentloaded'});
      assert.equal(await page.locator('#side-nav [data-module=research],#side-nav [data-module=collection]').count(),0);
      assert.equal(await page.locator('#side-nav .nav-link').count(),4);
      await page.close();
    }
    console.log('Navigation: 6 modules, role filtering, date-preserving tabs, SSR first view, HTMX switching, collapsed persistence and mobile passed.');
  } finally { await browser?.close(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
