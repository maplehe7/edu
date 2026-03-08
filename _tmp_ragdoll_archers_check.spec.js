const { test, expect } = require('playwright/test');

test('ragdoll archers local build stays offline from crazygames sdk', async ({ page }) => {
  const consoleMessages = [];
  const pageErrors = [];
  const sdkRequests = [];

  page.on('console', (message) => {
    consoleMessages.push({
      type: message.type(),
      text: message.text(),
    });
  });

  page.on('pageerror', (error) => {
    pageErrors.push(String(error && error.message ? error.message : error));
  });

  page.on('request', (request) => {
    const url = request.url();
    if (/sdk\.crazygames\.com/i.test(url)) {
      sdkRequests.push(url);
    }
  });

  await page.goto('http://127.0.0.1:8871/Ragdoll%20Archers/', {
    waitUntil: 'domcontentloaded',
  });

  await page.waitForFunction(
    () => document.body && document.body.getAttribute('data-ocean-unity-state') === 'ready',
    { timeout: 120000 }
  );

  const state = await page.evaluate(() => ({
    title: document.title,
    oceanState: document.body && document.body.getAttribute('data-ocean-unity-state'),
    hasCanvas: !!document.querySelector('canvas'),
  }));

  console.log(
    'RAGDOLL_REPORT ' +
      JSON.stringify({
        state,
        pageErrors,
        sdkRequests,
        severeConsole: consoleMessages.filter((item) => item.type === 'error'),
      })
  );

  expect(state.oceanState).toBe('ready');
  expect(state.hasCanvas).toBe(true);
  expect(sdkRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(
    consoleMessages.some((item) => /sdkDisabled|CrazySDK is disabled on this domain|GeneralError/i.test(item.text))
  ).toBe(false);
});
