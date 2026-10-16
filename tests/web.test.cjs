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
    children: [],
    style: { setProperty() {}, removeProperty() {} },
    appendChild(child) {
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(item => item !== child);
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    addEventListener(type, listener) { this.listeners[type] = listener; },
    setAttribute() {}, focus() {},
  }]));
  elements.get('sheet-panel').appendChild(elements.get('sheet-form'));
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
    if (url === '/api/message') return response({ ok: true, text: 'result' });
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
    assert.deepEqual(calls.filter(url => url !== '/api/message'), ['/api/preview']);
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
    if (url === '/api/message') return response({ ok: true, text: `full result ${revision}` });
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
  assert.match(ui.elements.get('sheet-refresh-label').textContent, /有新訊息/);
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
    assert.deepEqual(calls.filter(url => url !== '/api/message'), ['/api/preview']);
    // Opening also schedules transcript focus; no acknowledgement until two content frames.
    while (frames.length > 1) frames.shift()();
    frames.shift()();
    if (state === 'closed') ui.close();
    if (state === 'hidden') ui.visibility('hidden');
    frames.shift()();
    await settle();
    assert.equal(calls.filter(url => url === '/api/ack').length, state === 'open' ? 1 : 0);
  }
});

test('opening the sheet focuses the transcript, not the reply box, so no keyboard pops up', () => {
  const ui = dashboard();
  const focused = [];
  for (const id of ['sheet-text', 'sheet-preview']) ui.elements.get(id).focus = () => focused.push(id);
  const current = review('task', 1, 0);
  ui.render({ agents: [current] });
  ui.open(current);
  assert.deepEqual(focused, ['sheet-preview']);
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<div class="sheet-preview" id="sheet-preview" tabindex="0"/);
  assert.doesNotMatch(html, /<textarea[^>]*\bautofocus\b/);
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
  assert.deepEqual(calls, ['/api/preview', '/api/preview', '/api/message', '/api/ack']);
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
  ui.context.blobToBase64 = async blob => `b64-${blob.tag || blob.name}`;
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

test('unsupported files are rejected and at most four attachments stay attached', async () => {
  const t = uploader();
  await t.pick([imageFile('notes.pdf', 'application/pdf'), imageFile('1.jpg'), imageFile('2.jpg'),
    imageFile('3.jpg'), imageFile('4.jpg'), imageFile('5.jpg')]);
  assert.equal(t.uploads().length, 4);
  assert.equal(t.chipKeys().length, 4);
  assert.match(t.ui.elements.get('sheet-status').textContent, /最多 4 個|只支援圖片/);
  await t.pick([imageFile('6.jpg')]);
  assert.equal(t.uploads().length, 4);
});

test('text and Office files upload unchanged with filenames and mix with images', async () => {
  const t = uploader();
  await t.pick([imageFile('photo.jpg'), imageFile('筆記.TXT', ''), imageFile('report.docx', 'application/zip'), imageFile('data.xlsx', '')]);
  assert.equal(t.uploads().length, 4);
  assert.deepEqual(t.uploads().filter(call => call.body.filename).map(call => call.body.filename), ['筆記.TXT', 'report.docx', 'data.xlsx']);
  assert.match(t.chips(), /筆記.TXT/);
  assert.match(t.chips(), /attachment-file/);
  assert.equal((t.chips().match(/<img /g) || []).length, 1);
  await t.submit();
  assert.deepEqual(t.sends()[0].body.images, ['id-photo.jpg']);
  assert.deepEqual(t.sends()[0].body.files, ['id-筆記.TXT', 'id-report.docx', 'id-data.xlsx']);
  assert.deepEqual(t.revoked, ['blob:photo.jpg']);
});

test('a document alone can be sent, unsupported formats stay out and filenames are escaped', async () => {
  const t = uploader();
  await t.pick([imageFile('bad.exe', 'text/plain'), imageFile('archive.zip', ''), imageFile('notes.pdf', ''), imageFile('<notes>.md', '')]);
  assert.equal(t.uploads().length, 1);
  assert.match(t.chips(), /&lt;notes&gt;.md/);
  assert.doesNotMatch(t.chips(), /<notes>/);
  await t.submit();
  assert.deepEqual(t.sends()[0].body.files, ['id-<notes>.md']);
  assert.equal(t.sends()[0].body.text, '');
});

test('all listed text and Office extensions accept empty browser MIME types', async () => {
  for (const ext of ['txt', 'md', 'csv', 'tsv', 'json', 'log', 'xml', 'yaml', 'yml', 'ini', 'cfg', 'toml', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx']) {
    const t = uploader();
    await t.pick([imageFile(`file.${ext}`, '')]);
    assert.equal(t.uploads().length, 1, ext);
    assert.equal(t.uploads()[0].body.filename, `file.${ext}`);
  }
});

test('oversized documents fail before reading or uploading and block sending', async () => {
  const t = uploader();
  await t.pick([{ name: 'huge.docx', type: '', size: 9 * 1024 * 1024 }]);
  assert.equal(t.uploads().length, 0);
  assert.match(t.chips(), /attachment error file/);
  await t.submit();
  assert.equal(t.sends().length, 0);
  t.remove(t.chipKeys()[0]);
  assert.deepEqual(t.revoked, []);
});

test('document uploads preserve wait, failure and closed-sheet guards', async () => {
  const t = uploader({ holdUploads: true });
  await t.pick([imageFile('notes.txt', '')]);
  await t.submit();
  assert.equal(t.sends().length, 0);
  assert.match(t.ui.elements.get('sheet-status').textContent, /附件上傳中/);
  t.ui.close();
  t.held.forEach(release => release());
  await flush();
  assert.equal(t.chipKeys().length, 0);
  assert.deepEqual(t.revoked, []);
  const failed = uploader({ failFor: ['bad.docx'] });
  await failed.pick([imageFile('bad.docx', '')]);
  await failed.submit();
  assert.equal(failed.sends().length, 0);
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

function readerSetup({ message, preview = PREVIEW, send, upload, globals = {}, startHash = '', clipboard, storage } = {}) {
  const calls = [];
  const browser = fakeBrowser();
  browser.state.hash = startHash;
  browser.state.stack = [startHash];
  const ui = dashboard({
    window: browser.window,
    globals: { ...globals, ...(storage ? { sessionStorage: storage } : {}) },
    navigator: clipboard ? { clipboard } : {},
    fetchRequest: async (url, options) => {
      const body = JSON.parse(options.body);
      calls.push({ url, body });
      if (url === '/api/preview') return jsonReply(typeof preview === 'function' ? preview(body) : preview);
      if (url === '/api/send' && send) return send(body);
      if (url === '/api/upload' && upload) return upload(body);
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
    target: { closest: selector => selector === 'a[href]' ? (extra.link ? {} : null) : selector === '.preview-line[data-role]' ? line(role, index) : null },
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
  assert.equal(t.messages().length, 1, 'only the background reply prefetch was sent');
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
  const t = readerSetup({ message: body => ++attempts <= 2
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

test('the reader contains the same composer and preserves the draft on back navigation', async () => {
  const t = readerSetup();
  let focused = 0;
  t.el('sheet-text').focus = () => { focused += 1; };
  await openSheetWithPreview(t);
  const form = t.el('sheet-form');
  t.el('sheet-text').value = 'draft reply';
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(form.parentNode, t.el('reader-compose'));
  assert.equal(t.el('sheet-text').value, 'draft reply');
  assert.equal(t.ui.elements.has('reader-reply'), false);
  assert.equal(focused, 0, 'opening a message does not bring up the keyboard');
  t.el('sheet-text').value += ' from the reader';
  t.browser.window.history.back();
  await t.settle();
  assert.equal(form.parentNode, t.el('sheet-panel'));
  assert.equal(t.el('sheet-text').value, 'draft reply from the reader');
});

test('sending directly from the reader returns to the dashboard route', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  const timers = [];
  t.ui.context.setTimeout = (fn, ms) => timers.push({ fn, ms });
  t.el('sheet-text').value = 'reply in place';
  await t.el('sheet-form').listeners.submit(submitEvent());
  await t.settle();
  assert.deepEqual(t.calls.find(call => call.url === '/api/send').body,
    { host: 'lab', pane: '%1', pid: '123', text: 'reply in place', submit: true, images: [] });
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(t.el('sheet-root').hidden, true);
  assert.equal(t.el('sheet-root').inert, false);
  assert.equal(t.browser.state.hash, '');
  assert.equal(t.browser.state.stack.length, 1);
  assert.equal(t.el('sheet-form').parentNode, t.el('sheet-panel'));
  assert.equal(t.el('sheet-text').value, '');
  t.ui.render({ agents: [t.current] });
  assert.match(t.el('list').innerHTML, /class="sent-tag">已送出/);
  assert.equal(timers.some(timer => timer.ms === 280), false);
  assert.equal(t.el('sheet-send').disabled, false);
});

test('reader uploads and sends mixed attachments using the shared composer', async () => {
  const revoked = [];
  let release;
  const t = readerSetup({ globals: { URL: { createObjectURL: file => `blob:${file.name}`, revokeObjectURL: url => revoked.push(url) } },
    upload: body => body.filename
      ? new Promise(resolve => { release = () => resolve(jsonReply({ ok: true, id: 'document.txt' })); })
      : jsonReply({ ok: true, id: 'image.png' }) });
  t.ui.context.prepareImage = async file => file;
  t.ui.context.blobToBase64 = async file => `b64-${file.name}`;
  await openSheetWithPreview(t);
  t.el('sheet-file').files = [imageFile('draft.txt', ''), imageFile('image.png')];
  t.el('sheet-file').listeners.change();
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(t.el('sheet-form').parentNode, t.el('reader-compose'));
  assert.match(t.el('sheet-attachments').innerHTML, /uploading/);
  await t.el('sheet-form').listeners.submit(submitEvent());
  assert.equal(t.calls.some(call => call.url === '/api/send'), false);
  release();
  await t.settle();
  assert.doesNotMatch(t.el('sheet-attachments').innerHTML, /uploading/);
  const chips = t.el('sheet-attachments').innerHTML;
  t.el('reader-back').listeners.click();
  await t.settle();
  assert.equal(t.el('sheet-attachments').innerHTML, chips);
  await t.tap('agent', 0);
  await t.settle();
  await t.el('sheet-form').listeners.submit(submitEvent());
  const sent = t.calls.find(call => call.url === '/api/send').body;
  assert.deepEqual(sent.images, ['image.png']);
  assert.deepEqual(sent.files, ['document.txt']);
  await t.settle();
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(t.el('sheet-root').hidden, true);
  assert.equal(t.el('sheet-attachments').hidden, true);
  assert.deepEqual(revoked, ['blob:image.png']);
});

test('a failed reader send preserves the draft and can be retried in place', async () => {
  let attempts = 0;
  const t = readerSetup({ send: () => ++attempts === 1
    ? jsonReply({ ok: false, error: 'pane not found' }, 409) : jsonReply({ ok: true }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  t.el('sheet-text').value = 'keep my reply';
  await t.el('sheet-form').listeners.submit(submitEvent());
  assert.equal(t.el('reader-root').hidden, false);
  assert.equal(t.el('sheet-form').parentNode, t.el('reader-compose'));
  assert.equal(t.el('sheet-status').textContent, 'pane not found');
  assert.equal(t.el('sheet-text').value, 'keep my reply');
  assert.equal(t.el('sheet-send').disabled, false);
  await t.el('sheet-form').listeners.submit(submitEvent());
  await t.settle();
  assert.equal(attempts, 2);
  assert.equal(t.el('reader-root').hidden, true);
  assert.equal(t.el('sheet-root').hidden, true);
});

test('typing the next draft during a reader send preserves the new text', async () => {
  let release;
  const t = readerSetup({ send: () => new Promise(resolve => { release = () => resolve(jsonReply({ ok: true })); }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  t.el('sheet-text').value = 'submitted reply';
  const pending = t.el('sheet-form').listeners.submit(submitEvent());
  t.el('sheet-text').value = 'my next draft';
  await t.el('sheet-form').listeners.submit(submitEvent());
  assert.equal(t.calls.filter(call => call.url === '/api/send').length, 1);
  release();
  await pending;
  assert.equal(t.el('sheet-text').value, 'my next draft');
  assert.equal(t.el('reader-root').hidden, false);
  assert.equal(t.el('sheet-send').disabled, false);
});

test('opening another message after a successful sheet send is not closed by a delayed callback', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  const timers = [];
  t.ui.context.setTimeout = (fn, ms) => timers.push({ fn, ms });
  t.el('sheet-text').value = 'reply';
  await t.el('sheet-form').listeners.submit(submitEvent());
  assert.equal(t.el('sheet-root').hidden, true);
  assert.equal(timers.some(timer => timer.ms === 280), false);
  t.ui.open(t.current);
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  timers.forEach(timer => timer.fn());
  assert.equal(t.el('reader-root').hidden, false);
  assert.equal(t.el('sheet-form').parentNode, t.el('reader-compose'));
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
  t.browser.window.location.hash = '#/m/lab/%251/123/agent/0';
  await t.settle();
  assert.equal(t.el('reader-title').textContent, 'Agent 回覆');
  assert.equal(t.el('sheet-text').value, '');
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

test('a pending reply prefetch shows the preview immediately and is shared with the reader', async () => {
  let release;
  const t = readerSetup({ message: () => new Promise(resolve => {
    release = () => resolve(jsonReply({ ok: true, text: 'complete reply', chars: 14 }));
  }) });
  await openSheetWithPreview(t);
  assert.equal(t.messages().length, 1);
  assert.equal(t.messages()[0].body.role, 'agent');
  assert.equal(t.el('reader-root').hidden, true);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /agent answer/);
  assert.match(t.el('reader-body').innerHTML, /載入全文中/);
  assert.equal(t.el('reader-copy').disabled, true);
  assert.equal(t.messages().length, 1, 'clicking must not duplicate the pending request');
  release();
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /complete reply/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /載入全文中/);
  assert.equal(t.el('reader-copy').disabled, false);
});

test('a completed reply prefetch displays synchronously and reopening reuses it', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  t.ui.context.openReaderFromSheet('agent', 0);
  t.ui.context.applyRoute();
  assert.match(t.el('reader-body').innerHTML, /FULL agent 0/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /載入/);
  t.browser.window.history.back();
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(t.messages().length, 1);
});

test('user messages prefill immediately but are fetched only on demand and then reused', async () => {
  let release;
  const t = readerSetup({ message: body => body.role === 'agent'
    ? jsonReply({ ok: true, text: 'agent full' })
    : new Promise(resolve => { release = () => resolve(jsonReply({ ok: true, text: 'user full' })); }) });
  await openSheetWithPreview(t);
  assert.equal(t.messages().length, 1);
  await t.tap('user', 1);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /second prompt/);
  assert.equal(t.messages().length, 2);
  release();
  await t.settle();
  t.browser.window.history.back();
  await t.settle();
  await t.tap('user', 1);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /user full/);
  assert.equal(t.messages().length, 2);
});

test('failed prefetches are silent and the reader fetches again when opened', async () => {
  let attempts = 0;
  const t = readerSetup({ message: () => ++attempts === 1
    ? Promise.reject(new Error('offline')) : jsonReply({ ok: true, text: 'recovered reply' }) });
  await openSheetWithPreview(t);
  assert.equal(t.el('sheet-status').textContent, '');
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /recovered reply/);
  assert.equal(attempts, 2);
});

test('partial prefetches are fetched again even when the preview revision stays the same', async () => {
  let attempts = 0;
  const t = readerSetup({ message: () => ++attempts === 1
    ? jsonReply({ ok: true, text: 'partial…', partial: true }) : jsonReply({ ok: true, text: 'now complete' }) });
  await openSheetWithPreview(t);
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /now complete/);
  assert.equal(attempts, 2);
});

test('refresh and reopen invalidate reply cache even if revision and clipped prefix stay the same', async () => {
  let attempts = 0;
  const t = readerSetup({ message: () => jsonReply({ ok: true, text: `reply ${++attempts}` }) });
  await openSheetWithPreview(t);
  t.ui.refreshPreview();
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /reply 2/);
  assert.equal(attempts, 2);
  t.ui.close();
  await t.settle();
  t.ui.open(t.current);
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /reply 3/);
});

test('newer fleet versions invalidate prefetched replies without replacing the frozen preview', async () => {
  let attempts = 0;
  const t = readerSetup({ message: () => ++attempts === 1 ? jsonReply({ ok: true, text: 'old full' })
    : jsonReply({ ok: false, error: 'content changed; refresh preview' }, 409) });
  t.current.revision = 3;
  await openSheetWithPreview(t);
  t.ui.render({ agents: [{ ...t.current, revision: 4 }] });
  assert.match(t.el('sheet-preview').innerHTML, /agent answer/);
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(attempts, 2);
  assert.match(t.el('reader-body').innerHTML, /已有新內容/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /old full/);
});

test('late prefetches from another pane cannot populate the active reader', async () => {
  let release;
  const t = readerSetup({ message: body => body.pane === '%1'
    ? new Promise(resolve => { release = () => resolve(jsonReply({ ok: true, text: 'WRONG PANE' })); })
    : jsonReply({ ok: true, text: 'correct pane' }) });
  await openSheetWithPreview(t);
  const other = { ...t.current, pane: '%2' };
  t.ui.render({ agents: [t.current, other] });
  t.ui.open(other);
  await t.settle();
  await t.tap('agent', 0);
  await t.settle();
  release();
  await t.settle();
  assert.match(t.el('reader-body').innerHTML, /correct pane/);
  assert.doesNotMatch(t.el('reader-body').innerHTML, /WRONG PANE/);
});

test('hidden sheets do not speculatively fetch full replies', async () => {
  const t = readerSetup();
  t.ui.visibility('hidden');
  await openSheetWithPreview(t);
  assert.equal(t.messages().length, 0);
});

function draftStorage() {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key) };
}

function typeDraft(t, text) {
  t.el('sheet-text').value = text;
  t.el('sheet-text').listeners.input();
}

test('text drafts survive sheet close and remain isolated by host, pane and PID', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  typeDraft(t, 'first session draft');
  t.ui.close();
  await t.settle();
  t.ui.open(t.current);
  assert.equal(t.el('sheet-text').value, 'first session draft');
  for (const other of [{ ...t.current, host: 'other' }, { ...t.current, pane: '%2' }, { ...t.current, pid: '999' }]) {
    t.ui.render({ agents: [t.current, other] });
    t.ui.open(other);
    assert.equal(t.el('sheet-text').value, '');
    typeDraft(t, `draft ${other.host} ${other.pane} ${other.pid}`);
    t.ui.open(t.current);
    assert.equal(t.el('sheet-text').value, 'first session draft');
    t.ui.open(other);
    assert.equal(t.el('sheet-text').value, `draft ${other.host} ${other.pane} ${other.pid}`);
    t.ui.open(t.current);
  }
});

test('reader text drafts survive a page reload and explicit clearing removes them', async () => {
  const localStorage = draftStorage();
  const first = readerSetup({ globals: { localStorage } });
  await openSheetWithPreview(first);
  await first.tap('agent', 0);
  await first.settle();
  typeDraft(first, 'unfinished reader reply\n第二行');
  const reloaded = readerSetup({ globals: { localStorage } });
  await openSheetWithPreview(reloaded);
  assert.equal(reloaded.el('sheet-text').value, 'unfinished reader reply\n第二行');
  typeDraft(reloaded, '');
  const cleared = readerSetup({ globals: { localStorage } });
  await openSheetWithPreview(cleared);
  assert.equal(cleared.el('sheet-text').value, '');
});

test('failed sends retain saved drafts and successful sends remove them across reloads', async () => {
  const localStorage = draftStorage();
  const first = readerSetup({ globals: { localStorage }, send: () => jsonReply({ ok: false, error: 'offline' }, 502) });
  await openSheetWithPreview(first);
  typeDraft(first, 'please keep this');
  await first.el('sheet-form').listeners.submit(submitEvent());
  first.ui.close();
  const reloaded = readerSetup({ globals: { localStorage } });
  await openSheetWithPreview(reloaded);
  assert.equal(reloaded.el('sheet-text').value, 'please keep this');
  await reloaded.el('sheet-form').listeners.submit(submitEvent());
  const sent = readerSetup({ globals: { localStorage } });
  await openSheetWithPreview(sent);
  assert.equal(sent.el('sheet-text').value, '');
});

test('a late send clears only its submitted draft after closing or switching sessions', async () => {
  for (const editWhilePending of [false, true]) {
    let release;
    const localStorage = draftStorage();
    const t = readerSetup({ globals: { localStorage }, send: () => new Promise(resolve => {
      release = () => resolve(jsonReply({ ok: true }));
    }) });
    await openSheetWithPreview(t);
    typeDraft(t, 'submitted text');
    const sending = t.el('sheet-form').listeners.submit(submitEvent());
    t.ui.close();
    await t.settle();
    if (editWhilePending) {
      t.ui.open(t.current);
      typeDraft(t, 'new unsent draft');
      t.ui.close();
    }
    const other = { ...t.current, pane: '%2' };
    t.ui.render({ agents: [t.current, other] });
    t.ui.open(other);
    typeDraft(t, 'other session draft');
    release();
    await sending;
    assert.equal(t.el('sheet-text').value, 'other session draft');
    t.ui.open(t.current);
    assert.equal(t.el('sheet-text').value, editWhilePending ? 'new unsent draft' : '');
  }
});

test('new text typed during a send is saved as the next draft', async () => {
  let release;
  const localStorage = draftStorage();
  const t = readerSetup({ globals: { localStorage }, send: () => new Promise(resolve => {
    release = () => resolve(jsonReply({ ok: true }));
  }) });
  await openSheetWithPreview(t);
  typeDraft(t, 'submitted text');
  const sending = t.el('sheet-form').listeners.submit(submitEvent());
  typeDraft(t, 'next reply');
  release();
  await sending;
  const reloaded = readerSetup({ globals: { localStorage } });
  await openSheetWithPreview(reloaded);
  assert.equal(reloaded.el('sheet-text').value, 'next reply');
});

test('drafts remain available when storage writes fail but reads still work', async () => {
  const t = readerSetup({ globals: { localStorage: {
    getItem: () => 'older stored draft', setItem: () => { throw new Error('quota'); }, removeItem: () => { throw new Error('quota'); },
  } } });
  await openSheetWithPreview(t);
  typeDraft(t, 'newer memory draft');
  t.ui.close();
  t.ui.open(t.current);
  assert.equal(t.el('sheet-text').value, 'newer memory draft');
  typeDraft(t, '');
  t.ui.close();
  t.ui.open(t.current);
  assert.equal(t.el('sheet-text').value, '');
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

test('a card parked on background work is still a running card, marked with its own icon', () => {
  const parked = cardHtmlFor({ state: 'RUNNING', pending: false, background_wait: true, reason: '等待背景工作（1 shell）· 可先看進度' });
  assert.match(parked, /class="card running/);
  assert.match(parked, /<span class="emoji"[^>]*>⏳<\/span>/);
  assert.match(parked, /class="reason"[^>]*>等待背景工作（1 shell）/);
  for (const overrides of [{ reason: '正在工作' }, { background_wait: false }, {}]) {
    const working = cardHtmlFor({ state: 'RUNNING', pending: false, ...overrides });
    assert.match(working, /<span class="emoji"[^>]*>🏃<\/span>/);
    assert.doesNotMatch(working, /⏳/);
  }
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

function killSetup({ kill, names = ['demo'] } = {}) {
  const calls = [];
  const clock = { now: 1_000_000 };
  const timers = [];
  const replaced = [];
  const refocused = [];
  const FakeDate = class extends Date { static now() { return clock.now; } };
  const doc = {
    // A card the page can swap in place: it records the new markup instead of touching a real DOM.
    querySelector(selector) {
      const key = (selector.match(/data-key="([^"]*)"/) || [])[1];
      if (key === undefined) return null;
      return {
        classList: { contains: () => false, add() {}, remove() {} },
        contains: node => node === ui.document.activeElement,
        set outerHTML(html) { replaced.push([key, html]); },
        querySelector: inner => ({ focus: () => refocused.push([key, inner]) }),
      };
    },
  };
  const ui = dashboard({
    document: doc,
    globals: { Date: FakeDate, setTimeout: (fn, ms) => { timers.push({ fn, at: clock.now + ms }); } },
    fetchRequest: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      if (url === '/api/kill') return kill ? kill() : jsonReply({ ok: true });
      if (url === '/api/preview') return jsonReply(PREVIEW);
      if (url === '/api/send' && send) return send(body);
      if (url === '/api/upload' && upload) return upload(body);
      return jsonReply({ ok: true });
    },
  });
  const agents = names.map((name, i) => ({ ...agent('lab', 'WAIT'), project: name, pane: `%${i + 1}`, pid: String(100 + i) }));
  ui.render({ agents });
  const keyOf = i => ui.context.agentKey(agents[i]);
  const tap = (i = 0, onKill = true) => ui.elements.get('list').listeners.click({
    target: { closest: selector => (selector === '[data-kill]' ? (onKill ? {} : null) : { dataset: { key: keyOf(i) } }) },
  });
  const list = () => { ui.render({ agents }); return ui.elements.get('list').innerHTML; };
  const advance = ms => {
    clock.now += ms;
    const due = timers.filter(timer => timer.at <= clock.now);
    for (const timer of due) timers.splice(timers.indexOf(timer), 1);
    for (const timer of due) timer.fn();
  };
  const killCalls = () => calls.filter(call => call.url === '/api/kill');
  const live = () => ui.elements.get('kill-live').textContent;
  const lastHtml = i => (replaced.filter(([key]) => key === keyOf(i)).pop() || [])[1] || '';
  const focusTrash = () => { ui.document.activeElement = { closest: selector => (selector === '[data-kill]' ? {} : null) }; };
  return { ui, calls, replaced, refocused, agents, tap, list, advance, killCalls, live, lastHtml, focusTrash };
}

test('tracked cards carry a trash button beside the age; the card itself is not a button', () => {
  const ui = dashboard();
  ui.render({ agents: [{ ...agent('lab', 'WAIT'), project: 'demo' },
    { ...agent('lab', 'SHELL', false), pane: '%9', pid: '9', project: 'plain-shell' }] });
  const list = ui.elements.get('list').innerHTML;
  assert.equal((list.match(/class="card-kill"/g) || []).length, 1);
  assert.match(list, /class="row1-side">\s*<div class="age">[^<]*<\/div>\s*<button type="button" class="card-kill" data-kill aria-label="關閉 demo"/);
  assert.doesNotMatch(list, /card-kill-label/);
  // Nested interactive controls hide the trash from screen readers, so the open action is its own button.
  assert.doesNotMatch(list, /role="button"/);
  assert.equal((list.match(/class="card-open" aria-label="開啟 demo"/g) || []).length, 1);
});

test('the first tap arms the trash on that card only, announces it and never opens the sheet', async () => {
  const t = killSetup();
  await t.tap();
  await settle();
  assert.deepEqual(t.calls, []);
  assert.equal(t.replaced.length, 1);
  assert.match(t.lastHtml(0), /class="card-kill armed" data-kill aria-label="再按一次確認關閉 demo"/);
  assert.match(t.lastHtml(0), /class="card-kill-label">確認關閉/);
  assert.match(t.lastHtml(0), /class="card [^"]*\bkill-armed\b/);
  assert.equal(t.live(), '再按一次確認關閉 demo');
  assert.match(t.list(), /class="card-kill armed"/, 'a poll render keeps the arm');
});

test('arming one card disarms another, and a tap on the second card never closes it', async () => {
  const t = killSetup({ names: ['alpha', 'beta'] });
  await t.tap(0);
  await t.tap(1);
  await settle();
  assert.equal(t.killCalls().length, 0);
  assert.doesNotMatch(t.lastHtml(0), /armed/);
  assert.match(t.lastHtml(1), /card-kill armed/);
  await t.tap(1);
  await settle();
  assert.deepEqual(t.killCalls().map(call => call.body), [{ host: 'lab', pane: '%2', pid: '101' }]);
});

test('a second tap within five seconds closes the pane on its own host', async () => {
  const t = killSetup();
  await t.tap();
  await t.tap();
  await settle();
  assert.deepEqual(t.killCalls().map(call => call.body), [{ host: 'lab', pane: '%1', pid: '100' }]);
  assert.equal(t.calls.some(call => call.url === '/api/preview'), false);
});

test('the arm expires on a timer, and an older timer never cuts a newer arm short', async () => {
  const t = killSetup({ names: ['alpha', 'beta'] });
  await t.tap(0);
  t.advance(5000);
  assert.doesNotMatch(t.lastHtml(0), /armed/, 'the pill reverts by itself');
  assert.equal(t.killCalls().length, 0);
  await t.tap(0);
  t.advance(4000);
  await t.tap(1); // arms beta; alpha's timer is still pending
  t.advance(1000); // alpha's timer fires now
  assert.match(t.list(), /data-key="lab\|%2\|101"[^>]*>[\s\S]*?card-kill armed/, 'beta stays armed');
  t.advance(4100);
  assert.doesNotMatch(t.list(), /armed/);
});

test('a confirmed close stays on "closing" until a render no longer lists the pane', async () => {
  const t = killSetup();
  await t.tap();
  await t.tap();
  await settle();
  assert.match(t.lastHtml(0), /class="card-kill busy" data-kill[^>]*aria-disabled="true"/);
  assert.doesNotMatch(t.lastHtml(0), /\bdisabled\b(?!=)/);
  assert.match(t.list(), /card-kill busy/, 'a stale poll that still lists the pane keeps it busy');
  t.ui.render({ agents: [] });
  assert.doesNotMatch(t.list(), /busy/);
});

test('taps during an in-flight close are ignored', async () => {
  let release;
  const t = killSetup({ names: ['alpha', 'beta'], kill: () => new Promise(resolve => { release = () => resolve(jsonReply({ ok: true })); }) });
  await t.tap(0);
  const inFlight = t.tap(0); // the confirming tap waits on the request
  await settle();
  await t.tap(0);
  await t.tap(1);
  assert.equal(t.killCalls().length, 1);
  release();
  await inFlight;
});

test('a failed close shows and announces the reason, disarms, and the note clears itself', async () => {
  const t = killSetup({ kill: () => jsonReply({ ok: false, error: 'boom <b>' }, 409) });
  await t.tap();
  await t.tap();
  await settle();
  assert.match(t.lastHtml(0), /class="kill-note">boom &lt;b&gt;/);
  assert.doesNotMatch(t.lastHtml(0), /armed|busy/);
  assert.equal(t.live(), '關閉失敗：boom <b>');
  t.advance(6000);
  assert.doesNotMatch(t.lastHtml(0), /kill-note/);
  assert.doesNotMatch(t.list(), /kill-note/);
});

test('keyboard focus follows the trash when its markup is swapped, and only then', async () => {
  const t = killSetup();
  await t.tap();
  assert.deepEqual(t.refocused, []);
  t.focusTrash();
  await t.tap(); // confirm: the button re-renders as busy
  await settle();
  assert.ok(t.refocused.length >= 1);
  assert.ok(t.refocused.every(([key, selector]) => key === 'lab|%1|100' && selector === '[data-kill]'));
});

test('tapping the card body still opens the sheet', async () => {
  const t = killSetup();
  await t.tap(0, false);
  await settle();
  assert.equal(t.calls.some(call => call.url === '/api/preview'), true);
  assert.equal(t.killCalls().length, 0);
});

test('the reply sheet has no trash button and keeps attach and send inside one composer box', () => {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /id="sheet-kill"/);
  const form = html.slice(html.indexOf('<form class="sheet-compose"'), html.indexOf('</form>', html.indexOf('<form class="sheet-compose"')));
  const box = form.slice(form.indexOf('<div class="composer"'), form.indexOf('id="sheet-status"'));
  for (const id of ['sheet-attachments', 'sheet-text', 'sheet-attach', 'sheet-send']) {
    assert.ok(box.includes(`id="${id}"`), `${id} belongs inside .composer`);
  }
  assert.ok(box.indexOf('id="sheet-attachments"') < box.indexOf('id="sheet-text"'), 'thumbnails sit above the text');
  assert.ok(box.indexOf('id="sheet-text"') < box.indexOf('id="sheet-attach"'), 'the toolbar sits below the text');
});

const HUB = '5d18590aaaaa';
const hostRows = (...extra) => [{ name: 'hub', ok: true, source: 'local', version: HUB }, ...extra];

test('the update hint names peers on another version and offers the update command', () => {
  const ui = dashboard();
  ui.render({ version: HUB, agents: [], hosts: hostRows(
    { name: 'lab', ok: true, source: 'peer', url: 'http://lab', version: '8e7d0fcbbbbb' },
    { name: 'down', ok: false, source: 'peer', url: 'http://down' },
    { name: 'same', ok: true, source: 'peer', url: 'http://same', version: HUB },
  ) });
  const hint = ui.elements.get('update-hint');
  assert.equal(hint.hidden, false);
  assert.match(hint.innerHTML, /<strong>lab<\/strong>/);
  assert.match(hint.innerHTML, /8e7d0fc → hub 5d18590/);
  assert.match(hint.innerHTML, /<code>tmux-attn update --ref 5d18590aaaaa<\/code>/);
  assert.doesNotMatch(hint.innerHTML, /down|same/, 'offline hosts and hosts already on the hub version stay out');
});

test('a peer that reports no version gets the bootstrap command, since it predates `tmux-attn update`', () => {
  const ui = dashboard();
  ui.render({ version: HUB, agents: [], hosts: hostRows({ name: 'old', ok: true, source: 'peer', url: 'http://old' }) });
  const html = ui.elements.get('update-hint').innerHTML;
  assert.match(html, /未回報版本 → hub 5d18590/);
  assert.match(html, /update\.sh \| TMUX_ATTN_REF=5d18590aaaaa sh/);
  assert.doesNotMatch(html, /tmux-attn update --ref/);
});

test('the update hint stays hidden when versions agree, the hub is unversioned, or only the hub is listed', () => {
  for (const [version, hosts] of [
    [HUB, hostRows({ name: 'lab', ok: true, source: 'peer', version: HUB })],
    ['', hostRows({ name: 'lab', ok: true, source: 'peer', version: 'ffffffffffff' })],
    [HUB, hostRows()],
  ]) {
    const ui = dashboard();
    ui.render({ version, agents: [], hosts });
    assert.equal(ui.elements.get('update-hint').hidden, true);
    assert.equal(ui.elements.get('update-hint').innerHTML, '');
  }
});

test('host names in the update hint are escaped', () => {
  const ui = dashboard();
  ui.render({ version: HUB, agents: [], hosts: hostRows({ name: '<img src=x onerror=1>', ok: true, source: 'peer', version: 'ffffffffffff' }) });
  assert.doesNotMatch(ui.elements.get('update-hint').innerHTML, /<img/);
});

test('the copy button puts the command on the clipboard, falling back when there is no clipboard API', async () => {
  const written = [];
  const ui = dashboard({ navigator: { clipboard: { writeText: async text => { written.push(text); } } } });
  const button = { dataset: { copy: 'tmux-attn update' }, textContent: '複製' };
  const click = target => ui.elements.get('update-hint').listeners.click({ target: { closest: s => (s === '[data-copy]' ? target : null) } });
  await click(button);
  assert.deepEqual(written, ['tmux-attn update']);
  assert.equal(button.textContent, '已複製');
  await click(null);
  assert.equal(written.length, 1, 'a click elsewhere in the hint does nothing');
  const plain = { dataset: { copy: 'x' }, textContent: '複製' };
  await dashboard().elements.get('update-hint').listeners.click({ target: { closest: s => (s === '[data-copy]' ? plain : null) } });
  assert.equal(plain.textContent, '無法複製');
});

test('identical update-hint markup is not rewritten on every poll, so a selection or the copied label survives', () => {
  const ui = dashboard();
  const payload = { version: HUB, agents: [], hosts: hostRows({ name: 'lab', ok: true, source: 'peer', version: 'ffffffffffff' }) };
  ui.render(payload);
  const hint = ui.elements.get('update-hint');
  hint.innerHTML = 'user-visible state';
  ui.render(payload);
  assert.equal(hint.innerHTML, 'user-visible state');
  ui.render({ ...payload, hosts: hostRows({ name: 'lab', ok: true, source: 'peer', version: HUB }) });
  assert.equal(hint.innerHTML, '');
});

test('a hub version that is not a commit hash never produces a command', () => {
  const ui = dashboard();
  ui.render({ version: 'abc; rm -rf ~', agents: [], hosts: hostRows({ name: 'lab', ok: true, source: 'peer', version: 'ffffffffffff' }) });
  assert.equal(ui.elements.get('update-hint').hidden, true);
});


// ---- sheet redesign

function sheetAgent(extra = {}) {
  return { ...agent('lab', 'IDLE'), project: 'demo', cli: 'claude', branch: 'main', dirty: true, path: '/work/demo',
    age: '2m', reason: '已完成，尚未查看', ...extra };
}

test('the sheet header carries the CLI badge and keeps branch, dirty flag and age in one meta row', () => {
  const ui = dashboard();
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  assert.match(ui.elements.get('sheet-cli').innerHTML, /class="cli claude"/);
  const env = ui.elements.get('sheet-env');
  assert.equal(env.hidden, false);
  assert.match(env.innerHTML, /class="branch">main</);
  assert.match(env.innerHTML, /class="dirty">dirty</);
  assert.match(env.innerHTML, />2m</);
  assert.doesNotMatch(env.innerHTML, /\/work\/demo/, 'the path stays out of the way until asked for');
  assert.equal(ui.elements.get('sheet-path').textContent, '/work/demo');
  assert.equal(ui.elements.get('sheet-path').hidden, true);
});

test('the path toggle shows and hides the path, and a new session starts collapsed', () => {
  const ui = dashboard();
  const a = sheetAgent();
  const b = sheetAgent({ pane: '%2', pid: '2', project: 'other', path: '/work/other' });
  ui.render({ agents: [a, b] });
  ui.open(a);
  const attrs = {};
  const button = { setAttribute: (k, v) => { attrs[k] = v; } };
  const tap = () => ui.elements.get('sheet-env').listeners.click({ target: { closest: s => (s === '[data-toggle-path]' ? button : null) } });
  tap();
  assert.equal(ui.elements.get('sheet-path').hidden, false);
  assert.equal(attrs['aria-expanded'], 'true');
  tap();
  assert.equal(ui.elements.get('sheet-path').hidden, true);
  tap();
  ui.close();
  ui.open(b);
  assert.equal(ui.elements.get('sheet-path').hidden, true);
  assert.equal(ui.elements.get('sheet-path').textContent, '/work/other');
});

const FULL = { ok: true, revision: 3, viewable: false,
  preview: { users: ['first ask', 'second ask', 'latest ask'], agent: 'the result' } };

async function sheetWith(preview = FULL) {
  const ui = dashboard(async url => response(url === '/api/preview' ? preview : { ok: true }));
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  await settle();
  return { ui, html: () => ui.elements.get('sheet-preview').innerHTML };
}

test('older prompts fold into one line, the latest prompt and the result stay in order, indices are kept', async () => {
  const { html } = await sheetWith();
  const h = html();
  assert.match(h, /data-earlier[^>]*aria-expanded="false">較早的 2 則訊息</);
  assert.match(h, /class="earlier" hidden>[\s\S]*data-index="0"[\s\S]*data-index="1"[\s\S]*<\/div>/);
  assert.ok(h.indexOf('data-index="1"') < h.indexOf('latest ask'), 'earlier ones come first');
  assert.ok(h.indexOf('latest ask') < h.indexOf('the result'), 'the result stays after the prompt it answers');
  assert.match(h, /data-role="user" data-index="2"/);
  assert.match(h, /data-role="agent" data-index="0"/);
  assert.doesNotMatch(h.slice(0, h.indexOf('class="earlier"')), /first ask/);
});

test('a single prompt has no fold; expanding the fold works in place, survives a refresh, and resets for the next session', async () => {
  const one = await sheetWith({ ok: true, revision: 1, viewable: false, preview: { users: ['only'], agent: 'r' } });
  assert.doesNotMatch(one.html(), /earlier/);
  const { ui, html } = await sheetWith();
  const attrs = {};
  const panel = { hidden: true };
  const toggle = { setAttribute: (k, v) => { attrs[k] = v; }, nextElementSibling: panel };
  const tapFold = () => ui.elements.get('sheet-preview').listeners.click({
    target: { closest: s => (s === '[data-earlier]' ? toggle : null) } });
  const before = html();
  await tapFold();
  assert.equal(html(), before, 'no re-render: replacing the button would drop keyboard focus');
  assert.equal(attrs['aria-expanded'], 'true');
  assert.equal(panel.hidden, false);
  ui.refreshPreview();
  await settle();
  assert.doesNotMatch(html(), /class="earlier" hidden/, 'a refresh must not re-collapse what the user opened');
  await tapFold();
  assert.equal(panel.hidden, true);
  await tapFold();
  ui.close();
  const other = sheetAgent({ pane: '%2', pid: '2', project: 'other' });
  ui.render({ agents: [sheetAgent(), other] });
  ui.open(other);
  await settle();
  assert.match(html(), /class="earlier" hidden/, 'a different session starts folded');
});

test('the refresh control is an icon whose label only shows when something newer exists', () => {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<button[^>]*id="sheet-refresh"[^>]*>[\s\S]*?<span class="refresh-label sr-only" id="sheet-refresh-label">重新整理內容<\/span>/);
  assert.doesNotMatch(html, /class="host-pill" id="sheet-refresh"/);
});

test('the loading bar shows while the full preview loads and is gone afterwards, even on failure', async () => {
  for (const fail of [false, true]) {
    const ui = dashboard(async url => { if (fail && url === '/api/preview') throw new Error('offline'); return response(url === '/api/preview' ? FULL : { ok: true }); });
    const a = sheetAgent();
    ui.render({ agents: [a] });
    ui.open(a);
    assert.equal(ui.elements.get('sheet-progress').hidden, false);
    await settle();
    assert.equal(ui.elements.get('sheet-progress').hidden, true);
  }
});

test('while the sheet is open the page behind it is inert, and it is released on close', () => {
  const wrap = { inert: false };
  const ui = dashboard({ document: { querySelector: s => (s === '.wrap' ? wrap : null) } });
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  assert.equal(wrap.inert, true);
  ui.close();
  assert.equal(wrap.inert, false);
});

test('the keyboard shrinks sheet and reader roots to the visible area and compacts the header', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(dashboard().context.viewportMetrics({ height: 430, offsetTop: 0 }, 844))),
    { height: 430, top: 0, keyboard: true });
  assert.equal(dashboard().context.viewportMetrics({ height: 820, offsetTop: 0 }, 844).keyboard, false, 'browser chrome is not a keyboard');
  const calls = {};
  const classes = {};
  const readerCalls = {};
  const listeners = {};
  const ui = dashboard({ window: { visualViewport: { height: 844, offsetTop: 0, addEventListener: (t, fn) => { listeners[t] = fn; } }, innerHeight: 844 } });
  const root = ui.elements.get('sheet-root');
  root.style = { setProperty: (k, v) => { calls[k] = v; } };
  ui.elements.get('reader-root').style = { setProperty: (k, v) => { readerCalls[k] = v; } };
  root.classList = { add() {}, remove() {}, toggle: (c, on) => { classes[c] = on; } };
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  ui.context.window.visualViewport.height = 430;
  listeners.resize();
  assert.equal(calls['--vv-h'], '430px');
  assert.equal(readerCalls['--vv-h'], '430px');
  assert.equal(readerCalls['--vv-top'], '0px');
  assert.equal(classes['kb-open'], true);
  ui.context.window.visualViewport.height = 844;
  listeners.resize();
  assert.equal(classes['kb-open'], false);
});

test('dragging the header down dismisses the sheet; a short drag springs back', () => {
  const { dragOutcome } = dashboard().context;
  assert.equal(dragOutcome(120, 600), 'dismiss');
  assert.equal(dragOutcome(60, 80), 'dismiss', 'a quick flick');
  assert.equal(dragOutcome(60, 900), 'back');
  assert.equal(dragOutcome(-50, 100), 'back');
  const ui = dashboard();
  const style = {};
  const panel = ui.elements.get('sheet-panel');
  panel.style = style;
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  const drag = ui.elements.get('sheet-drag');
  const at = (y, extra = {}) => ({ clientY: y, pointerId: 1, target: { closest: () => null }, currentTarget: { setPointerCapture() {} }, ...extra });
  drag.listeners.pointerdown(at(100));
  ui.setNow(100); drag.listeners.pointermove(at(150));
  assert.equal(style.transform, 'translateY(50px)');
  ui.setNow(900); drag.listeners.pointerup(at(150));
  assert.equal(ui.elements.get('sheet-root').hidden, false);
  assert.equal(style.transform, '');
  drag.listeners.pointerdown(at(100));
  ui.setNow(1000); drag.listeners.pointermove(at(260));
  ui.setNow(1100); drag.listeners.pointerup(at(260));
  assert.equal(ui.elements.get('sheet-root').hidden, true);
});

test('a drag that starts on a button is left to the button', () => {
  const ui = dashboard();
  ui.elements.get('sheet-panel').style = {};
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  const drag = ui.elements.get('sheet-drag');
  drag.listeners.pointerdown({ clientY: 0, pointerId: 1, target: { closest: s => (s === 'button' ? {} : null) }, currentTarget: { setPointerCapture() {} } });
  drag.listeners.pointermove({ clientY: 300, pointerId: 1 });
  drag.listeners.pointerup({ clientY: 300, pointerId: 1 });
  assert.equal(ui.elements.get('sheet-root').hidden, false);
});

test('after a send the card says so until the agent shows as running or eight seconds pass', async () => {
  const clock = { now: 5_000_000 };
  const FakeDate = class extends Date { static now() { return clock.now; } };
  const ui = dashboard({ globals: { Date: FakeDate }, fetchRequest: async url => response(url === '/api/preview' ? FULL : { ok: true }) });
  const a = sheetAgent({ state: 'WAIT', pending: true, category: 'wait' });
  ui.render({ agents: [a] });
  ui.open(a);
  await settle();
  ui.elements.get('sheet-text').value = 'go ahead';
  await ui.elements.get('sheet-form').listeners.submit({ preventDefault() {} });
  const list = () => { ui.render({ agents: [a] }); return ui.elements.get('list').innerHTML; };
  assert.match(list(), /class="sent-tag">已送出/);
  assert.doesNotMatch((ui.render({ agents: [{ ...a, state: 'RUNNING', category: 'running', pending: false }] }), ui.elements.get('list').innerHTML), /sent-tag/);
  clock.now += 9000;
  assert.doesNotMatch(list(), /sent-tag/);
});


// ---- review hardening

function stubSheetChrome(ui) {
  const log = { props: {}, removed: [], classes: {}, transform: [] };
  const root = ui.elements.get('sheet-root');
  root.style = { setProperty: (k, v) => { log.props[k] = v; }, removeProperty: k => { log.removed.push(k); } };
  root.classList = { add: c => { log.classes[c] = true; }, remove: c => { log.classes[c] = false; }, toggle: (c, on) => { log.classes[c] = on; } };
  const panel = ui.elements.get('sheet-panel');
  panel.style = { set transform(v) { log.transform.push(v); }, get transform() { return log.transform.at(-1); } };
  return log;
}

test('closing the sheet clears everything it set: viewport vars, keyboard class, drag offset, loading bar, inert', () => {
  const wrap = { inert: false };
  const vv = { height: 430, offsetTop: 12, scale: 1, addEventListener() {} };
  const ui = dashboard({ window: { visualViewport: vv, innerHeight: 844 }, document: { querySelector: s => (s === '.wrap' ? wrap : null) } });
  const log = stubSheetChrome(ui);
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  assert.deepEqual([log.props['--vv-h'], log.props['--vv-top'], log.classes['kb-open']], ['430px', '12px', true],
    'the viewport is applied on open, without waiting for an event');
  ui.close();
  assert.deepEqual(log.removed.sort(), ['--vv-h', '--vv-top']);
  assert.equal(log.classes['kb-open'], false);
  assert.equal(log.transform.at(-1), '');
  assert.equal(ui.elements.get('sheet-progress').hidden, true);
  assert.equal(wrap.inert, false);
});

test('visualViewport scroll as well as resize re-applies the viewport, and the top offset is used', () => {
  const listeners = {};
  const vv = { height: 500, offsetTop: 0, scale: 1, addEventListener: (t, fn) => { listeners[t] = fn; } };
  const ui = dashboard({ window: { visualViewport: vv, innerHeight: 500 } });
  const log = stubSheetChrome(ui);
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  vv.offsetTop = 40;
  listeners.scroll();
  assert.equal(log.props['--vv-top'], '40px');
  assert.equal(typeof listeners.resize, 'function');
});

test('a pinch-zoomed page is not mistaken for a keyboard', () => {
  const { viewportMetrics } = dashboard().context;
  assert.equal(viewportMetrics({ height: 430, offsetTop: 0, scale: 1 }, 844).keyboard, true);
  assert.equal(viewportMetrics({ height: 430, offsetTop: 0, scale: 2 }, 844).keyboard, false);
});

test('drag handling ignores the mouse, a second finger and anything after the pointer is lost', () => {
  const ui = dashboard();
  const log = stubSheetChrome(ui);
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  const drag = ui.elements.get('sheet-drag');
  const ev = (y, extra = {}) => ({ clientY: y, pointerId: 1, pointerType: 'touch', isPrimary: true,
    target: { closest: () => null }, currentTarget: { setPointerCapture() {} }, ...extra });
  drag.listeners.pointerdown(ev(0, { pointerType: 'mouse' }));
  drag.listeners.pointermove(ev(300, { pointerType: 'mouse' }));
  drag.listeners.pointerup(ev(300, { pointerType: 'mouse' }));
  assert.equal(ui.elements.get('sheet-root').hidden, false, 'a mouse selection must not dismiss');
  drag.listeners.pointerdown(ev(0, { isPrimary: false }));
  drag.listeners.pointerup(ev(300));
  assert.equal(ui.elements.get('sheet-root').hidden, false);
  drag.listeners.pointerdown(ev(0));
  drag.listeners.pointermove(ev(50, { pointerId: 2 }));
  assert.equal(log.transform.includes('translateY(50px)'), false, 'another pointer cannot move the sheet');
  drag.listeners.pointerup(ev(300, { pointerId: 2 }));
  assert.equal(ui.elements.get('sheet-root').hidden, false, 'another pointer cannot move or finish this drag');
  drag.listeners.lostpointercapture();
  drag.listeners.pointermove(ev(300));
  assert.equal(log.transform.at(-1), '', 'after the capture is lost nothing follows the pointer');
  drag.listeners.pointerdown(ev(0));
  drag.listeners.pointercancel();
  drag.listeners.pointerup(ev(300));
  assert.equal(ui.elements.get('sheet-root').hidden, false);
});

test('the pointer is captured so a drag keeps following the finger outside the header', () => {
  const ui = dashboard();
  stubSheetChrome(ui);
  const a = sheetAgent();
  ui.render({ agents: [a] });
  ui.open(a);
  let captured = 0;
  ui.elements.get('sheet-drag').listeners.pointerdown({ clientY: 0, pointerId: 1, pointerType: 'touch', isPrimary: true,
    target: { closest: () => null }, currentTarget: { setPointerCapture: () => { captured += 1; } } });
  assert.equal(captured, 1);
});

test('the reader makes the sheet beneath it inert and gives it back when it closes', async () => {
  const t = readerSetup();
  await openSheetWithPreview(t);
  assert.notEqual(t.el('sheet-root').inert, true);
  await t.tap('agent', 0);
  await t.settle();
  assert.equal(t.el('sheet-root').inert, true);
  await t.el('reader-back').listeners.click();
  await t.settle();
  assert.equal(t.el('sheet-root').inert, false);
});

test('a send closes the sheet immediately, schedules a refresh follow-up and tags the card for eight seconds', async () => {
  const clock = { now: 5_000_000 };
  const FakeDate = class extends Date { static now() { return clock.now; } };
  const delays = [];
  const ui = dashboard({ globals: { Date: FakeDate, setTimeout: (fn, ms) => { delays.push(ms); } },
    fetchRequest: async url => response(url === '/api/preview' ? FULL : { ok: true }) });
  const a = sheetAgent({ state: 'WAIT', pending: true, category: 'wait' });
  ui.render({ agents: [a] });
  ui.open(a);
  await settle();
  ui.elements.get('sheet-text').value = 'go';
  await ui.elements.get('sheet-form').listeners.submit({ preventDefault() {} });
  assert.equal(ui.elements.get('sheet-root').hidden, true);
  assert.deepEqual(delays.filter(ms => ms === 280 || ms === 1500), [1500]);
  const tagged = () => { ui.render({ agents: [a] }); return /sent-tag/.test(ui.elements.get('list').innerHTML); };
  clock.now += 7999;
  assert.equal(tagged(), true);
  clock.now += 2;
  assert.equal(tagged(), false);
});

test('once the agent has been seen running, a quick finish does not bring the sent tag back', async () => {
  const clock = { now: 9_000_000 };
  const FakeDate = class extends Date { static now() { return clock.now; } };
  const ui = dashboard({ globals: { Date: FakeDate }, fetchRequest: async url => response(url === '/api/preview' ? FULL : { ok: true }) });
  const wait = sheetAgent({ state: 'WAIT', pending: true, category: 'wait' });
  ui.render({ agents: [wait] });
  ui.open(wait);
  await settle();
  await ui.elements.get('sheet-form').listeners.submit({ preventDefault() {} });
  ui.render({ agents: [{ ...wait, state: 'RUNNING', pending: false, category: 'running' }] });
  clock.now += 3000;
  ui.render({ agents: [{ ...wait, state: 'IDLE', pending: false, category: 'review', needs_review: true, revision: 2, acknowledged: 1 }] });
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /sent-tag/);
});

test('long latest prompts are measured for the fade once the sheet is visible, not only while it is still hidden', () => {
  const seen = [];
  const doc = { querySelectorAll: selector => { seen.push([selector, ui.elements.get('sheet-root').hidden]); return []; } };
  const ui = dashboard({ document: doc, fetchRequest: async () => new Promise(() => {}) });
  ui.elements.get('sheet-root').hidden = true; // as in the real page: closed sheets are display:none
  const a = sheetAgent();
  ui.render({ agents: [a] });
  seen.length = 0;
  ui.open(a);
  const sheetCalls = seen.filter(([selector]) => selector.includes('.sheet-preview .preview-line.user'));
  assert.equal(sheetCalls[0][1], true, 'the first render happens before the sheet is shown');
  assert.equal(sheetCalls.some(([, hidden]) => hidden === false), true, 'and it is measured again once visible');
});

test('the path toggle state is reflected in the markup after a refresh, and a stale fresh-label is reset on open', async () => {
  const { ui } = await sheetWith();
  const attrs = {};
  const button = { setAttribute: (k, v) => { attrs[k] = v; } };
  ui.elements.get('sheet-env').listeners.click({ target: { closest: s => (s === '[data-toggle-path]' ? button : null) } });
  ui.refreshPreview();
  await settle();
  assert.match(ui.elements.get('sheet-env').innerHTML, /data-toggle-path aria-expanded="true"/);
  ui.elements.get('sheet-refresh-label').textContent = '有新訊息';
  ui.close();
  ui.open(sheetAgent());
  assert.equal(ui.elements.get('sheet-refresh-label').textContent, '重新整理內容');
  assert.match(ui.elements.get('sheet-env').innerHTML, /data-toggle-path aria-expanded="false"/);
});

test('the refresh button keeps the verb in its accessible name while it shows only 有新訊息', () => {
  const attrs = {};
  const ui = dashboard();
  const button = ui.elements.get('sheet-refresh');
  button.setAttribute = (k, v) => { attrs[k] = v; };
  button.classList = { toggle() {}, add() {}, remove() {} };
  ui.context.setRefreshLabel(true);
  assert.equal(ui.elements.get('sheet-refresh-label').textContent, '有新訊息');
  assert.match(attrs['aria-label'], /重新整理/);
});

test('closing the sheet hands focus back to the card that opened it', () => {
  const focused = [];
  const a = sheetAgent();
  const ui = dashboard({ document: { querySelector: s => (s.includes('.card-open') ? { focus: () => focused.push(s) } : null) } });
  ui.render({ agents: [a] });
  ui.open(a);
  ui.close();
  assert.equal(focused.length, 1);
  assert.match(focused[0], /lab\|%1\|123/);
});

test('the transcript cannot chain scrolling into the page, and motion respects reduced-motion', () => {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /\.sheet-preview \{[^}]*overscroll-behavior: contain/);
  assert.match(html, /@media \(prefers-reduced-motion: no-preference\) \{\s*\.sheet \{ transition/);
});

const CODEX_QUESTIONS = [
  { id: '["request_user_input_async","call_1",0]', group: '["request_user_input_async","call_1",0]', title: '第 2 項 Staging 要怎麼處理？',
    header: '', options: [{ label: '保留每次 PR push（建議）', description: '' }, { label: '延後到<合併>前', description: '' }], multi: false, free_text: true },
  { id: '["request_user_input_async","call_2",0]', group: '["request_user_input_async","call_2",0]', title: 'Naming?',
    header: '', options: [], multi: false, free_text: true },
];
const CLAUDE_QUESTIONS = [
  { id: 'toolu_1:0', group: 'toolu_1', title: 'Which database?', header: 'DB',
    options: [{ label: 'Postgres', description: 'Relational' }, { label: 'SQLite', description: 'Embedded' }], multi: false, free_text: true },
  { id: 'toolu_1:1', group: 'toolu_1', title: 'Features?', header: 'Feat',
    options: [{ label: 'Logs', description: '' }, { label: 'Metrics', description: '' }, { label: 'Traces', description: '' }], multi: true, free_text: false },
];

function askSetup(questions, answer = () => jsonReply({ ok: true })) {
  const calls = [];
  const ui = dashboard({
    fetchRequest: async (url, options) => {
      const body = JSON.parse(options.body);
      calls.push({ url, body });
      if (url === '/api/preview') return jsonReply({ ...PREVIEW, questions });
      if (url === '/api/answer') return answer(body);
      return jsonReply({ ok: true });
    },
  });
  const current = { ...agent('lab', 'WAIT'), project: 'demo' };
  const el = id => ui.elements.get(id);
  const target = (selector, value) => ({ closest: s => (s === selector ? value : null) });
  return {
    ui, el, calls, current,
    answers: () => calls.filter(call => call.url === '/api/answer'),
    pick: (qid, option) => el('sheet-ask').listeners.click({ target: target('.ask-option', { dataset: { qid, option: String(option) } }) }),
    submit: group => el('sheet-ask').listeners.click({ target: target('.ask-submit', { dataset: { group } }) }),
    type: (qid, value, key) => {
      const input = { value, dataset: { qid }, parentNode: null };
      el('sheet-ask').listeners.input({ target: target('.ask-other', input) });
      if (key) el('sheet-ask').listeners.keydown({ key, isComposing: false, preventDefault() {}, target: target('.ask-other', input) });
    },
    async open() {
      ui.render({ agents: [current] });
      ui.open(current);
      await flush(); await flush();
    },
  };
}

test('pending questions from the preview render as a tray with numbered, escaped options', async () => {
  const t = askSetup(CODEX_QUESTIONS);
  await t.open();
  const html = t.el('sheet-ask').innerHTML;
  assert.equal(t.el('sheet-ask').hidden, false);
  assert.match(html, /第 2 項 Staging 要怎麼處理？/);
  assert.match(html, /<span class="ask-key" aria-hidden="true">1<\/span>/);
  assert.match(html, /延後到&lt;合併&gt;前/);
  assert.doesNotMatch(html, /<合併>/);
  assert.match(html, /placeholder="其他回答（自行輸入）"/);
  assert.match(html, /placeholder="輸入回答"/);
  assert.equal((html.match(/class="ask-submit"/g) || []).length, 2, 'Codex questions are answered one by one');
  const plain = askSetup([]);
  await plain.open();
  assert.equal(plain.el('sheet-ask').hidden, true);
  assert.equal(plain.el('sheet-ask').innerHTML, '');
});

test('a picked option is sent to the pane host and the last answer returns to the dashboard', async () => {
  const t = askSetup(CODEX_QUESTIONS.slice(0, 1));
  await t.open();
  t.pick(CODEX_QUESTIONS[0].id, 1);
  assert.match(t.el('sheet-ask').innerHTML, /aria-checked="true" data-qid="[^"]+" data-option="1"/);
  await t.submit(CODEX_QUESTIONS[0].group);
  await flush();
  assert.deepEqual(t.answers().map(call => call.body), [{
    host: 'lab', pane: '%1', pid: '123', group: CODEX_QUESTIONS[0].group, answers: [{ id: CODEX_QUESTIONS[0].id, option: 1 }],
  }]);
  assert.equal(t.el('sheet-root').hidden, true);
});

test('typed text replaces the option, Enter submits it, and nothing is sent until each question is answered', async () => {
  const t = askSetup(CODEX_QUESTIONS);
  await t.open();
  await t.submit(CODEX_QUESTIONS[1].group);
  assert.equal(t.answers().length, 0);
  assert.match(t.el('sheet-ask').innerHTML, /ask-status bad" role="status">請先選擇或輸入回答/);
  t.pick(CODEX_QUESTIONS[0].id, 0);
  t.type(CODEX_QUESTIONS[0].id, '  我自己的答案 ', 'Enter');
  await flush(); await flush();
  assert.deepEqual(t.answers()[0].body.answers, [{ id: CODEX_QUESTIONS[0].id, text: '我自己的答案' }]);
  // The other question stays open in the sheet after this one is answered.
  assert.equal(t.el('sheet-root').hidden, false);
  assert.doesNotMatch(t.el('sheet-ask').innerHTML, /Staging/);
  assert.match(t.el('sheet-ask').innerHTML, /Naming\?/);
  assert.equal(t.el('sheet-status').textContent, '已送出回答');
});

test('a Claude question group is answered in one request, including multi-select choices', async () => {
  const t = askSetup(CLAUDE_QUESTIONS);
  await t.open();
  const html = t.el('sheet-ask').innerHTML;
  assert.equal((html.match(/class="ask-submit"/g) || []).length, 1);
  assert.match(html, /問題 1\/2 · DB/);
  assert.match(html, /role="checkbox"/);
  assert.equal((html.match(/class="ask-other"/g) || []).length, 1, 'multi-select has no typed answer');
  t.pick('toolu_1:0', 0);
  await t.submit('toolu_1');
  assert.match(t.el('sheet-ask').innerHTML, /每一題都要先選擇或輸入回答/);
  t.pick('toolu_1:1', 2);
  t.pick('toolu_1:1', 0);
  t.pick('toolu_1:1', 2);
  t.pick('toolu_1:1', 1);
  await t.submit('toolu_1');
  await flush();
  assert.deepEqual(t.answers()[0].body.answers, [{ id: 'toolu_1:0', option: 0 }, { id: 'toolu_1:1', options: [0, 1] }]);
});

test('a refused answer keeps the choices and shows why, including for an outdated peer', async () => {
  let reply = { ok: false, error: 'Codex 沒有接受這個回答' };
  const t = askSetup(CODEX_QUESTIONS.slice(0, 1), () => jsonReply(reply, 409));
  await t.open();
  t.pick(CODEX_QUESTIONS[0].id, 1);
  await t.submit(CODEX_QUESTIONS[0].group);
  await flush();
  let html = t.el('sheet-ask').innerHTML;
  assert.match(html, /ask-status bad" role="status">Codex 沒有接受這個回答/);
  assert.match(html, /aria-checked="true" data-qid="[^"]+" data-option="1"/);
  assert.match(html, />送出回答</);
  assert.equal(t.el('sheet-root').hidden, false);
  reply = { ok: false, error: 'peer is running an older tmux-attn without this feature' };
  await t.submit(CODEX_QUESTIONS[0].group);
  await flush();
  assert.match(t.el('sheet-ask').innerHTML, /版本較舊/);
});

test('controls rendered inside an agent-written question title are ignored', async () => {
  const t = askSetup(CODEX_QUESTIONS.slice(0, 1));
  await t.open();
  const inTitle = (selector, value) => ({ closest: s => (s === '.ask-title' ? {} : s === selector ? value : null) });
  t.el('sheet-ask').listeners.click({ target: inTitle('.ask-option', { dataset: { qid: CODEX_QUESTIONS[0].id, option: '1' } }) });
  await t.el('sheet-ask').listeners.click({ target: inTitle('.ask-submit', { dataset: { group: CODEX_QUESTIONS[0].group } }) });
  await flush();
  assert.equal(t.answers().length, 0);
  assert.doesNotMatch(t.el('sheet-ask').innerHTML, /aria-checked="true"/);
});


// ---- model badge

test('the card shows the short model label and leaves the effort and full id for the sheet', () => {
  const ui = dashboard();
  const a = sheetAgent({ model: 'gpt-6.1-sol', model_label: 'gpt-6.1-sol', effort: 'xhigh' });
  ui.render({ agents: [a] });
  const list = ui.elements.get('list').innerHTML;
  assert.match(list, /class="model">gpt-6\.1-sol</);
  assert.doesNotMatch(list, /xhigh/);
});

test('the sheet meta row spells out the full model id and reasoning effort', () => {
  const ui = dashboard();
  const a = sheetAgent({ model: 'claude-sonnet-5-5', model_label: 'sonnet 5.5', effort: 'xhigh' });
  ui.render({ agents: [a] });
  ui.open(a);
  const env = ui.elements.get('sheet-env').innerHTML;
  assert.match(env, /class="model">claude-sonnet-5-5</);
  assert.match(env, /class="effort">xhigh</);
});

test('agents without a known model render no model chip', () => {
  const ui = dashboard();
  const a = sheetAgent({ model: '', model_label: '', effort: '' });
  ui.render({ agents: [a] });
  ui.open(a);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /class="model"/);
  assert.doesNotMatch(ui.elements.get('sheet-env').innerHTML, /class="model"|class="effort"/);
});


// ---- external links

function fakeAnchor(href, tag = 'A') {
  const attrs = href === null ? {} : { href };
  return {
    tagName: tag, attrs,
    hasAttribute: name => Object.hasOwn(attrs, name),
    getAttribute: name => attrs[name] ?? null,
    setAttribute(name, value) { attrs[name] = value; },
  };
}

test('every rendered link opens in a new window without exposing the dashboard', () => {
  const ui = dashboard();
  for (const href of ['https://example.com/pr/1', 'http://100.64.0.10:7788/', '/api/agents', '#top']) {
    const link = fakeAnchor(href);
    ui.context.openLinkInNewWindow(link);
    assert.equal(link.attrs.target, '_blank', href);
    assert.equal(link.attrs.rel, 'noopener noreferrer', href);
  }
});

test('mailto links and non-link nodes are left alone', () => {
  const ui = dashboard();
  const mail = fakeAnchor('mailto:dev@example.com');
  const bare = fakeAnchor(null);
  const span = fakeAnchor('https://example.com', 'SPAN');
  for (const node of [mail, bare, span]) ui.context.openLinkInNewWindow(node);
  assert.equal(mail.attrs.target, undefined);
  assert.equal(bare.attrs.target, undefined);
  assert.equal(span.attrs.target, undefined);
});

test('markdown rendering registers the new-window hook with DOMPurify once', () => {
  const hooks = [];
  const ui = dashboard({ globals: {
    marked: { setOptions() {}, parse: text => `<p>${text}</p>` },
    DOMPurify: { addHook: (name, fn) => hooks.push([name, fn]), sanitize: html => html },
  } });
  ui.context.renderMarkdown('one');
  ui.context.renderMarkdown('two');
  assert.equal(hooks.length, 1);
  assert.equal(hooks[0][0], 'afterSanitizeAttributes');
  assert.equal(hooks[0][1], ui.context.openLinkInNewWindow);
});
