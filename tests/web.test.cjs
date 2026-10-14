const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

function dashboard(firstArg = {}, animationFrame = callback => callback()) {
  const isFn = typeof firstArg === 'function';
  const fetchRequest = isFn ? firstArg : (firstArg && firstArg.fetchRequest);
  const overrides = isFn ? {} : (firstArg || {});
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    innerHTML: '', textContent: '', value: '', hidden: false, disabled: false, title: '', dataset: {}, listeners: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, listener) { this.listeners[type] = listener; },
    setAttribute() {}, focus() {},
  }]));
  const document = {
    body: elements.get('list'),
    getElementById(id) {
      assert.ok(elements.has(id), `Missing element: ${id}`);
      return elements.get(id);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  let nowMs = 0;
  const win = { ...(overrides.window || {}) };
  const nav = overrides.navigator || win.navigator || {};
  win.navigator = nav;
  const context = vm.createContext({
    window: win, navigator: nav, location: win.location,
    document, performance: { now: () => nowMs },
    fetch: (url, options) => (url === '/api/fleet' || !fetchRequest) ? new Promise(() => {}) : fetchRequest(url, options),
    setInterval() {}, requestAnimationFrame: animationFrame, CSS: { escape: value => String(value) },
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script, context);
  return {
    elements,
    context,
    setNow(ms) { nowMs = ms; },
    render: data => context.render(data),
    open: agent => context.openSheet(agent),
    close: () => context.closeSheet(),
    refreshPreview: () => elements.get('sheet-refresh').listeners.click(),
    visibility: state => { document.visibilityState = state; },
  };
}

function agent(host, state, tracked = true, pending = false) {
  return { host, state, tracked, pending, pane: '%1', pid: '123' };
}

function hostNames(element) {
  return [...element.innerHTML.matchAll(/data-host="([^"]*)"/g)].map(([, name]) => name);
}

test('host counts exclude shells and sort active hosts before empty/offline hosts', () => {
  const ui = dashboard();
  const hosts = [
    { name: 'offline', ok: false }, { name: 'idle', ok: true },
    { name: 'alpha', ok: true }, { name: 'beta', ok: true },
  ];
  ui.render({ hosts, agents: [
    agent('alpha', 'RUNNING'), agent('beta', 'WAIT'), agent('beta', 'IDLE', true, true),
    agent('idle', 'SHELL'), agent('idle', 'RUNNING', false),
  ] });
  const pills = ui.elements.get('hosts');
  assert.deepEqual(hostNames(pills), ['', 'beta', 'alpha', 'idle', 'offline']);
  assert.match(pills.innerHTML, /全部<span class="count">\(3\)/);
  assert.match(pills.innerHTML, /class="host-pill tone-\d empty"[^>]*>idle.*?\(0\)/);
  assert.match(pills.innerHTML, /class="host-pill tone-\d bad"[^>]*>offline offline/);
  assert.deepEqual(hosts.map(host => host.name), ['offline', 'idle', 'alpha', 'beta']);
});

test('host filter updates summary and cards while keeping fleet counts', () => {
  const ui = dashboard();
  const hosts = [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }];
  ui.render({ hosts, agents: [agent('alpha', 'WAIT'), agent('beta', 'RUNNING')] });
  const click = name => ui.elements.get('hosts').listeners.click({
    target: { closest: () => ({ dataset: { host: name } }) },
  });
  click('alpha');
  assert.match(ui.elements.get('summary').innerHTML, /🙋 1/);
  assert.match(ui.elements.get('summary').innerHTML, /🏃 0/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /group-running/);
  assert.match(ui.elements.get('hosts').innerHTML, /全部<span class="count">\(2\)/);
  click('');
  assert.match(ui.elements.get('summary').innerHTML, /🏃 1/);
  assert.match(ui.elements.get('list').innerHTML, /group-running/);
});

test('unnamed local agents count against the local host and stale filters clear', () => {
  const ui = dashboard();
  ui.render({ hosts: [{ name: 'old', ok: true }], agents: [agent('old', 'WAIT')] });
  ui.elements.get('hosts').listeners.click({
    target: { closest: () => ({ dataset: { host: 'old' } }) },
  });
  ui.render({ hosts: [{ name: 'local-machine', source: 'local', ok: true }],
    agents: [agent('', 'IDLE', true, true)] });
  assert.deepEqual(hostNames(ui.elements.get('hosts')), ['local-machine']);
  assert.match(ui.elements.get('hosts').innerHTML, /local-machine.*?\(1\)/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.doesNotMatch(ui.elements.get('meta').textContent, /篩選/);
});

test('host labels and attributes escape HTML', () => {
  const ui = dashboard();
  const name = '<work>"&';
  ui.render({ hosts: [{ name, ok: true }], agents: [] });
  const pills = ui.elements.get('hosts').innerHTML;
  assert.match(pills, /data-host="&lt;work&gt;&quot;&amp;"/);
  assert.doesNotMatch(pills, /<work>/);
});

test('review cards retain review labels without restoring removed terminal metadata', () => {
  const ui = dashboard(() => new Promise(() => {}));
  const current = { ...agent('work-machine', 'IDLE', true, true), cli: 'codex', session: 'build-session' };
  ui.render({ agents: [current] });
  const card = ui.elements.get('list').innerHTML;
  assert.doesNotMatch(card, /class="host tone-|class="cli |\/icons\/codex.svg/);
  assert.match(card, /class="project">未命名專案<\/span>/);
  assert.match(card, /class="review-state unread">未看<\/span>/);
  assert.doesNotMatch(card, /class="project">%1<\/span>/);
  ui.open(current);
  assert.equal(ui.elements.has('sheet-target'), false);
  assert.equal(ui.elements.get('sheet-title').textContent, '未命名專案');
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /\.host\.tone-7|\.cli\.broken::after|\.sheet-sub \{|function cliBadge/);
});

function review(project, revision, acknowledged, updated = revision) {
  return { ...agent('work', 'IDLE'), project, pane: `%${project}`, needs_review: true,
    revision, acknowledged, unread: revision > acknowledged, reviewed: revision <= acknowledged,
    pending: revision > acknowledged, updated, preview: { agent: `result ${revision}` } };
}

function response(data, ok = true) {
  return { ok, status: ok ? 200 : 409, json: async () => data };
}

async function settle() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

test('review groups put unread first and sort newest messages inside each group', () => {
  const ui = dashboard();
  ui.render({ agents: [review('seen-old', 1, 1, 10), review('unread-old', 2, 0, 20),
    review('seen-new', 3, 3, 40), review('unread-new', 4, 0, 30)] });
  const list = ui.elements.get('list').innerHTML;
  assert.ok(list.indexOf('data-review="unread"') < list.indexOf('data-review="reviewed"'));
  const projects = [...list.matchAll(/<span class="project">([^<]+)<\/span>/g)].map(([, value]) => value);
  assert.deepEqual(projects, ['unread-new', 'unread-old', 'seen-new', 'seen-old']);
  assert.match(list, /class="card review unread newest"/);
  assert.match(list, /class="card review reviewed"/);
  assert.match(list, /class="review-state unread">未看/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 4/);
  assert.match(ui.elements.get('summary').innerHTML, />未看 2<\/span>/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, />已看|>結果|class="secondary"/);
  assert.match(ui.elements.get('summary').innerHTML, /aria-label="跳到待看，共 4，未看 2，已看 2"/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 0/);
});

test('review navigation remains enabled with only seen results and legacy pending remains unread', () => {
  const ui = dashboard();
  ui.render({ agents: [review('seen', 1, 1)] });
  const summary = ui.elements.get('summary').innerHTML;
  assert.match(summary, /data-group="review" title="跳到待看"/);
  assert.match(summary, /👀 1/);
  assert.doesNotMatch(summary, /review-unread/);
  assert.match(summary, /aria-label="跳到待看，共 1，未看 0，已看 1"/);
  ui.render({ agents: [agent('legacy', 'IDLE', true, true)] });
  assert.match(ui.elements.get('list').innerHTML, /data-review="unread"/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('summary').innerHTML, />未看 1<\/span>/);
  ui.render({ agents: [] });
  assert.match(ui.elements.get('summary').innerHTML, /data-group="review" disabled/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 0/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /review-unread/);
});

test('review total and unread hint reflect only the selected hosts', () => {
  const ui = dashboard();
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [
    { ...review('alpha-unread', 2, 0), host: 'alpha' },
    { ...review('alpha-seen', 1, 1), host: 'alpha' },
    { ...review('beta-seen', 1, 1), host: 'beta' },
  ] });
  const click = name => ui.elements.get('hosts').listeners.click({
    target: { closest: () => ({ dataset: { host: name } }) },
  });
  assert.match(ui.elements.get('summary').innerHTML, /👀 3/);
  assert.match(ui.elements.get('summary').innerHTML, />未看 1<\/span>/);
  click('alpha');
  assert.match(ui.elements.get('summary').innerHTML, /👀 2/);
  assert.match(ui.elements.get('summary').innerHTML, />未看 1<\/span>/);
  click('alpha');
  click('beta');
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /review-unread/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /alpha-unread|alpha-seen/);
  click('');
  assert.match(ui.elements.get('summary').innerHTML, /👀 3/);
  assert.match(ui.elements.get('summary').innerHTML, />未看 1<\/span>/);
});

test('successful rendered preview acknowledges its exact version and newer fleet content stays unread', async () => {
  const calls = [];
  let resolvePreview;
  const ui = dashboard((url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    if (url === '/api/preview') return new Promise(resolve => { resolvePreview = resolve; });
    assert.match(ui.elements.get('sheet-preview').innerHTML, /displayed version 2/);
    return Promise.resolve(response({ ok: true, revision: 3, acknowledged: 2 }));
  });
  const current = review('task', 2, 0);
  ui.render({ agents: [current] });
  ui.open(current);
  ui.render({ agents: [review('task', 3, 0)] });
  resolvePreview(response({ ok: true, preview: { agent: 'displayed version 2' }, revision: 2, viewable: true }));
  await settle();
  assert.deepEqual(calls.map(call => call.url), ['/api/preview', '/api/ack']);
  assert.equal(calls[1].body.revision, 2);
  assert.deepEqual(calls[1].body, { host: 'work', pane: '%task', pid: '123', revision: 2 });
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('sheet-preview').innerHTML, /displayed version 2/);
  ui.close();
  assert.match(ui.elements.get('list').innerHTML, /review unread/);
});

test('successful acknowledgement moves result into seen group, survives stale fleet and does not repeat', async () => {
  const calls = [];
  const ui = dashboard(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return response(url === '/api/preview'
      ? { ok: true, preview: { agent: 'result' }, revision: 2, viewable: true }
      : { ok: true, acknowledged: 2 });
  });
  const current = { ...review('task', 2, 0), emoji: '👀', reason: '已完成，尚未查看（背景工作仍在運行）' };
  ui.render({ agents: [current] });
  ui.open(current);
  await settle();
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /review-unread/);
  assert.match(ui.elements.get('summary').innerHTML, /aria-label="跳到待看，共 1，未看 0，已看 1"/);
  assert.match(ui.elements.get('list').innerHTML, /review reviewed/);
  assert.match(ui.elements.get('list').innerHTML, /aria-hidden="true">✓<\/span>/);
  assert.match(ui.elements.get('list').innerHTML, /已完成，已查看（背景工作仍在運行）/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /尚未查看|👀<\/span>/);
  assert.equal(current.reason, '已完成，尚未查看（背景工作仍在運行）');
  assert.equal(current.emoji, '👀');
  assert.equal(ui.elements.get('sheet-reason').textContent, current.reason);
  ui.render({ agents: [current] });
  ui.refreshPreview();
  await settle();
  assert.equal(calls.filter(call => call.url === '/api/ack').length, 1);
});

test('failed acknowledgement stays unread and retries only on explicit refresh or reopen', async () => {
  let ackAttempts = 0;
  const ui = dashboard(async url => {
    if (url === '/api/preview') return response({ ok: true, preview: { agent: 'result' }, revision: 2, viewable: true });
    ackAttempts += 1;
    return response({ ok: ackAttempts > 1, error: 'offline', acknowledged: 2 }, ackAttempts > 1);
  });
  const current = review('task', 2, 0);
  ui.render({ agents: [current] });
  ui.open(current);
  await settle();
  assert.match(ui.elements.get('sheet-status').textContent, /儲存失敗/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  ui.render({ agents: [current] });
  await settle();
  assert.equal(ackAttempts, 1);
  ui.refreshPreview();
  await settle();
  assert.equal(ackAttempts, 2);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /review-unread/);
});

test('failed, cached, empty or action-needed previews never acknowledge', async () => {
  for (const data of [
    { ok: false, error: 'changed', revision: 2, viewable: true },
    { ok: true, preview: {}, revision: 2, viewable: false },
    { ok: true, preview: { agent: 'cached old result' }, revision: 2, viewable: false },
    { ok: true, preview: { agent: 'waiting approval' }, revision: 2, viewable: false },
    { ok: true, preview: { agent: 'legacy result' } },
  ]) {
    const calls = [];
    const ui = dashboard(async url => {
      calls.push(url);
      return response(data, data.ok);
    });
    const current = review('task', 2, 0);
    ui.render({ agents: [current] });
    ui.open(current);
    await settle();
    assert.deepEqual(calls, ['/api/preview']);
    assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
    if (data.viewable === false) {
      assert.equal(ui.elements.get('sheet-status').textContent, '最新內容尚未就緒，請稍後重新整理。');
      assert.equal(ui.elements.get('sheet-status').className, 'sheet-feedback');
    }
  }
});

test('not-ready feedback only appears for unread review results with explicit viewable false', async () => {
  for (const current of [agent('work', 'WAIT', true, true), review('seen', 1, 1)]) {
    const ui = dashboard(async () => response({ ok: true, preview: { agent: 'content' }, revision: 1, viewable: false }));
    ui.render({ agents: [current] });
    ui.open(current);
    await settle();
    assert.equal(ui.elements.get('sheet-status').textContent, '');
  }
  const current = agent('legacy', 'IDLE', true, true);
  const ui = dashboard(async () => response({ ok: true, preview: { agent: 'legacy content' } }));
  ui.render({ agents: [current] });
  ui.open(current);
  await settle();
  assert.equal(ui.elements.get('sheet-status').textContent, '');
});

test('closed and switched sheets ignore late preview responses without acknowledging or showing errors', async () => {
  for (const switchSheet of [false, true]) {
    const calls = [];
    const pending = [];
    const ui = dashboard((url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return new Promise(resolve => pending.push(resolve));
    });
    const first = review('first', 1, 0);
    const second = review('second', 2, 0);
    ui.render({ agents: [first, second] });
    ui.open(first);
    if (switchSheet) ui.open(second);
    else ui.close();
    pending[0](response({ ok: true, preview: { agent: 'stale first' }, revision: 1, viewable: true }));
    await settle();
    assert.equal(calls.filter(call => call.url === '/api/ack').length, 0);
    assert.doesNotMatch(ui.elements.get('sheet-preview').innerHTML, /stale first/);
    assert.equal(ui.elements.get('sheet-status').textContent, '');
  }
});

test('polling keeps open sheet content and metadata stable until explicit refresh', async () => {
  let revision = 1;
  let previews = 0;
  const ui = dashboard(async url => {
    if (url === '/api/ack') return response({ ok: true, acknowledged: revision });
    previews += 1;
    return response({ ok: true, preview: { agent: `full result ${revision}` }, revision, viewable: true });
  });
  const current = { ...review('task', 1, 0), reason: 'first reason', branch: 'first branch' };
  ui.render({ agents: [current] });
  ui.open(current);
  await settle();
  revision = 2;
  ui.render({ agents: [{ ...review('task', 2, 0), reason: 'second reason', branch: 'second branch' }] });
  assert.equal(previews, 1);
  assert.match(ui.elements.get('sheet-preview').innerHTML, /full result 1/);
  assert.equal(ui.elements.get('sheet-reason').textContent, 'first reason');
  assert.match(ui.elements.get('sheet-env').innerHTML, /first branch/);
  assert.match(ui.elements.get('sheet-refresh').textContent, /有新訊息/);
  ui.refreshPreview();
  await settle();
  assert.equal(previews, 2);
  assert.match(ui.elements.get('sheet-preview').innerHTML, /full result 2/);
  assert.equal(ui.elements.get('sheet-reason').textContent, 'second reason');
});

test('acknowledgement waits for content to paint and skips hidden or closed sheets', async () => {
  for (const state of ['open', 'closed', 'hidden']) {
    const frames = [];
    const calls = [];
    const ui = dashboard(async url => {
      calls.push(url);
      return response(url === '/api/preview'
        ? { ok: true, preview: { agent: 'rendered result' }, revision: 1, viewable: true }
        : { ok: true, acknowledged: 1 });
    }, callback => frames.push(callback));
    const current = review('task', 1, 0);
    ui.render({ agents: [current] });
    ui.open(current);
    await settle();
    assert.match(ui.elements.get('sheet-preview').innerHTML, /rendered result/);
    assert.deepEqual(calls, ['/api/preview']);
    // Opening also schedules textarea focus; no acknowledgement until two content frames.
    while (frames.length > 1) frames.shift()();
    frames.shift()();
    if (state === 'closed') ui.close();
    if (state === 'hidden') ui.visibility('hidden');
    frames.shift()();
    await settle();
    assert.equal(calls.filter(url => url === '/api/ack').length, state === 'open' ? 1 : 0);
  }
});

test('preview network failure preserves unread and can retry on reopen', async () => {
  let attempts = 0;
  const calls = [];
  const ui = dashboard(async url => {
    calls.push(url);
    if (url === '/api/preview' && attempts++ === 0) throw new Error('offline');
    return response(url === '/api/preview'
      ? { ok: true, preview: { agent: 'result' }, revision: 1, viewable: true }
      : { ok: true, acknowledged: 1 });
  });
  const current = review('task', 1, 0);
  ui.render({ agents: [current] });
  ui.open(current);
  await settle();
  assert.match(ui.elements.get('sheet-status').textContent, /預覽載入失敗/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.deepEqual(calls, ['/api/preview']);
  ui.close();
  ui.open(current);
  await settle();
  assert.deepEqual(calls, ['/api/preview', '/api/preview', '/api/ack']);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /review-unread/);
});

test('transient host failure retains last-known-good sessions until stale TTL expires', () => {
  const ui = dashboard();
  ui.setNow(1000);
  ui.render({
    hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }],
    agents: [agent('alpha', 'WAIT'), agent('beta', 'RUNNING')],
  });
  assert.match(ui.elements.get('hosts').innerHTML, /全部<span class="count">\(2\)/);

  ui.setNow(11000);
  ui.render({
    hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: false, error: 'TimeoutError' }],
    agents: [agent('alpha', 'WAIT')],
  });
  assert.match(ui.elements.get('hosts').innerHTML, /全部<span class="count">\(2\)/);
  assert.match(ui.elements.get('hosts').innerHTML, /beta stale<span class="count">\(1\)/);
  assert.match(ui.elements.get('list').innerHTML, /group-running/);

  ui.setNow(50000);
  ui.render({
    hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: false, error: 'TimeoutError' }],
    agents: [agent('alpha', 'WAIT')],
  });
  assert.match(ui.elements.get('hosts').innerHTML, /全部<span class="count">\(1\)/);
  assert.match(ui.elements.get('hosts').innerHTML, /beta offline<span class="count">\(0\)/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /group-running/);
});

test('microphone click requests getUserMedia permission before starting speech recognition', async () => {
  let getUserMediaCalls = 0;
  let trackStopped = 0;
  let speechStarted = 0;
  class FakeSpeech {
    start() {
      speechStarted += 1;
      if (this.onstart) this.onstart();
    }
    stop() {}
  }
  const ui = dashboard({
    window: { SpeechRecognition: FakeSpeech, isSecureContext: true },
    navigator: {
      language: 'zh-TW',
      mediaDevices: {
        async getUserMedia(constraints) {
          assert.equal(constraints && constraints.audio, true);
          getUserMediaCalls += 1;
          return { getTracks: () => [{ stop() { trackStopped += 1; } }] };
        },
      },
    },
  });
  await ui.elements.get('sheet-mic').listeners.click();
  assert.equal(getUserMediaCalls, 1);
  assert.equal(trackStopped, 1);
  assert.equal(speechStarted, 1);

  let resolveDelayed;
  let delayedStarts = 0;
  class DelayedSpeech {
    start() { delayedStarts += 1; }
    stop() {}
  }
  const closeUi = dashboard({
    window: { SpeechRecognition: DelayedSpeech, isSecureContext: true },
    navigator: {
      mediaDevices: {
        getUserMedia: () => new Promise(resolve => { resolveDelayed = resolve; }),
      },
    },
  });
  closeUi.open(agent('alpha', 'WAIT'));
  const pendingClick = closeUi.elements.get('sheet-mic').listeners.click();
  closeUi.close();
  resolveDelayed({ getTracks: () => [{ stop() {} }] });
  await pendingClick;
  assert.equal(delayedStarts, 0);
});

test('microphone click explains insecure HTTP context or blocked permission instead of hanging', async () => {
  let speechStarted = 0;
  class FakeSpeech {
    start() { speechStarted += 1; }
    stop() {}
  }
  const insecureUi = dashboard({
    window: {
      SpeechRecognition: FakeSpeech,
      isSecureContext: false,
      location: { protocol: 'http:', hostname: '100.64.0.1' },
    },
  });
  await insecureUi.elements.get('sheet-mic').listeners.click();
  assert.equal(speechStarted, 0);
  assert.match(insecureUi.elements.get('sheet-status').textContent, /HTTPS|localhost/);

  const deniedUi = dashboard({
    window: { SpeechRecognition: FakeSpeech, isSecureContext: true },
    navigator: {
      mediaDevices: {
        async getUserMedia() {
          const err = new Error('Permission denied');
          err.name = 'NotAllowedError';
          throw err;
        },
      },
    },
  });
  await deniedUi.elements.get('sheet-mic').listeners.click();
  assert.equal(speechStarted, 0);
  assert.match(deniedUi.elements.get('sheet-status').textContent, /已被瀏覽器封鎖|允許麥克風/);
});
