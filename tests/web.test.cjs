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
    listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; },
  };
  Object.assign(document, overrides.document || {});
  let nowMs = 0;
  const win = { location: { hash: '' }, addEventListener() {}, history: {}, ...(overrides.window || {}) };
  const nav = overrides.navigator || win.navigator || {};
  win.navigator = nav;
  const context = vm.createContext({
    window: win, navigator: nav, location: win.location,
    document, performance: { now: () => nowMs },
    fetch: (url, options) => (url === '/api/fleet' || !fetchRequest) ? new Promise(() => {}) : fetchRequest(url, options),
    setInterval() {}, setTimeout() {}, requestAnimationFrame: animationFrame, CSS: { escape: value => String(value) },
    ...(overrides.globals || {}),
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script, context);
  return {
    elements,
    context,
    document,
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

test('review cards show the agent icon without restoring host chips or sheet target text', () => {
  const ui = dashboard(() => new Promise(() => {}));
  const current = { ...agent('work-machine', 'IDLE', true, true), cli: 'codex', session: 'build-session' };
  ui.render({ agents: [current] });
  const card = ui.elements.get('list').innerHTML;
  assert.match(card, /class="cli codex"[^>]*>.*?src="\/icons\/codex\.svg"/);
  assert.doesNotMatch(card, /class="host tone-/);
  assert.match(card, /class="project">未命名專案<\/span>/);
  assert.match(card, /class="review-state unread">未看<\/span>/);
  assert.doesNotMatch(card, /class="project">%1<\/span>/);
  ui.open(current);
  assert.equal(ui.elements.has('sheet-target'), false);
  assert.equal(ui.elements.get('sheet-title').textContent, '👀 未命名專案');
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /\.host\.tone-7|\.sheet-sub \{/);
});

test('cards map every supported coding agent to its icon and skip non-agent panes', () => {
  const ui = dashboard(() => new Promise(() => {}));
  const expected = { claude: 'claude', codex: 'codex', pi: 'pi', agent: 'agent', cursor: 'agent' };
  for (const [cli, icon] of Object.entries(expected)) {
    ui.render({ agents: [{ ...agent('', 'RUNNING'), cli }] });
    assert.match(ui.elements.get('list').innerHTML, new RegExp(`class="cli ${icon}"[^>]*>.*?src="/icons/${icon}\\.svg"`), cli);
  }
  for (const cli of ['shell', 'zsh', '', 'constructor', '__proto__', 'toString', 'hasOwnProperty']) {
    ui.render({ agents: [{ ...agent('', 'RUNNING', false), cli }] });
    assert.doesNotMatch(ui.elements.get('list').innerHTML, /class="cli /, `non-agent ${cli}`);
  }
});

function review(project, revision, acknowledged, updated = revision) {
  return { ...agent('work', 'IDLE'), project, pane: `%${project}`, needs_review: true,
    category: revision > acknowledged ? 'review' : 'idle',
    revision, acknowledged, unread: revision > acknowledged, reviewed: revision <= acknowledged,
    pending: revision > acknowledged, updated, preview: { agent: `result ${revision}` } };
}

function response(data, ok = true) {
  return { ok, status: ok ? 200 : 409, json: async () => data };
}

async function settle() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

test('unread results stay in review and viewed results move to idle, newest first', () => {
  const ui = dashboard();
  ui.render({ agents: [review('seen-old', 1, 1, 10), review('unread-old', 2, 0, 20),
    review('seen-new', 3, 3, 40), review('unread-new', 4, 0, 30)] });
  const list = ui.elements.get('list').innerHTML;
  assert.ok(list.indexOf('id="group-review"') < list.indexOf('id="group-idle"'));
  const projects = [...list.matchAll(/<span class="project">([^<]+)<\/span>/g)].map(([, value]) => value);
  assert.deepEqual(projects, ['unread-new', 'unread-old', 'seen-new', 'seen-old']);
  assert.match(list, /class="card review unread newest"/);
  assert.match(list, /class="card idle"/);
  assert.match(list, /class="review-state unread">未看/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 2/);
  assert.match(ui.elements.get('summary').innerHTML, /class="l">待看<\/div>/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, />未看|>已看|>結果|class="secondary"/);
  assert.match(ui.elements.get('summary').innerHTML, /aria-label="待看 2，跳到待看"/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 2/);
});

test('only viewed results disable review navigation and enable idle; legacy pending stays unread', () => {
  const ui = dashboard();
  ui.render({ agents: [review('seen', 1, 1)] });
  const summary = ui.elements.get('summary').innerHTML;
  assert.match(summary, /data-group="review" disabled/);
  assert.match(summary, /👀 0/);
  assert.match(summary, /💤 1/);
  assert.match(summary, /data-group="idle" title="跳到閒置"/);
  assert.match(summary, /class="l">待看<\/div>/);
  ui.render({ agents: [agent('legacy', 'IDLE', true, true)] });
  assert.match(ui.elements.get('list').innerHTML, /class="card review unread newest"/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 0/);
  ui.render({ agents: [] });
  assert.match(ui.elements.get('summary').innerHTML, /data-group="review" disabled/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 0/);
  assert.match(ui.elements.get('summary').innerHTML, /class="l">待看<\/div>/);
});

test('review and idle counts reflect only selected hosts', () => {
  const ui = dashboard();
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [
    { ...review('alpha-unread', 2, 0), host: 'alpha' },
    { ...review('alpha-seen', 1, 1), host: 'alpha' },
    { ...review('beta-seen', 1, 1), host: 'beta' },
  ] });
  const click = name => ui.elements.get('hosts').listeners.click({
    target: { closest: () => ({ dataset: { host: name } }) },
  });
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 2/);
  click('alpha');
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
  click('alpha');
  click('beta');
  assert.match(ui.elements.get('summary').innerHTML, /👀 0/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /alpha-unread|alpha-seen/);
  click('');
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 2/);
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
  assert.match(ui.elements.get('sheet-title').textContent, /^💤 task/);
  ui.close();
  assert.match(ui.elements.get('list').innerHTML, /review unread/);
});

test('successful acknowledgement moves result into idle, survives stale fleet and does not repeat', async () => {
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
  assert.match(ui.elements.get('summary').innerHTML, /👀 0/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
  assert.match(ui.elements.get('summary').innerHTML, /data-group="review" disabled/);
  assert.match(ui.elements.get('list').innerHTML, /class="card idle selected"/);
  assert.match(ui.elements.get('list').innerHTML, /aria-hidden="true">💤<\/span>/);
  assert.match(ui.elements.get('list').innerHTML, /class="review-state">已看<\/span>/);
  assert.match(ui.elements.get('list').innerHTML, /已完成，已查看（背景工作仍在運行）/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /尚未查看|👀<\/span>/);
  assert.equal(current.reason, '已完成，尚未查看（背景工作仍在運行）');
  assert.equal(current.emoji, '👀');
  assert.equal(ui.elements.get('sheet-reason').textContent, '已完成，已查看（背景工作仍在運行）');
  assert.match(ui.elements.get('sheet-title').textContent, /^💤 task/);
  ui.render({ agents: [current] });
  ui.refreshPreview();
  await settle();
  assert.equal(calls.filter(call => call.url === '/api/ack').length, 1);
  ui.close();
  ui.open(current);
  await settle();
  assert.match(ui.elements.get('sheet-title').textContent, /^💤 task/);
  assert.equal(ui.elements.get('sheet-reason').textContent, '已完成，已查看（背景工作仍在運行）');
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
  assert.match(ui.elements.get('summary').innerHTML, /👀 0/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
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

test('refreshing or reopening the same version waits for its in-flight acknowledgement', async () => {
  for (const reopen of [false, true]) {
    for (const ok of [false, true]) {
      let resolveAck;
      let ackCalls = 0;
      const current = { ...review('task', 2, 0), reason: '已完成，尚未查看' };
      const ui = dashboard(url => {
        if (url === '/api/ack') {
          ackCalls += 1;
          return new Promise(resolve => { resolveAck = resolve; });
        }
        return Promise.resolve(response({ ok: true, preview: { agent: 'version 2' }, revision: 2, viewable: true }));
      });
      ui.render({ agents: [current] });
      ui.open(current);
      await settle();
      if (reopen) { ui.close(); ui.open(current); }
      else ui.refreshPreview();
      await settle();
      resolveAck(response({ ok, acknowledged: ok ? 2 : 0 }, ok));
      await settle();
      assert.equal(ackCalls, 1);
      assert.equal(ui.elements.get('sheet-title').textContent, `${ok ? '💤' : '👀'} task`);
      assert.equal(ui.elements.get('sheet-reason').textContent, ok ? '已完成，已查看' : current.reason);
      assert.match(ui.elements.get('sheet-preview').innerHTML, /version 2/);
      if (!ok) assert.match(ui.elements.get('sheet-status').textContent, /儲存失敗/);
    }
  }
});

test('late acknowledgement updates its result without changing a different open sheet', async () => {
  let resolveAck;
  const first = { ...review('first', 1, 0), reason: 'first reason' };
  const second = { ...review('second', 2, 0), reason: 'second reason' };
  const ui = dashboard((url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/ack') return new Promise(resolve => { resolveAck = resolve; });
    return Promise.resolve(response({ ok: true, preview: { agent: body.pane },
      revision: body.pane === first.pane ? 1 : 2, viewable: body.pane === first.pane }));
  });
  ui.render({ agents: [first, second] });
  ui.open(first);
  await settle();
  ui.open(second);
  await settle();
  resolveAck(response({ ok: true, acknowledged: 1 }));
  await settle();
  assert.equal(ui.elements.get('sheet-title').textContent, '👀 second');
  assert.equal(ui.elements.get('sheet-reason').textContent, 'second reason');
  assert.match(ui.elements.get('sheet-preview').innerHTML, /%second/);
  assert.match(ui.elements.get('summary').innerHTML, /👀 1/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
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
  assert.match(ui.elements.get('summary').innerHTML, /👀 0/);
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
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

test('reply composer has no microphone control or speech-recognition code', () => {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /sheet-mic|voice-hint|SpeechRecognition|webkitSpeechRecognition|getUserMedia|麥克風/);
  const ui = dashboard();
  assert.equal(ui.elements.has('sheet-mic'), false);
  assert.equal(ui.elements.has('voice-hint'), false);
  assert.doesNotMatch(ui.elements.get('sheet-text').placeholder || html.match(/<textarea[^>]*>/)[0], /語音|麥克風/);
});

const flush = () => new Promise(resolve => setTimeout(resolve, 0));
const imageFile = (name, type = 'image/jpeg', outSize = 1000) => ({ name, type, size: 1000, outSize });
const submitEvent = () => ({ preventDefault() {} });

const PEER_OUTDATED = 'peer is running an older tmux-attn without this feature';

function uploader({ failFor = [], holdUploads = false, holdSend = false, holdPrepare = false, outdatedPeer = false } = {}) {
  const calls = [];
  const revoked = [];
  const held = [];
  const heldSends = [];
  const heldPrepares = [];
  const ui = dashboard({
    globals: { URL: { createObjectURL: file => `blob:${file.name}`, revokeObjectURL: url => revoked.push(url) } },
    fetchRequest: async (url, options) => {
      if (url === '/api/preview') return new Promise(() => {});
      const body = JSON.parse(options.body);
      calls.push({ url, body });
      if (url === '/api/upload') {
        const name = body.data.replace(/^b64-/, '');
        const reply = outdatedPeer
          ? { ok: false, status: 502, json: async () => ({ ok: false, error: PEER_OUTDATED }) }
          : failFor.includes(name)
          ? { ok: false, status: 400, json: async () => ({ ok: false, error: 'unsupported image type' }) }
          : { ok: true, status: 200, json: async () => ({ ok: true, id: `id-${name}` }) };
        if (!holdUploads) return reply;
        return new Promise(resolve => held.push(() => resolve(reply)));
      }
      const sent = { ok: true, status: 200, json: async () => ({ ok: true }) };
      if (url === '/api/send' && holdSend) return new Promise(resolve => heldSends.push(() => resolve(sent)));
      return sent;
    },
  });
  ui.context.prepareImage = async file => {
    if (holdPrepare) await new Promise(resolve => heldPrepares.push(resolve));
    return { tag: file.name, size: file.outSize };
  };
  ui.context.blobToBase64 = async blob => `b64-${blob.tag}`;
  const current = { ...agent('lab', 'WAIT'), project: 'demo' };
  ui.render({ agents: [current] });
  ui.open(current);
  const pick = async files => {
    ui.elements.get('sheet-file').files = files;
    await ui.elements.get('sheet-file').listeners.change();
    await flush();
  };
  const submit = async () => { await ui.elements.get('sheet-form').listeners.submit(submitEvent()); await flush(); };
  const sends = () => calls.filter(call => call.url === '/api/send');
  const uploads = () => calls.filter(call => call.url === '/api/upload');
  const chips = () => ui.elements.get('sheet-attachments').innerHTML;
  const chipKeys = () => [...chips().matchAll(/data-key="([^"]+)"/g)].map(([, key]) => key);
  const remove = key => ui.elements.get('sheet-attachments').listeners.click({
    target: { closest: () => ({ dataset: { key } }) },
  });
  return { ui, calls, revoked, held, heldSends, heldPrepares, current, pick, submit, sends, uploads, chips, chipKeys, remove };
}

test('attached images upload to the pane host and their ids ride along with the message', async () => {
  const t = uploader();
  await t.pick([imageFile('a.jpg'), imageFile('b.png', 'image/png')]);
  assert.deepEqual(t.uploads().map(call => call.body), [
    { host: 'lab', pane: '%1', pid: '123', data: 'b64-a.jpg' },
    { host: 'lab', pane: '%1', pid: '123', data: 'b64-b.png' },
  ]);
  assert.equal(t.chipKeys().length, 2);
  assert.doesNotMatch(t.chips(), /uploading|error/);
  t.ui.elements.get('sheet-text').value = 'look at these';
  await t.submit();
  assert.equal(t.sends().length, 1);
  assert.deepEqual(t.sends()[0].body,
    { host: 'lab', pane: '%1', pid: '123', text: 'look at these', submit: true, images: ['id-a.jpg', 'id-b.png'] });
  assert.equal(t.chipKeys().length, 0);
  assert.deepEqual(t.revoked.sort(), ['blob:a.jpg', 'blob:b.png']);
});

test('an image alone is a valid message', async () => {
  const t = uploader();
  await t.pick([imageFile('only.jpg')]);
  await t.submit();
  assert.deepEqual(t.sends()[0].body.images, ['id-only.jpg']);
  assert.equal(t.sends()[0].body.text, '');
});

test('sending waits for in-flight uploads instead of dropping them', async () => {
  const t = uploader({ holdUploads: true });
  await t.pick([imageFile('slow.jpg')]);
  assert.match(t.chips(), /attachment uploading/);
  t.ui.elements.get('sheet-text').value = 'hi';
  await t.submit();
  assert.equal(t.sends().length, 0);
  assert.match(t.ui.elements.get('sheet-status').textContent, /上傳中/);
  t.held.forEach(release => release());
  await flush();
  assert.doesNotMatch(t.chips(), /uploading/);
  await t.submit();
  assert.deepEqual(t.sends()[0].body.images, ['id-slow.jpg']);
});

test('a failed upload blocks sending until the image is removed', async () => {
  const t = uploader({ failFor: ['bad.jpg'] });
  await t.pick([imageFile('ok.jpg'), imageFile('bad.jpg')]);
  assert.match(t.chips(), /attachment error/);
  t.ui.elements.get('sheet-text').value = 'hi';
  await t.submit();
  assert.equal(t.sends().length, 0);
  assert.match(t.ui.elements.get('sheet-status').textContent, /unsupported image type|上傳失敗/);
  t.remove(t.chipKeys()[1]);
  await t.submit();
  assert.deepEqual(t.sends()[0].body.images, ['id-ok.jpg']);
});

test('only images are accepted and at most four stay attached', async () => {
  const t = uploader();
  await t.pick([imageFile('notes.pdf', 'application/pdf'), imageFile('1.jpg'), imageFile('2.jpg'),
    imageFile('3.jpg'), imageFile('4.jpg'), imageFile('5.jpg')]);
  assert.equal(t.uploads().length, 4);
  assert.equal(t.chipKeys().length, 4);
  assert.match(t.ui.elements.get('sheet-status').textContent, /最多 4 張|只支援圖片/);
  await t.pick([imageFile('6.jpg')]);
  assert.equal(t.uploads().length, 4);
});

test('closing the sheet discards attachments and ignores late upload results', async () => {
  const t = uploader({ holdUploads: true });
  await t.pick([imageFile('late.jpg')]);
  t.ui.close();
  assert.deepEqual(t.revoked, ['blob:late.jpg']);
  assert.equal(t.chipKeys().length, 0);
  t.held.forEach(release => release());
  await flush();
  assert.equal(t.chipKeys().length, 0);
  t.ui.open(t.current);
  assert.equal(t.chipKeys().length, 0);
});

test('an image that is still too large after downscaling is rejected before any upload', async () => {
  const t = uploader();
  await t.pick([imageFile('huge.png', 'image/png', 9 * 1024 * 1024)]);
  assert.equal(t.uploads().length, 0);
  assert.match(t.chips(), /attachment error/);
  t.ui.elements.get('sheet-text').value = 'hi';
  await t.submit();
  assert.equal(t.sends().length, 0);
});

test('the wait-for-upload notice goes away once uploads finish', async () => {
  const t = uploader({ holdUploads: true });
  await t.pick([imageFile('slow.jpg')]);
  t.ui.elements.get('sheet-text').value = 'hi';
  await t.submit();
  assert.match(t.ui.elements.get('sheet-status').textContent, /上傳中/);
  t.held.forEach(release => release());
  await flush();
  assert.equal(t.ui.elements.get('sheet-status').textContent, '');
});

test('images cannot be added while a send is in flight', async () => {
  const t = uploader({ holdSend: true });
  await t.pick([imageFile('a.jpg')]);
  const pending = t.ui.elements.get('sheet-form').listeners.submit(submitEvent());
  await flush();
  assert.equal(t.ui.elements.get('sheet-attach').disabled, true);
  t.heldSends.forEach(release => release());
  await pending;
  assert.equal(t.ui.elements.get('sheet-attach').disabled, false);
});

test('a sheet closed while an image is still being prepared never uploads it', async () => {
  const t = uploader({ holdPrepare: true });
  await t.pick([imageFile('late.jpg')]);
  t.ui.close();
  t.heldPrepares.forEach(release => release());
  await flush();
  assert.equal(t.uploads().length, 0);
});

// ---- full-message reader -------------------------------------------------

function fakeBrowser() {
  const listeners = {};
  const state = { hash: '', stack: [''], selection: '' };
  const fire = () => Promise.resolve().then(() => listeners.hashchange && listeners.hashchange());
  return {
    state,
    listeners,
    window: {
      addEventListener(type, fn) { listeners[type] = fn; },
      getSelection: () => ({ toString: () => state.selection }),
      location: {
        pathname: '/', search: '',
        get hash() { return state.hash; },
        set hash(value) { state.hash = value; state.stack.push(value); fire(); },
      },
      history: {
        back() { state.stack.pop(); state.hash = state.stack[state.stack.length - 1]; fire(); },
        replaceState(_state, _title, url) {
          state.hash = String(url).includes('#') ? '#' + String(url).split('#')[1] : '';
          state.stack[state.stack.length - 1] = state.hash;
        },
      },
    },
  };
}

const jsonReply = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
const PREVIEW = { ok: true, revision: 3, viewable: false,
  preview: { users: ['first prompt', 'second prompt'], agent: 'agent answer '.repeat(30) } };

function readerSetup({ message, startHash = '', clipboard, storage } = {}) {
  const calls = [];
  const browser = fakeBrowser();
  browser.state.hash = startHash;
  browser.state.stack = [startHash];
  const ui = dashboard({
    window: browser.window,
    globals: storage ? { sessionStorage: storage } : {},
    navigator: clipboard ? { clipboard } : {},
    fetchRequest: async (url, options) => {
      const body = JSON.parse(options.body);
      calls.push({ url, body });
      if (url === '/api/preview') return jsonReply(PREVIEW);
      if (url === '/api/message') {
        return message ? message(body) : jsonReply({ ok: true, role: body.role, text: `FULL ${body.role} ${body.index}`, chars: 14, truncated: false });
      }
      return jsonReply({ ok: true });
    },
  });
  const current = { ...agent('lab', 'WAIT'), project: 'demo' };
  const el = id => ui.elements.get(id);
  const line = (role, index) => ({ dataset: { role, index: String(index) } });
  const tap = (role, index, extra = {}) => el('sheet-preview').listeners.click({
    target: { closest: selector => selector === 'a[href]' ? (extra.link ? {} : null) : line(role, index) },
  });
  const settle = async () => { await flush(); await flush(); };
  const messages = () => calls.filter(call => call.url === '/api/message');
  return { ui, el, calls, browser, current, tap, settle, messages };
}

async function openSheetWithPreview(t) {
  t.ui.render({ agents: [t.current] });
  t.ui.open(t.current);
  await t.settle();
}

test('sheet messages become tappable only once the full preview has loaded', async () => {
  const t = readerSetup();
  t.ui.render({ agents: [{ ...t.current, preview: { users: ['card prompt'], agent: 'card agent' } }] });
  t.ui.open({ ...t.current, preview: { users: ['card prompt'], agent: 'card agent' } });
  assert.doesNotMatch(t.el('sheet-preview').innerHTML, /data-role/);
  await t.settle();
  const html = t.el('sheet-preview').innerHTML;
  assert.match(html, /data-role="user" data-index="0"/);
  assert.match(html, /data-role="user" data-index="1"/);
  assert.match(html, /data-role="agent" data-index="0"/);
  assert.equal((html.match(/class="open-full"/g) || []).length, 3);
});

test('tapping a message opens the reader on a history entry and loads its full text', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(t.browser.state.hash, '#/m/lab/%251/123/agent/0');
  assert.equal(t.browser.state.stack.length, 2);
  assert.equal(t.el('reader-root').hidden, false);
  assert.equal(t.el('reader-title').textContent, 'Agent 回覆');
  assert.match(t.el('reader-sub').textContent, /demo/);
  assert.match(t.el('reader-sub').textContent, /14 字/);
  assert.match(t.el('reader-body').innerHTML, /FULL agent 0/);
  assert.deepEqual(t.messages()[0].body, {
    host: 'lab', pane: '%1', pid: '123', role: 'agent', index: 0,
    sig: Array.from('agent answer '.repeat(30)).slice(0, 200).join(''),
  });
  await t.tap('user', 1);
  await t.settle();
  assert.equal(t.messages()[1].body.sig, 'second prompt');
  assert.equal(t.el('reader-title').textContent, '你的訊息');
});

test('the browser back button closes the reader but keeps the sheet', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  await t.tap('user', 0);
  await t.settle();
  t.browser.window.history.back();
  await t.settle();
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(t.el('sheet-root').hidden, false);
  assert.equal(t.browser.state.hash, '');
});

async function assertCloseGestureRestoresSheet(gesture) {
  const t = readerSetup();
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(t.browser.state.stack.length, 2);
  await gesture(t);
  await t.settle();
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(t.browser.state.stack.length, 1);
  assert.equal(t.el('sheet-root').hidden, false);
}

test('the reader back button pops the history entry it pushed', async () => {
  await assertCloseGestureRestoresSheet(t => t.el('reader-back').listeners.click());
});

test('Escape closes only the reader, then the sheet', async () => {
  await assertCloseGestureRestoresSheet(t => t.ui.document.listeners.keydown({ key: 'Escape' }));
});

test('taps on links and text selections do not open the reader', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  await t.tap('agent', 0, { link: true });
  assert.equal(t.browser.state.hash, '');
  t.browser.state.selection = 'some selected words';
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(t.browser.state.hash, '');
  assert.equal(t.browser.state.stack.length, 1);
  assert.equal(t.messages().length, 0);
});

test('a conversation that moved on shows a recoverable message instead of the wrong text', async () => {
  const t = readerSetup({ message: () => jsonReply({ ok: false, error: 'content changed; refresh preview' }, 409) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /已有新內容/);
  assert.match(t.el('reader-body').innerHTML, /data-retry/);
  assert.equal(t.el('reader-copy').disabled, true);
});

test('failed loads can be retried', async () => {
  let attempts = 0;
  const t = readerSetup({ message: body => ++attempts === 1
    ? jsonReply({ ok: false, error: 'peer message failed: URLError' }, 502)
    : jsonReply({ ok: true, role: body.role, text: 'second try', chars: 10, truncated: false }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /peer message failed/);
  await t.el('reader-body').listeners.click({ target: { closest: selector => selector === '[data-retry]' ? {} : null } });
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /second try/);
  assert.equal(t.el('reader-copy').disabled, false);
});

test('truncated messages say how much is shown', async () => {
  const t = readerSetup({ message: body => jsonReply({ ok: true, role: body.role, text: 'x'.repeat(50), chars: 300000, truncated: true }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /reader-note/);
  assert.match(t.el('reader-body').innerHTML, /300000/);
});

test('a late response after the reader closed is ignored', async () => {
  let release;
  const t = readerSetup({ message: body => new Promise(resolve => { release = () => resolve(jsonReply({ ok: true, role: body.role, text: 'LATE', chars: 4, truncated: false })); }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  t.browser.window.history.back();
  await t.settle();
  release();
  await t.settle();
  assert.equal(t.el('reader-root').hidden, true);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /LATE/);
});

test('closing the sheet closes the reader and leaves no history entry behind', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  t.ui.close();
  await t.settle();
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(t.browser.state.stack.length, 1);
  assert.equal(t.browser.state.hash, '');
});

test('reply closes the reader and focuses the reply box', async () => {
  const t = readerSetup();
  let focused = 0;
  t.el('sheet-text').focus = () => { focused += 1; };
  await openSheetWithPreview(t);
  focused = 0;
  await t.tap('agent', 0);
  await t.settle();
  await t.el('reader-reply').listeners.click();
  await t.settle();
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(focused, 1);
});

test('a reload on a reader link reopens the sheet and the message once the session list arrives', async () => {
  const t = readerSetup({ startHash: '#/m/lab/%251/123/agent/0' });
  assert.equal(t.messages().length, 0);
  t.ui.render({ agents: [t.current] });
  await t.settle();
  assert.equal(t.el('sheet-root').hidden, false);
  assert.equal(t.el('reader-root').hidden, false);
  assert.match(t.el('reader-body').innerHTML, /FULL agent 0/);
  assert.equal(t.messages()[0].body.sig, '');
  t.ui.render({ agents: [t.current] });
  await t.settle();
  assert.equal(t.messages().length, 1);
});

test('a reader link to a session that no longer exists is dropped', async () => {
  for (const hash of ['#/m/lab/%259/999/agent/0', '#/m/broken', '#/m/lab/%251/123/robot/0', '#/m/lab/%251/123/user/-1']) {
    const t = readerSetup({ startHash: hash });
    t.ui.render({ agents: [t.current] });
    await t.settle();
    assert.equal(t.el('reader-root').hidden, true, hash);
    assert.equal(t.browser.state.hash, '', hash);
    assert.equal(t.messages().length, 0, hash);
  }
});

test('copy puts the full text on the clipboard and reports failure when it cannot', async () => {
  const written = [];
  const t = readerSetup({ clipboard: { writeText: async text => { written.push(text); } } });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  await t.el('reader-copy').listeners.click();
  assert.deepEqual(written, ['FULL agent 0']);
  assert.equal(t.el('reader-status').textContent, '已複製');
  const plain = readerSetup();
  await openSheetWithPreview(plain);
  await plain.tap('agent', 0);
  await plain.settle();
  await plain.el('reader-copy').listeners.click();
  assert.equal(plain.el('reader-status').textContent, '無法複製');
});

test('forward navigation to a reader link reopens the sheet and message from a clean state', async () => {
  const t = readerSetup();
  const other = { ...agent('lab', 'WAIT'), pane: '%2', pid: '456', project: 'other' };
  t.ui.render({ agents: [t.current, other] });
  t.ui.open(t.current);
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  t.browser.window.history.back();
  await t.settle();
  t.ui.close();
  await t.settle();
  t.ui.open(other);
  await t.settle();
  t.el('sheet-text').value = 'draft meant for the other session';
  await t.el('sheet-kill').listeners.click();
  assert.match(t.el('sheet-kill').title, /5 秒內/);
  t.browser.window.location.hash = '#/m/lab/%251/123/agent/0';
  await t.settle();
  assert.equal(t.el('reader-title').textContent, 'Agent 回覆');
  assert.equal(t.el('sheet-text').value, '');
  assert.doesNotMatch(t.el('sheet-kill').title, /5 秒內/);
});

test('a reloaded reader link still notices that the conversation moved on', async () => {
  const store = new Map();
  const storage = { getItem: key => (store.has(key) ? store.get(key) : null), setItem: (key, value) => store.set(key, String(value)) };
  const first = readerSetup({ storage });
  await openSheetWithPreview(first);
  await first.tap('user', 1);
  await first.settle();
  const reloaded = readerSetup({ storage, startHash: '#/m/lab/%251/123/user/1' });
  reloaded.ui.render({ agents: [reloaded.current] });
  await reloaded.settle();
  assert.equal(reloaded.messages()[0].body.sig, 'second prompt');
});

test('a moved-on user message offers no retry that could show a different prompt', async () => {
  const t = readerSetup({ message: () => jsonReply({ ok: false, error: 'content changed; refresh preview' }, 409) });
  await openSheetWithPreview(t);
  await t.tap('user', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /重新點選/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /data-retry/);
});

test('text still syncing from the hook is labelled as partial', async () => {
  const t = readerSetup({ message: body => jsonReply({ ok: true, role: body.role, text: 'clipped…', chars: 8, truncated: true, partial: true }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /還在同步/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /僅顯示前/);
});

test('reader output stays inert even if a peer relays hostile metadata', async () => {
  const t = readerSetup({ message: () => jsonReply({
    ok: true, role: 'agent', text: { length: '<img src=x onerror=alert(1)>' },
    chars: '<img src=x onerror=alert(2)>', truncated: true,
  }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.doesNotMatch(t.el('reader-body').innerHTML, /<img/i);
  assert.doesNotMatch(t.el('reader-sub').textContent, /<img/i);
});

test('wide markdown in a preview scrolls in its own box instead of widening the message column', () => {
  // The vm harness has no layout engine, so guard the two CSS rules that make this work; see the browser check.
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /\.preview-line \{[^}]*grid-template-columns: 2\.4rem minmax\(0, 1fr\)/);
  assert.match(html, /\.preview-line \.text\.md table \{[^}]*display: block;[^}]*overflow-x: auto/);
});

test('a reader link to a host with an older tmux-attn explains what to do and offers no pointless retry', async () => {
  const t = readerSetup({ message: () => jsonReply({ ok: false, error: PEER_OUTDATED }, 502) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /版本較舊/);
  assert.match(t.el('reader-body').innerHTML, /更新/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /data-retry|not found/);
  assert.equal(t.el('reader-copy').disabled, true);
});

test('attaching an image on a host with an older tmux-attn says so on the image', async () => {
  const t = uploader({ outdatedPeer: true });
  await t.pick([imageFile('a.jpg')]);
  assert.match(t.chips(), /attachment error/);
  assert.match(t.chips(), /版本較舊/);
  assert.doesNotMatch(t.chips(), /older tmux-attn/);
});

// ---- card layout --------------------------------------------------------

function cardHtmlFor(overrides) {
  const ui = dashboard(() => new Promise(() => {}));
  ui.render({ agents: [{ ...agent('', 'IDLE', true, true), project: 'demo', ...overrides }] });
  return ui.elements.get('list').innerHTML;
}

test('a card leads with the result and keeps only the latest prompt as context', () => {
  const html = cardHtmlFor({ preview: { users: ['prompt one', 'prompt two', 'prompt three'], agent: 'the finished result' } });
  assert.ok(html.indexOf('the finished result') < html.indexOf('prompt three'), 'result comes first');
  assert.doesNotMatch(html, /prompt one|prompt two/);
  assert.equal((html.match(/class="preview-line (user|agent)"/g) || []).length, 2);
});

test('a card for a running agent shows its latest prompt when there is no result yet', () => {
  const html = cardHtmlFor({ state: 'RUNNING', pending: false, preview: { users: ['older', 'the latest ask'], agent: '' } });
  assert.match(html, /the latest ask/);
  assert.doesNotMatch(html, /older|preview-line agent/);
});

test('the sheet keeps every prompt in order while cards are shortened', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  const html = t.el('sheet-preview').innerHTML;
  assert.ok(html.indexOf('first prompt') < html.indexOf('second prompt'));
  assert.ok(html.indexOf('second prompt') < html.indexOf('agent answer'));
});

test('a reason that only restates the state is left out, one that adds information stays', () => {
  const hidden = [
    ['IDLE', '已完成，尚未查看'], ['IDLE', '已完成，已查看'], ['IDLE', '閒置，等待新任務'], ['RUNNING', '正在工作'],
  ];
  for (const [state, reason] of hidden) {
    assert.doesNotMatch(cardHtmlFor({ state, reason }), /class="reason"/, reason);
  }
  const kept = ['發生錯誤，尚未查看', '已中斷，尚未查看', '已完成，尚未查看（背景工作仍在運行）', '需要批准指令或操作 · rm -rf build'];
  for (const reason of kept) {
    const html = cardHtmlFor({ state: reason.startsWith('需要') ? 'WAIT' : 'IDLE', reason });
    assert.match(html, /class="reason"/, reason);
  }
});

test('previews that overflow their lines are marked so they can fade out', () => {
  const ui = dashboard(() => new Promise(() => {}));
  const toggled = [];
  const preview = (name, scrollHeight, clientHeight) => ({
    scrollHeight, clientHeight, classList: { toggle: (cls, on) => toggled.push([name, cls, on]) },
  });
  ui.document.querySelectorAll = selector => (selector.includes('.preview-line') ? [preview('long', 130, 60), preview('short', 40, 40)] : []);
  ui.render({ agents: [{ ...agent('', 'IDLE', true, true), preview: { users: ['p'], agent: 'a' } }] });
  assert.deepEqual(toggled, [['long', 'clipped', true], ['short', 'clipped', false]]);
});

test('card preview CSS cuts on whole lines, drops the label gutter and keeps labels for screen readers', () => {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /max-height: 4\.9em|max-height: 6\.6em/);
  assert.match(html, /\.card \.preview-line \.text\.md \{[^}]*max-height: calc\(1\.45em \* 5\)/);
  assert.match(html, /\.card \.preview-line\.user \.text\.md \{[^}]*max-height: calc\(1\.45em \* 2\)/);
  assert.match(html, /\.card \.preview-line \{[^}]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(html, /\.card \.preview-line \.who \{[^}]*clip:/);
  assert.match(html, /\.text\.md\.clipped \{[^}]*mask-image/);
});

test('clipped previews are re-measured after web fonts load and when the window is resized', () => {
  const fontListeners = {};
  const browser = fakeBrowser();
  const ui = dashboard({
    window: browser.window,
    document: { fonts: { addEventListener: (type, fn) => { fontListeners[type] = fn; } } },
  });
  let overflow = false;
  ui.document.querySelectorAll = selector => (selector.includes('.preview-line')
    ? [{ scrollHeight: overflow ? 130 : 40, clientHeight: 60, classList: { toggle: (cls, on) => { toggled.push(on); } } }] : []);
  const toggled = [];
  ui.render({ agents: [{ ...agent('', 'IDLE', true, true), preview: { users: ['p'], agent: 'a' } }] });
  assert.deepEqual(toggled, [false]);
  overflow = true;                       // the web font arrives and the text now wraps onto more lines
  fontListeners.loadingdone();
  browser.listeners.resize();
  assert.deepEqual(toggled, [false, true, true]);
});
