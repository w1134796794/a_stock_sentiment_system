const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(require.resolve('playwright', {paths: [process.argv[2] || process.cwd()]}));
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/portfolio-parity-ui');
(async () => {
  const server = http.createServer((req, res) => {
    const css = req.url.startsWith('/static/');
    const file = css ? path.join(root, 'web/static/css/app.css') : path.join(output, req.url.includes('/backtest') ? 'backtest.html' : 'paper.html');
    res.setHeader('Content-Type', css ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, channel: 'chrome'});
    for (const width of [1440, 390]) {
      const page = await browser.newPage({viewport: {width, height: 1000}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.route('https://**', r => r.abort());
      let adoption = 0;
      await page.route('**/api/**', r => {
        const url = new URL(r.request().url());
        let data = {ok: true, data: {positions: [], trades: [], exit_signals: [], summary: {}}};
        if (url.pathname.endsWith('/accounts')) data.data = [{account_key: 'default', name: 'paper'}, {account_key: 'history_new', name: 'new replay'}, {account_key: 'backup_old', name: 'backup'}];
        if (url.pathname.endsWith('/checkpoint')) data.data = {};
        if (url.pathname.endsWith('/status')) data = {state: 'idle', paper_replay: {}};
        if (url.pathname.endsWith('/adopt/preview')) data.data = {revision: 'revision', end_date: '20261014', counts: {default: {positions: 1, trades: 2}, history_new: {positions: 2, trades: 8}}};
        if (url.pathname.endsWith('/adopt')) {
          adoption++;
          assert.equal(r.request().postDataJSON().confirm_replace, true);
          data.data = {backup_account: 'backup_saved'};
        }
        return r.fulfill({contentType: 'application/json', body: JSON.stringify(data)});
      });
      const origin = `http://127.0.0.1:${server.address().port}`;
      await page.goto(origin + '/portfolio', {waitUntil:'domcontentloaded',timeout:60000});
      await page.locator('#portfolio-account option[value="backup_old"]').waitFor({state: 'attached'});
      assert.equal(await page.locator('#start-replay').isVisible(), true);
      assert.equal(await page.locator('#start-rebuild').isVisible(), false);
      await page.screenshot({path: path.join(output, `paper-${width}.png`), fullPage: true});
      await page.goto(origin + '/portfolio/backtest', {waitUntil:'domcontentloaded',timeout:60000});
      await page.locator('#portfolio-account option[value="history_new"]').waitFor({state: 'attached'});
      assert.equal(await page.locator('#start-rebuild').isVisible(), true);
      assert.equal(await page.locator('#start-replay').isVisible(), false);
      assert.equal(await page.locator('#adopt-backtest').isVisible(), true);
      assert.equal(await page.locator('#portfolio-account option[value="default"]').count(), 0);
      page.on('dialog', d => d.accept());
      await page.locator('#adopt-backtest').click();
      await page.waitForURL(origin + '/portfolio');
      assert.equal(adoption, 1);
      assert.deepEqual(errors, []);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
      await page.goto(origin + '/portfolio/backtest', {waitUntil:'domcontentloaded',timeout:60000});
      await page.screenshot({path: path.join(output, `backtest-${width}.png`), fullPage: true});
      await page.close();
    }
    console.log('PASS: desktop/mobile, mode separation, explicit adoption, no console errors or horizontal overflow');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
