const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
  return {hidden: true, dataset: {}, style: {}, classList: {toggle() {}, add() {}, remove() {}},
    handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelectorAll() { return []; }};
}

async function run() {
  const ids = ['jobs', 'form-message', 'cookie-message', 'cookie-status', 'cookie-domains',
    'download-path-message', 'download-path', 'browser-modal', 'browser-frame', 'browser-message', 'remove-cookies'];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  let poll;
  let jobs = [];
  const opened = [];
  const context = {
    document: {querySelector: () => ({content: 'csrf'}), getElementById: id => elements[id]},
    window: {setInterval: fn => { poll = fn; return 1; }, addEventListener() {}, clearInterval() {}, location: {assign() {}}},
    Headers, URLSearchParams,
    fetch: async url => {
      if (url === '/api/jobs') return {ok: true, json: async () => ({jobs, cookies_configured: true, cookie_domains: ['facebook.com']})};
      opened.push(url);
      return {ok: true, json: async () => ({ticket: 'test'})};
    },
  };
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  jobs = [
    {id: 'fb', status: 'FAILED', can_login: true, cookies_configured: true},
    {id: 'ig', status: 'FAILED', can_login: true, cookies_configured: false},
    {id: 'unknown', status: 'FAILED', can_login: false, cookies_configured: false},
    {id: 'youtube', status: 'NEEDS_AUTH', can_login: true, cookies_configured: false},
  ];
  await poll();
  assert.equal(opened.length, 1);
  assert.equal(opened[0], '/api/jobs/ig/auth-session');
  await poll();
  assert.equal(opened.length, 1, 'No navigation while another login dialog is open');
  elements['browser-modal'].hidden = true;
  await poll();
  assert.equal(opened[1], '/api/jobs/youtube/auth-session');
  elements['browser-modal'].hidden = true;
  await poll();
  assert.equal(opened.length, 2, 'Each eligible job prompts once; saved or unsupported sites do not prompt');
  console.log('Auto-login tests passed');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
