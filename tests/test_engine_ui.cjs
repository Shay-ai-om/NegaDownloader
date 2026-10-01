const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
  return {hidden: true, value: 'stable', style: {}, classList: {toggle() {}}, handlers: {},
    addEventListener(event, fn) { this.handlers[event] = fn; }, querySelectorAll() { return []; }};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

async function run() {
  const ids = ['jobs', 'form-message', 'cookie-status', 'cookie-domains', 'remove-cookies', 'browser-modal',
    'yt-dlp-form', 'yt-dlp-version', 'yt-dlp-source', 'yt-dlp-channel', 'yt-dlp-message', 'update-yt-dlp', 'rollback-yt-dlp'];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  let poll;
  let state = {version: '2026.08.19', channel: 'image', source: 'image', busy: false, rollback_available: false, phase: 'IDLE'};
  const mutations = [];
  const context = {
    document: {querySelector: () => ({content: 'test-csrf'}), getElementById: id => elements[id]},
    window: {setInterval(fn) { poll = fn; }, addEventListener() {}, clearInterval() {}},
    Headers, URLSearchParams,
    fetch: async (url, options) => {
      if (url === '/api/jobs') return {ok: true, json: async () => ({jobs: []})};
      if (options.method === 'POST') {
        mutations.push({url, options});
        assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf');
        if (url.endsWith('/update')) state = {...state, busy: true, phase: 'INSTALLING', message: 'installing'};
        if (url.endsWith('/rollback')) state = {...state, version: '2026.08.19', channel: 'image', source: 'image', busy: false, phase: 'ROLLED_BACK'};
      }
      return {ok: true, json: async () => state};
    },
  };
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
  await settle();
  assert.equal(elements['update-yt-dlp'].disabled, false);
  assert.equal(elements['rollback-yt-dlp'].disabled, true);
  elements['yt-dlp-channel'].value = 'nightly';
  elements['yt-dlp-form'].handlers.submit({preventDefault() {}});
  elements['yt-dlp-form'].handlers.submit({preventDefault() {}});
  await settle();
  assert.equal(mutations.length, 1, 'Duplicate clicks cannot start multiple updates');
  assert.equal(JSON.parse(mutations[0].options.body).channel, 'nightly');
  assert.equal(elements['update-yt-dlp'].disabled, true);
  assert.equal(elements['yt-dlp-channel'].disabled, true);
  state = {...state, busy: false, version: '2026.10.01', channel: 'nightly', source: 'persistent', rollback_available: true, rollback_version: '2026.08.19', phase: 'SUCCEEDED'};
  await poll();
  assert.equal(elements['update-yt-dlp'].disabled, false);
  assert.equal(elements['rollback-yt-dlp'].disabled, false);
  assert.match(elements['yt-dlp-version'].textContent, /2026.10.01/);
  elements['rollback-yt-dlp'].handlers.click();
  await settle();
  assert.equal(mutations[1].url, '/api/settings/yt-dlp/rollback');
  assert.match(elements['yt-dlp-version'].textContent, /2026.08.19/);
  state = {...state, phase: 'FAILED', message: 'Update failed'};
  await poll();
  assert.equal(elements['yt-dlp-message'].style.color, 'var(--red)');
  assert.equal(elements['update-yt-dlp'].disabled, false);
  console.log('Engine update UI tests passed');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
