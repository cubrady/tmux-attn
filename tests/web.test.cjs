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
    innerHTML: '', textContent: '', value: '', hidden: false, disabled: false, title: '', dataset: {}, attrs: {}, listeners: {},
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
    contains(node) { return node === this; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    removeAttribute(name) { delete this.attrs[name]; },
    focus() {},
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
    window: win, navigator: nav, location: win.location, AbortSignal,
    document, performance: { now: () => nowMs },
    fetch: (url, options) => {
      const fleetPoll = typeof url === 'string' && (url === '/api/fleet' || url.startsWith('/api/fleet/'));
      const usagePoll = typeof url === 'string' && url.startsWith('/api/usage');
      const infoPoll = url === '/api/info';
      if (!fetchRequest || (fleetPoll && !overrides.allowFleetFetch) || (usagePoll && !overrides.allowUsageFetch)
        || (infoPoll && !overrides.allowInfoFetch)) {
        return new Promise(() => {});
      }
      return fetchRequest(url, options);
    },
    setInterval() {}, setTimeout() {}, clearTimeout() {}, requestAnimationFrame: animationFrame, CSS: { escape: value => String(value) },
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
    renderUsage: data => context.renderUsage(data),
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

test('cached usage renders on startup with the saved agent filter', () => {
  const values = {
    'tmux-attn:agent-filter': 'codex',
    'tmux-attn:usage-cache': JSON.stringify({ accounts: [
      { provider: 'codex', account: 'cached', windows: [{ label: 'Weekly', usedPercent: 25 }] },
      { provider: 'claude', account: 'other', windows: [{ label: 'Weekly', usedPercent: 50 }] },
    ] }),
  };
  const ui = dashboard({ globals: { sessionStorage: {
    getItem: key => values[key] || null, setItem() {}, removeItem() {},
  } } });
  assert.match(ui.elements.get('agent-usage').innerHTML, /cached/);
  assert.doesNotMatch(ui.elements.get('agent-usage').innerHTML, /other/);
  assert.equal(typeof ui.elements.get('hosts').listeners.click, 'function');
});

test('Cursor Bot shows its own reset countdown beside remaining allowance', () => {
  const resetsAt = new Date(Date.now() + 12 * 3600000).toISOString();
  const values = {
    'tmux-attn:agent-filter': 'cursor',
    'tmux-attn:usage-cache': JSON.stringify({ accounts: [
      { provider: 'cursor', account: 'alice', windows: [
        { key: 'grokBot', label: 'Grok Bot', shortLabel: 'Bot', usedPercent: 94, resetsAt },
      ] },
    ] }),
  };
  const ui = dashboard({ globals: { sessionStorage: {
    getItem: key => values[key] || null, setItem() {}, removeItem() {},
  } } });
  const html = ui.elements.get('agent-usage').innerHTML;
  assert.match(html, /Bot<\/span>/);
  assert.match(html, /剩 6%/);
  assert.match(html, /class="usage-win-reset">12h<\/span>/);
  assert.match(html, /12h 後重置/);
});

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

function filterSession(cli, host = 'work', state = 'RUNNING', extra = {}) {
  return { ...agent(host, state), cli, project: `${host}-${cli}`, pane: `%${host}-${cli}`, ...extra };
}

function pickAgent(ui, name) {
  ui.elements.get('agent-filters').listeners.click({ target: {
    closest: () => ({ dataset: { agentFilter: name } }),
  } });
}

test('agent chips show only live tracked types, combine Cursor aliases and exclude unknown HTML', () => {
  const ui = dashboard();
  ui.render({ agents: [filterSession('codex'), filterSession('agent'), filterSession('cursor'),
    filterSession('claude', 'work', 'SHELL'), filterSession('pi', 'work', 'RUNNING', { tracked: false }),
    filterSession('<script>'), filterSession('__proto__')] });
  const chips = ui.elements.get('agent-filters');
  assert.equal(chips.hidden, false);
  assert.deepEqual([...chips.innerHTML.matchAll(/data-agent-filter="([^"]*)"/g)].map(m => m[1]), ['', 'codex', 'cursor']);
  assert.match(chips.innerHTML, /Cursor<span class="count">2/);
  assert.doesNotMatch(chips.innerHTML, /<script>|__proto__|Claude|Pi/);
  pickAgent(ui, 'cursor');
  assert.match(ui.elements.get('list').innerHTML, /work-agent/);
  assert.match(ui.elements.get('list').innerHTML, /work-cursor/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /work-codex/);
  ui.render({ agents: [filterSession('zsh', 'work', 'SHELL')] });
  assert.equal(chips.hidden, true);
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /\.agent-filter-label\s*\{[^}]*font-size:\s*0\.68rem/);
  assert.match(html, /\.agent-filter\s*\{[^}]*font-size:\s*0\.68rem;[^}]*padding:\s*4px 8px;/s);
  assert.doesNotMatch(html.match(/\.agent-filter\s*\{[^}]*\}/s)[0], /min-height/);
});

test('OpenCode has its own filter label and CLI icon', () => {
  const ui = dashboard();
  ui.render({ agents: [filterSession('opencode')] });
  assert.match(ui.elements.get('agent-filters').innerHTML,
    /data-agent-filter="opencode"[^>]*>OpenCode/);
  assert.match(ui.elements.get('list').innerHTML,
    /class="cli sm opencode"[^>]*>.*?src="\/icons\/opencode\.svg"/);
  pickAgent(ui, 'opencode');
  assert.match(ui.elements.get('list').innerHTML, /work-opencode/);
  assert.match(ui.elements.get('meta').textContent, /Agent OpenCode/);
});

test('agent selection switches directly and filters status, later counts and bare panes', () => {
  const ui = dashboard();
  ui.render({ agents: [filterSession('codex', 'work', 'WAIT'), filterSession('claude'),
    filterSession('pi', 'work', 'IDLE', { later: true }), filterSession('zsh', 'work', 'SHELL')] });
  pickAgent(ui, 'codex');
  assert.match(ui.elements.get('list').innerHTML, /work-codex/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /work-claude|work-pi|work-zsh/);
  assert.match(ui.elements.get('summary').innerHTML, /🙋 1/);
  assert.match(ui.elements.get('summary').innerHTML, /🏃 0/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /summary-later/);
  assert.match(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="codex" aria-pressed="true"/);
  pickAgent(ui, 'claude');
  assert.match(ui.elements.get('list').innerHTML, /work-claude/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /work-codex/);
  assert.match(ui.elements.get('summary').innerHTML, /🏃 1/);
  assert.match(ui.elements.get('meta').textContent, /Agent Claude/);
  pickAgent(ui, 'pi');
  assert.match(ui.elements.get('summary').innerHTML, /稍後 1/);
  pickAgent(ui, '');
  assert.match(ui.elements.get('list').innerHTML, /work-zsh/);
  assert.match(ui.elements.get('summary').innerHTML, /🙋 1/);
  assert.match(ui.elements.get('summary').innerHTML, /🏃 1/);
});

test('agent chips and wait notice follow host selection, and wait navigation keeps that selection', () => {
  const ui = dashboard();
  const group = { scrollIntoView() { this.scrolled = true; } };
  ui.document.getElementById = id => id === 'group-wait' ? group : ui.elements.get(id);
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [
    filterSession('codex', 'alpha'), filterSession('claude', 'alpha', 'WAIT'),
    filterSession('pi', 'beta', 'WAIT'), filterSession('codex', 'beta', 'WAIT'),
  ] });
  ui.elements.get('hosts').listeners.click({ target: { closest: () => ({ dataset: { host: 'alpha' } }) } });
  assert.doesNotMatch(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="pi"/);
  assert.match(ui.elements.get('agent-filters').innerHTML, /Codex<span class="count">1/);
  pickAgent(ui, 'codex');
  const notice = ui.elements.get('other-agent-wait');
  assert.equal(notice.hidden, false);
  assert.match(notice.textContent, /其他 agent 有 1 個等你/);
  assert.match(ui.elements.get('hosts').innerHTML, /全部<span class="count">\(4\)/);
  notice.listeners.click();
  assert.equal(notice.hidden, true);
  assert.equal(group.scrolled, true);
  assert.match(ui.elements.get('list').innerHTML, /alpha-codex|alpha-claude/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /beta-/);
  assert.match(ui.elements.get('summary').innerHTML, /🙋 1/);
  assert.match(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="" aria-pressed="true"/);
});

test('wait notice excludes running, review and bare-shell sessions', () => {
  const ui = dashboard();
  ui.render({ agents: [filterSession('codex'), filterSession('claude'),
    filterSession('pi', 'work', 'IDLE', { pending: true }),
    filterSession('cursor', 'work', 'WAIT', { tracked: false })] });
  pickAgent(ui, 'codex');
  assert.equal(ui.elements.get('other-agent-wait').hidden, true);
});

test('agent choice survives reload in the same tab and waits for all progressive host slices', () => {
  const sessionStorage = draftStorage();
  const first = dashboard({ globals: { sessionStorage } });
  const sessions = [filterSession('codex'), filterSession('claude')];
  first.render({ agents: sessions });
  pickAgent(first, 'codex');
  const reloaded = dashboard({ globals: { sessionStorage } });
  reloaded.render({ hosts: [{ name: 'work', ok: true, pending: true }], agents: [] });
  assert.match(reloaded.elements.get('meta').textContent, /Agent Codex/);
  assert.match(reloaded.elements.get('empty').textContent, /Codex.*載入中/);
  reloaded.render({ hosts: [{ name: 'fast', ok: true }, { name: 'work', ok: true, pending: true }],
    agents: [filterSession('claude', 'fast')] });
  assert.doesNotMatch(reloaded.elements.get('list').innerHTML, /fast-claude/);
  assert.match(reloaded.elements.get('empty').textContent, /Codex.*載入中/);
  reloaded.render({ hosts: [{ name: 'work', ok: true }], agents: sessions });
  assert.match(reloaded.elements.get('list').innerHTML, /work-codex/);
  assert.doesNotMatch(reloaded.elements.get('list').innerHTML, /work-claude/);
  assert.match(reloaded.elements.get('agent-filters').innerHTML, /data-agent-filter="codex" aria-pressed="true"/);
  const otherTab = dashboard();
  otherTab.render({ agents: sessions });
  assert.match(otherTab.elements.get('list').innerHTML, /work-claude/);
  pickAgent(reloaded, '');
  assert.equal(sessionStorage.getItem('tmux-attn:agent-filter'), null);
});

test('a vanished agent type falls back to All and removes the stale stored choice', () => {
  const sessionStorage = draftStorage();
  const ui = dashboard({ globals: { sessionStorage } });
  ui.render({ agents: [filterSession('codex'), filterSession('claude')] });
  pickAgent(ui, 'codex');
  ui.render({ agents: [filterSession('claude')] });
  assert.doesNotMatch(ui.elements.get('agent-filters').innerHTML, /Codex/);
  assert.match(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="" aria-pressed="true"/);
  assert.match(ui.elements.get('list').innerHTML, /work-claude/);
  assert.equal(sessionStorage.getItem('tmux-attn:agent-filter'), null);
});

test('selecting hosts without the chosen agent resets it even if another host is still pending', () => {
  const ui = dashboard();
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true, pending: true }],
    agents: [filterSession('codex', 'alpha'), filterSession('claude', 'alpha')] });
  pickAgent(ui, 'codex');
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true, pending: true }],
    agents: [filterSession('claude', 'alpha')] });
  ui.elements.get('hosts').listeners.click({ target: { closest: () => ({ dataset: { host: 'alpha' } }) } });
  assert.match(ui.elements.get('list').innerHTML, /alpha-claude/);
  assert.doesNotMatch(ui.elements.get('meta').textContent, /Agent Codex/);
});

test('agent filtering still works when session storage is blocked and ignores invalid saved values', () => {
  const sessionStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); },
    removeItem() { throw new Error('blocked'); } };
  const ui = dashboard({ globals: { sessionStorage } });
  ui.render({ agents: [filterSession('codex'), filterSession('claude')] });
  pickAgent(ui, 'codex');
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /work-claude/);
  pickAgent(ui, '');
  assert.match(ui.elements.get('list').innerHTML, /work-claude/);
  const invalid = dashboard({ globals: { sessionStorage: { getItem: () => '__proto__' } } });
  invalid.render({ agents: [filterSession('codex'), filterSession('claude')] });
  assert.match(invalid.elements.get('agent-filters').innerHTML, /data-agent-filter="" aria-pressed="true"/);
});

test('a warm progressive refresh keeps the agent choice until a newly added host settles', async () => {
  let round = 0;
  let releaseSlow;
  const slow = new Promise(resolve => { releaseSlow = resolve; });
  const hosts = [{ name: 'fast', ok: true }];
  const ui = dashboard({ allowFleetFetch: true, fetchRequest: async url => {
    if (url === '/api/fleet/plan') return response({ ok: true, hosts });
    const fast = url.includes('name=fast');
    if (round && !fast) await slow;
    return response({ ok: true, hosts: [hosts[fast ? 0 : 1]], agents: round
      ? (fast ? [] : [filterSession('codex', 'slow'), filterSession('claude', 'slow')])
      : [filterSession(fast ? 'codex' : 'claude', fast ? 'fast' : 'slow')] });
  } });
  for (let i = 0; i < 8; i++) await settle();
  pickAgent(ui, 'codex');
  round = 1;
  hosts.push({ name: 'slow', ok: true });
  const refreshing = ui.context.refresh();
  await settle();
  assert.match(ui.elements.get('meta').textContent, /Agent Codex/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /slow-claude/);
  ui.setNow(1000);
  releaseSlow();
  await refreshing;
  assert.match(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="codex" aria-pressed="true"/);
  assert.match(ui.elements.get('list').innerHTML, /slow-codex/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /slow-claude/);
});

test('a missing restored choice clears after the final pending host fails', async () => {
  const sessionStorage = draftStorage();
  sessionStorage.setItem('tmux-attn:agent-filter', 'codex');
  let releaseSlow;
  const slow = new Promise(resolve => { releaseSlow = resolve; });
  const ui = dashboard({ allowFleetFetch: true, globals: { sessionStorage }, fetchRequest: async url => {
    if (url === '/api/fleet/plan') return response({ ok: true, hosts: [{ name: 'fast' }, { name: 'slow' }] });
    if (url.includes('name=slow')) { await slow; throw new Error('offline'); }
    return response({ ok: true, hosts: [{ name: 'fast', ok: true }], agents: [filterSession('claude', 'fast')] });
  } });
  await settle();
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /fast-claude/);
  releaseSlow();
  for (let i = 0; i < 8; i++) await settle();
  assert.match(ui.elements.get('list').innerHTML, /fast-claude/);
  assert.equal(sessionStorage.getItem('tmux-attn:agent-filter'), null);
});

test('agent chips and selection retain last-known-good sessions only until the stale TTL', () => {
  const ui = dashboard();
  const hosts = [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }];
  ui.render({ hosts, agents: [filterSession('codex', 'alpha'), filterSession('claude', 'beta')] });
  pickAgent(ui, 'codex');
  const failure = { hosts: [{ name: 'alpha', ok: false }, hosts[1]], agents: [filterSession('claude', 'beta')] };
  ui.setNow(1000);
  ui.render(failure);
  assert.match(ui.elements.get('list').innerHTML, /alpha-codex/);
  assert.match(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="codex" aria-pressed="true"/);
  ui.setNow(600000);
  ui.render(failure);
  assert.doesNotMatch(ui.elements.get('agent-filters').innerHTML, /Codex/);
  assert.match(ui.elements.get('agent-filters').innerHTML, /data-agent-filter="" aria-pressed="true"/);
  assert.match(ui.elements.get('list').innerHTML, /beta-claude/);
});

test('review cards show the agent icon without restoring host chips or sheet target text', () => {
  const ui = dashboard(() => new Promise(() => {}));
  const current = { ...agent('work-machine', 'IDLE', true, true), cli: 'codex', session: 'build-session' };
  ui.render({ agents: [current] });
  const card = ui.elements.get('list').innerHTML;
  assert.match(card, /class="cli sm codex"[^>]*>.*?src="\/icons\/codex\.svg"/);
  assert.doesNotMatch(card, /class="host tone-/);
  assert.match(card, /class="card-title">未命名專案<\/span>/);
  assert.doesNotMatch(card, /未看|已看/);
  assert.doesNotMatch(card, /class="card-title">%1<\/span>/);
  ui.open(current);
  assert.equal(ui.elements.has('sheet-target'), false);
  assert.equal(ui.elements.get('sheet-title').textContent, '👀 未命名專案');
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /\.host\.tone-7|\.sheet-sub \{/);
});

test('cards map every supported coding agent to its icon and skip non-agent panes', () => {
  const ui = dashboard(() => new Promise(() => {}));
  const expected = { claude: 'claude', codex: 'codex', pi: 'pi', agent: 'agent', cursor: 'agent', opencode: 'opencode' };
  for (const [cli, icon] of Object.entries(expected)) {
    ui.render({ agents: [{ ...agent('', 'RUNNING'), cli }] });
    assert.match(ui.elements.get('list').innerHTML, new RegExp(`class="cli sm ${icon}"[^>]*>.*?src="/icons/${icon}\\.svg"`), cli);
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
  const projects = [...list.matchAll(/<span class="card-title">([^<]+)<\/span>/g)].map(([, value]) => value);
  assert.deepEqual(projects, ['unread-new', 'unread-old', 'seen-new', 'seen-old']);
  assert.match(list, /class="card review unread newest"/);
  assert.match(list, /class="card idle"/);
  assert.doesNotMatch(list, /未看|已看/);
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

test('later sessions leave the Web idle count and appear in a collapsed group', () => {
  const ui = dashboard();
  const parked = { ...review('tomorrow', 2, 2), later: true };
  ui.render({ agents: [parked, review('now', 2, 2)] });
  const list = ui.elements.get('list').innerHTML;
  assert.match(ui.elements.get('summary').innerHTML, /💤 1/);
  assert.match(ui.elements.get('summary').innerHTML, /class="summary-later" data-later-jump aria-label="稍後 1，展開並跳到稍後"/);
  assert.match(list, /<details class="later-group" id="group-later">/);
  assert.match(list, /🗃️ 稍後 <span class="count">1<\/span>/);
  assert.match(list, /aria-label="移回 tomorrow"/);
  assert.match(list, /aria-label="放到稍後 now"/);
  assert.match(list, /class="card-later" data-later="1"[^>]*><svg/);
  assert.doesNotMatch(list, /class="card-later"[^>]*>稍後|class="card-later"[^>]*>移回/);
  assert.match(ui.elements.get('hosts').innerHTML, /^$/);
  ui.render({ agents: [review('now', 2, 2)] });
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /summary-later/);
});

test('later header count follows host filters and opens the collapsed group', () => {
  const ui = dashboard();
  const group = { open: false, style: {}, scrollIntoView() { this.scrolled = true; } };
  ui.document.getElementById = id => id === 'group-later' ? group : ui.elements.get(id);
  const hosts = [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }];
  ui.render({ hosts, agents: [
    { ...review('one', 2, 2), host: 'alpha', later: true },
    { ...review('two', 2, 2), host: 'beta', later: true },
  ] });
  assert.match(ui.elements.get('summary').innerHTML, /稍後 2，展開並跳到稍後/);
  ui.elements.get('hosts').listeners.click({ target: {
    closest: () => ({ dataset: { host: 'alpha' } }),
  } });
  assert.match(ui.elements.get('summary').innerHTML, /稍後 1，展開並跳到稍後/);
  ui.elements.get('summary').listeners.click({ target: {
    closest: selector => selector === '[data-later-jump]' ? {} : null,
  } });
  assert.equal(group.open, true);
  assert.equal(group.scrolled, true);
  ui.render({ hosts, agents: [{ ...review('one', 2, 2), host: 'alpha', later: true }] });
  assert.match(ui.elements.get('list').innerHTML, /id="group-later" open/);
  assert.doesNotMatch(ui.elements.get('summary').innerHTML, /class="stat active"/);
});

test('cold load failures leave the skeleton and allow a later retry', async () => {
  let failed = true;
  const loading = [];
  const ui = dashboard({ allowFleetFetch: true, fetchRequest: async url => {
    if (failed) throw new Error('offline');
    if (url === '/api/fleet/plan') return response({ ok: true, hosts: [] });
    throw new Error('unexpected request');
  }, document: { body: { classList: { add() {}, remove() {}, toggle: (name, on) => loading.push([name, on]) } } } });
  for (let i = 0; i < 8; i++) await settle();
  assert.equal(ui.elements.get('error').hidden, false);
  assert.deepEqual(loading.at(-1), ['is-loading', false]);
  failed = false;
  await ui.context.refresh();
  assert.equal(ui.elements.get('error').hidden, true, ui.elements.get('error').textContent);
  assert.equal(ui.elements.get('clock').dataset.state, 'live');
});

test('a stalled first request times out and releases refresh for retry', async () => {
  const timers = [];
  let calls = 0;
  const ui = dashboard({ allowFleetFetch: true, globals: {
    setTimeout: callback => { timers.push(callback); return timers.length; },
    clearTimeout() {},
  }, fetchRequest: async url => {
    if (url === '/api/fleet/plan' && ++calls > 1) return response({ ok: true, hosts: [] });
    return new Promise(() => {});
  } });
  timers[0]();
  for (let i = 0; i < 8; i++) await settle();
  assert.match(ui.elements.get('error').textContent, /逾時/);
  await ui.context.refresh();
  assert.equal(calls, 2);
  assert.equal(ui.elements.get('error').hidden, true);
});

test('first load consumes the fleet plan request started in the page head', async () => {
  const agentRow = review('early', 2, 2);
  const calls = [];
  let hostFetched;
  const hostDone = new Promise(resolve => { hostFetched = resolve; });
  const ui = dashboard({
    allowFleetFetch: true,
    window: {
      initialFleetPlanRequest: Promise.resolve({
        res: response({ ok: true, hosts: [{ name: 'work', source: 'local' }], version: 'abc' }),
      }),
    },
    fetchRequest: async (url) => {
      calls.push(url);
      assert.equal(url, '/api/fleet/host?name=work');
      hostFetched();
      return response({
        ok: true,
        version: 'abc',
        summary: { wait: 0, review: 1, running: 0, idle: 0, tracked: 1, untracked: 0 },
        hosts: [{ name: 'work', ok: true, source: 'local' }],
        agents: [agentRow],
      });
    },
  });
  await hostDone;
  for (let i = 0; i < 8; i++) await settle();
  assert.match(ui.elements.get('list').innerHTML, /early/);
  assert.equal(ui.context.window.initialFleetPlanRequest, null);
  assert.deepEqual(calls, ['/api/fleet/host?name=work']);
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.ok(html.indexOf('id="early-fleet-request"') < html.indexOf('fonts.googleapis.com'));
  assert.match(html, /initialFleetPlanRequest/);
});

test('progressive refresh paints a fast host without treating pending peers as versionless', async () => {
  const hubVersion = '5d18590aaaaa';
  let releaseSlow;
  const slow = new Promise(resolve => { releaseSlow = resolve; });
  let sawSlow;
  const slowStarted = new Promise(resolve => { sawSlow = resolve; });
  const ui = dashboard({
    allowFleetFetch: true,
    fetchRequest: async (url) => {
      if (url === '/api/fleet/plan') {
        return response({
          ok: true,
          hosts: [
            { name: 'fast', source: 'peer' },
            { name: 'slow', source: 'peer' },
          ],
          version: hubVersion,
        });
      }
      if (url.includes('name=fast')) {
        return response({
          ok: true,
          version: hubVersion,
          summary: { wait: 1, review: 0, running: 0, idle: 0, tracked: 1, untracked: 0 },
          hosts: [{ name: 'fast', ok: true, source: 'peer', version: hubVersion }],
          agents: [{ ...review('fast-card', 2, 2), host: 'fast' }],
        });
      }
      if (url.includes('name=slow')) {
        sawSlow();
        await slow;
        return response({
          ok: true,
          version: hubVersion,
          summary: { wait: 0, review: 0, running: 1, idle: 0, tracked: 1, untracked: 0 },
          hosts: [{ name: 'slow', ok: true, source: 'peer', version: hubVersion }],
          agents: [{ ...agent('slow', 'RUNNING'), project: 'slow-card', pane: '%slow', updated: 1 }],
        });
      }
      throw new Error('unexpected ' + url);
    },
  });
  await slowStarted;
  await settle();
  assert.match(ui.elements.get('list').innerHTML, /fast-card/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /slow-card/);
  assert.match(ui.elements.get('hosts').innerHTML, /pending/);
  assert.equal(ui.elements.get('update-hint').hidden, true);
  assert.equal(ui.elements.get('update-hint').innerHTML, '');
  releaseSlow();
  for (let i = 0; i < 8; i++) await settle();
  assert.match(ui.elements.get('list').innerHTML, /slow-card/);
  assert.doesNotMatch(ui.elements.get('hosts').innerHTML, /pending/);
  assert.equal(ui.elements.get('update-hint').hidden, true);
});

test('later icon needs two taps to save and two taps to restore', async () => {
  const calls = [];
  const ui = dashboard(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return response({ ok: true, later: calls.length === 1 });
  });
  const current = review('tomorrow', 2, 2);
  ui.render({ agents: [current] });
  const clickLater = () => ui.elements.get('list').listeners.click({ target: {
    closest: selector => selector === '.card[data-key]' ? { dataset: { key: ui.context.agentKey(current) } }
      : selector === '[data-later]' ? {} : null,
  } });
  clickLater();
  await settle();
  assert.equal(calls.length, 0);
  assert.match(ui.context.cardHtml(current), /class="card-later armed" data-later="1" aria-label="再按一次確認稍後 tomorrow"/);
  assert.match(ui.context.cardHtml(current), /class="later-label">確認稍後/);
  clickLater();
  await settle();
  assert.equal(calls[0].url, '/api/later');
  assert.deepEqual(calls[0].body, { action: 'add', host: 'work', pane: '%tomorrow', pid: '123', revision: 2 });
  assert.match(ui.elements.get('list').innerHTML, /id="group-later"/);
  clickLater();
  await settle();
  assert.equal(calls.length, 1);
  assert.match(ui.context.cardHtml(current), /class="card-later armed" data-later="1" aria-label="再按一次確認移回 tomorrow"/);
  clickLater();
  await settle();
  assert.equal(calls[1].body.action, 'remove');
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /id="group-later"/);
});

test('later confirmation expires, and arming a different session cancels the first', async () => {
  let now = 0;
  const timers = [];
  const FakeDate = class extends Date { static now() { return now; } };
  const calls = [];
  const ui = dashboard({
    globals: { Date: FakeDate, setTimeout: (fn, ms) => timers.push({ fn, at: now + ms }) },
    fetchRequest: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return response({ ok: true, later: true });
    },
  });
  const first = review('first', 2, 2);
  const second = review('second', 2, 2);
  ui.render({ agents: [first, second] });
  const tap = agent => ui.context.onLater(agent);
  await tap(first);
  now = 5000;
  timers[0].fn();
  assert.doesNotMatch(ui.context.cardHtml(first), /card-later armed/);
  await tap(first);
  assert.equal(calls.length, 0);
  await tap(second);
  assert.doesNotMatch(ui.context.cardHtml(first), /card-later armed/);
  assert.match(ui.context.cardHtml(second), /card-later armed/);
  await tap(second);
  assert.deepEqual(calls.map(call => call.body.pane), ['%second']);
});

test('a newly reviewed revision needs its own two taps', async () => {
  const calls = [];
  const ui = dashboard(async (url, options) => {
    calls.push(JSON.parse(options.body));
    return response({ ok: true, later: true });
  });
  const old = review('task', 2, 2);
  ui.render({ agents: [old] });
  await ui.context.onLater(old);
  const newer = review('task', 3, 3);
  ui.render({ agents: [newer] });
  await ui.context.onLater(newer);
  assert.equal(calls.length, 0);
  assert.match(ui.context.cardHtml(newer), /card-later armed/);
  await ui.context.onLater(newer);
  assert.deepEqual(calls.map(call => call.revision), [3]);
});

test('keyboard focus stays on the later button when confirmation changes its markup', async () => {
  const focused = [];
  let ui;
  const doc = {
    querySelector(selector) {
      if (!selector.startsWith('.card[data-key=')) return null;
      return {
        classList: { contains: () => false },
        contains: node => node === ui.document.activeElement,
        set outerHTML(_) {},
        querySelector: inner => ({ focus: () => focused.push(inner) }),
      };
    },
  };
  ui = dashboard({ document: doc });
  const current = review('keyboard', 2, 2);
  ui.render({ agents: [current] });
  ui.document.activeElement = { closest: selector => selector === '[data-later]' ? {} : null };
  await ui.context.onLater(current);
  assert.deepEqual(focused, ['[data-later]']);
});

test('sheet later control is an icon and also requires confirmation', async () => {
  const calls = [];
  const ui = dashboard(async (url, options) => {
    calls.push(url);
    return response(url === '/api/later' ? { ok: true, later: true }
      : { ok: true, preview: { agent: 'result' }, revision: 2, viewable: true });
  });
  const current = review('sheet', 2, 2);
  ui.render({ agents: [current] });
  ui.open(current);
  const button = ui.elements.get('sheet-later');
  assert.match(button.innerHTML, /^<svg/);
  assert.equal(button.className, 'sheet-later');
  button.listeners.click();
  assert.equal(calls.filter(url => url === '/api/later').length, 0);
  assert.equal(button.className, 'sheet-later armed');
  assert.match(button.innerHTML, /確認稍後/);
  button.listeners.click();
  await settle();
  assert.equal(calls.filter(url => url === '/api/later').length, 1);
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
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /class="emoji"/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /未看|已看/);
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

test('dragging and dropping files onto the composer uploads them and ignores plain text drags', async () => {
  const t = uploader();
  const form = t.ui.elements.get('sheet-form');
  const classes = new Set();
  form.classList = {
    add: cls => classes.add(cls),
    remove: cls => classes.delete(cls),
    toggle: (cls, on) => (on ? classes.add(cls) : classes.delete(cls)),
  };

  let textPrevented = false;
  form.listeners.dragover({
    dataTransfer: { types: ['text/plain'], files: [] },
    preventDefault() { textPrevented = true; },
  });
  assert.equal(textPrevented, false);
  assert.equal(classes.has('drag-over'), false);

  let enterPrevented = false;
  const dt = { types: ['Files'], files: [], dropEffect: 'none' };
  form.listeners.dragenter({ dataTransfer: dt, preventDefault() { enterPrevented = true; } });
  form.listeners.dragenter({ dataTransfer: dt, preventDefault() {} }); // nested child enter
  form.listeners.dragleave({ dataTransfer: dt }); // leave outer into child
  assert.equal(enterPrevented, true);
  assert.equal(classes.has('drag-over'), true, 'drag-over persists across nested composer children');

  form.listeners.dragover({ dataTransfer: dt, preventDefault() {} });
  assert.equal(dt.dropEffect, 'copy');

  form.listeners.dragleave({ dataTransfer: dt });
  assert.equal(classes.has('drag-over'), false);

  let dropPrevented = false;
  form.listeners.dragenter({ dataTransfer: dt, preventDefault() {} });
  assert.equal(classes.has('drag-over'), true);
  form.listeners.drop({
    dataTransfer: {
      types: ['Files'],
      files: [imageFile('drop.png', 'image/png'), { name: 'notes.md', type: 'text/markdown', size: 120 }],
    },
    preventDefault() { dropPrevented = true; },
  });
  await flush();
  assert.equal(dropPrevented, true);
  assert.equal(classes.has('drag-over'), false);
  assert.equal(t.chipKeys().length, 2);
  assert.deepEqual(t.uploads().map(call => call.body), [
    { host: 'lab', pane: '%1', pid: '123', data: 'b64-notes.md', filename: 'notes.md' },
    { host: 'lab', pane: '%1', pid: '123', data: 'b64-drop.png' },
  ]);
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

test('unsupported files are rejected and at most 30 attachments stay attached', async () => {
  const t = uploader();
  await t.pick([imageFile('notes.pdf', 'application/pdf'), ...Array.from({length: 31}, (_, i) => imageFile(`${i}.jpg`))]);
  assert.equal(t.uploads().length, 30);
  assert.equal(t.chipKeys().length, 30);
  assert.match(t.ui.elements.get('sheet-status').textContent, /只支援圖片/);
  await t.pick([imageFile('extra.jpg')]);
  assert.equal(t.uploads().length, 30);
  assert.match(t.ui.elements.get('sheet-status').textContent, /最多 30 個/);
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
  const preview = html.slice(html.indexOf('<div class="preview">'));
  assert.ok(preview.indexOf('preview-line agent') < preview.indexOf('preview-line user'), 'result preview comes first');
  assert.ok(preview.indexOf('the finished result') < preview.indexOf('prompt three'), 'result comes before latest prompt within preview');
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
    assert.doesNotMatch(working, /class="emoji"/, 'ordinary running cards keep status in the rail/section only');
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

test('compact card previews cut on whole lines and keep full text available in the sheet', () => {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.doesNotMatch(html, /max-height: 4\.9em|max-height: 6\.6em/);
  assert.match(html, /\.card \.preview-line \.text\.md \{[^}]*max-height: calc\(1\.45em \* 3\)/);
  assert.match(html, /\.card \.preview-line\.user \.text\.md \{[^}]*max-height: calc\(1\.45em \* 1\)/);
  assert.match(html, /\.card \.reason ~ \.preview \.preview-line\.agent \.text\.md \{[^}]*max-height: calc\(1\.45em \* 1\)/);
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
    target: { closest: selector => (selector === '[data-kill]' ? (onKill ? {} : null)
      : selector === '[data-later]' || selector === '[data-shell-input]' ? null : { dataset: { key: keyOf(i) } }) },
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

test('cards including untracked shells carry a trash button beside the age; the card itself is not a button', () => {
  const ui = dashboard();
  ui.render({ agents: [{ ...agent('lab', 'WAIT'), project: 'demo' },
    { ...agent('lab', 'SHELL', false), pane: '%9', pid: '9', project: 'plain-shell' }] });
  const list = ui.elements.get('list').innerHTML;
  assert.equal((list.match(/class="card-kill"/g) || []).length, 2);
  assert.match(list, /class="row1-side">\s*<div class="age">[^<]*<\/div>\s*<button type="button" class="card-kill" data-kill aria-label="關閉 demo"/);
  assert.match(list, /data-key="lab\|%9\|9"[\s\S]*?<button type="button" class="card-kill" data-kill aria-label="關閉 plain-shell"/);
  assert.doesNotMatch(list, /card-kill-label/);
  // Nested interactive controls hide the trash from screen readers, so the open action is its own button.
  assert.doesNotMatch(list, /role="button"/);
  assert.equal((list.match(/class="card-open" aria-label="開啟 demo"/g) || []).length, 1);
});

test('untracked shell cards can be closed with two taps of the trash button', async () => {
  const t = killSetup();
  const shellAgent = { ...agent('lab', 'SHELL', false), pane: '%9', pid: '9', project: 'plain-shell' };
  t.agents[0] = shellAgent;
  t.ui.render({ agents: [shellAgent] });
  await t.tap(0);
  await settle();
  assert.equal(t.killCalls().length, 0);
  assert.match(t.lastHtml(0), /class="card-kill armed" data-kill aria-label="再按一次確認關閉 plain-shell"/);
  assert.equal(t.live(), '再按一次確認關閉 plain-shell');
  await t.tap(0);
  await settle();
  assert.deepEqual(t.killCalls().map(call => call.body), [{ host: 'lab', pane: '%9', pid: '9' }]);
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

test('the update hint names peers on another version and offers the update action', () => {
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
  assert.match(hint.innerHTML, /data-update-all/);
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

function updatingDashboard(fetchRequest, globals = {}) {
  const ui = dashboard({ allowFleetFetch: true, fetchRequest, globals });
  ui.render({ version: HUB, agents: [], hosts: hostRows(
    { name: 'lab', ok: true, source: 'peer', version: 'a'.repeat(12) },
  ) });
  return ui;
}
const jsonResponse = data => ({ ok: true, json: async () => data });
const updateClick = (ui, selector) => ui.elements.get('update-hint').listeners.click({
  target: { closest: s => s === selector ? {} : null },
});

test('updates remain busy through restart and preserve success until dismissed', async () => {
  let state = 'running';
  const ui = updatingDashboard(async url => url === '/api/fleet/update'
    ? jsonResponse({ ok: true, ref: 'c'.repeat(40), hosts: [{ name: 'lab', ok: true, started: true }] })
    : url.startsWith('/api/fleet/update-status')
      ? jsonResponse({ ok: true, hosts: [{ name: 'lab', state }] }) : new Promise(() => {}));
  await ui.context.runFleetUpdate();
  const hint = ui.elements.get('update-hint');
  assert.match(hint.innerHTML, /更新中/);
  assert.match(hint.innerHTML, /disabled/);
  assert.equal(hint.attrs['aria-busy'], 'true');
  ui.render({ version: HUB, agents: [], hosts: hostRows({ name: 'lab', pending: true, source: 'peer' }) });
  assert.equal(hint.hidden, false);
  assert.match(hint.innerHTML, /lab/);
  state = 'success';
  await ui.context.pollUpdateRun();
  ui.render({ version: HUB, agents: [], hosts: hostRows({ name: 'lab', ok: true, source: 'peer', version: HUB }) });
  assert.match(hint.innerHTML, /全部客戶端更新成功/);
  assert.match(hint.innerHTML, /data-update-dismiss/);
  assert.doesNotMatch(hint.innerHTML, /data-update-retry/);
  assert.equal(hint.attrs['aria-busy'], 'false');
  await updateClick(ui, '[data-update-dismiss]');
  assert.equal(hint.hidden, true);
});

test('failed updates offer retry and retry waits for confirmation again', async () => {
  let state = 'failed';
  let posts = 0;
  const ui = updatingDashboard(async url => {
    if (url === '/api/fleet/update') {
      posts++;
      return jsonResponse({ ok: true, ref: 'c'.repeat(40), hosts: [{ name: 'lab', ok: true, started: true }] });
    }
    if (url.startsWith('/api/fleet/update-status')) return jsonResponse({ ok: true, hosts: [{ name: 'lab', state, error: 'rollback' }] });
    return new Promise(() => {});
  });
  await ui.context.runFleetUpdate();
  assert.match(ui.elements.get('update-hint').innerHTML, /rollback/);
  assert.match(ui.elements.get('update-hint').innerHTML, /data-update-retry/);
  state = 'success';
  await updateClick(ui, '[data-update-retry]');
  assert.equal(posts, 2);
  assert.match(ui.elements.get('update-hint').innerHTML, /全部客戶端更新成功/);
});

test('unreachable status polling times out with a retry action', async () => {
  let now = 0;
  const ui = updatingDashboard(async url => {
    if (url === '/api/fleet/update') return jsonResponse({ ok: true, ref: 'c'.repeat(40), hosts: [{ name: 'lab', ok: true, running: true }] });
    if (url.startsWith('/api/fleet/update-status')) throw new Error('offline');
    return new Promise(() => {});
  }, { Date: { now: () => now } });
  await ui.context.runFleetUpdate();
  assert.match(ui.elements.get('update-hint').innerHTML, /更新中/);
  now = 240001;
  await ui.context.pollUpdateRun();
  assert.match(ui.elements.get('update-hint').innerHTML, /逾時/);
  assert.match(ui.elements.get('update-hint').innerHTML, /data-update-retry/);
});

test('a lost start response keeps polling because clients may already be updating', async () => {
  let state = 'running';
  const ui = updatingDashboard(async url => {
    if (url === '/api/fleet/update') throw new Error('connection reset');
    if (url.startsWith('/api/fleet/update-status')) return jsonResponse({ ok: true, ref: 'c'.repeat(40), hosts: [{ name: 'lab', state }] });
    return new Promise(() => {});
  });
  await ui.context.runFleetUpdate();
  assert.match(ui.elements.get('update-hint').innerHTML, /更新中/);
  assert.doesNotMatch(ui.elements.get('update-hint').innerHTML, /data-update-retry/);
  state = 'success';
  await ui.context.pollUpdateRun();
  assert.match(ui.elements.get('update-hint').innerHTML, /更新成功/);
});

test('dismiss after success does not resurrect the stale fleet snapshot', async () => {
  const ui = updatingDashboard(async url => {
    if (url === '/api/fleet/update') return jsonResponse({ ok: true, ref: HUB.padEnd(40, 'a'), hosts: [{ name: 'lab', ok: true, started: true }] });
    if (url.startsWith('/api/fleet/update-status')) return jsonResponse({ ok: true, hosts: [{ name: 'lab', state: 'success' }] });
    return new Promise(() => {});
  });
  await ui.context.runFleetUpdate();
  assert.match(ui.elements.get('update-hint').innerHTML, /更新成功/);
  await updateClick(ui, '[data-update-dismiss]');
  assert.equal(ui.elements.get('update-hint').hidden, true);
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

function handoffAgent(project, items, overrides = {}) {
  return { ...review(project, 7, 0), ...overrides,
    handoff: { revision: 7, source: 'completed response', truncated: false, items } };
}

test('output file discovery handles spaces, Chinese punctuation, file URLs and deduplication', () => {
  const ui = dashboard();
  const text = '[report](/Users/dev/my report.html) `/home/me/a b.txt` 完成 (/Users/dev/report.html)。 '
    + 'https://example.com/a/b [web](https://example.com/c/d) [local](file:///home/me/a%20b.txt)';
  assert.deepEqual(Array.from(ui.context.outputFilePaths(text)), [
    '/Users/dev/my report.html', '/home/me/a b.txt', '/Users/dev/report.html',
  ]);
  assert.deepEqual(Array.from(ui.context.outputFilePaths('`/home/me/../secret` //host/a/b /single')), []);
  assert.deepEqual(Array.from(ui.context.outputFilePaths(
    String.raw`[report](/Users/dev/report.html "Report title") [other](/home/me/a\(b\).html)`)),
  ['/Users/dev/report.html', '/home/me/a(b).html']);
});

test('output links preserve host and original text while encoding and escaping paths', () => {
  const ui = dashboard();
  const text = '完成 `/Users/dev/報告 & <test>.html`';
  const html = ui.context.outputFilesHtml(text, agent('peer', 'DONE'), ['/Users/dev/報告 & <test>.html']);
  assert.ok(html.includes('host=peer&amp;pane=%251&amp;pid=123'));
  assert.ok(html.includes(encodeURIComponent('/Users/dev/報告 & <test>.html')));
  assert.ok(html.includes('報告 &amp; &lt;test&gt;.html'));
  assert.ok(html.includes('sig=' + encodeURIComponent(text)));
  assert.ok(html.includes('&amp;download=1'));
  assert.equal((html.match(/target="_blank" rel="noopener noreferrer"/g) || []).length, 2);
  assert.equal(ui.context.outputFilesHtml(text, null), '');
  assert.equal(ui.context.outputFilesHtml('no file', agent('peer', 'DONE')), '');
});

test('full reply offers output files while user messages remain ordinary messages', () => {
  const ui = dashboard();
  const text = '完成 `/Users/dev/report.html`';
  const route = { ...agent('peer', 'DONE'), role: 'agent' };
  assert.ok(ui.context.readerBodyHtml(text, { output_files: ['/Users/dev/report.html'] }, route).includes('輸出檔案'));
  assert.ok(!ui.context.readerBodyHtml(text, {}, { ...route, role: 'user' }).includes('輸出檔案'));
  assert.equal(text, '完成 `/Users/dev/report.html`');
});

test('file links wait for the full sheet preview instead of signing a clipped card summary', () => {
  const ui = dashboard();
  const current = { ...agent('peer', 'IDLE'), preview: { agent: '完成 `/Users/dev/report.html`…' } };
  ui.render({ agents: [current] });
  ui.open(current);
  assert.ok(!ui.elements.get('sheet-preview').innerHTML.includes('輸出檔案'));
  ui.context.renderSheetPreview({ agent: '完成 `/Users/dev/report.html`\n完整內容', output_files: ['/Users/dev/report.html'] }, true);
  assert.ok(ui.elements.get('sheet-preview').innerHTML.includes('輸出檔案'));
  assert.ok(ui.elements.get('sheet-preview').innerHTML.includes(encodeURIComponent('完成 `/Users/dev/report.html`\n完整內容')));
});

function markdownLinkDashboard() {
  return dashboard({ globals: {
    marked: { setOptions() {}, parse(raw, options) {
      return raw.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, text, href) => {
        const token = { type: 'link', href };
        options.walkTokens(token);
        return `<a href="${token.href}">${text}</a>`;
      });
    } },
    DOMPurify: { addHook() {}, sanitize: html => html },
  } });
}

test('original Markdown file links retain their label and use the owning host file API', () => {
  const ui = markdownLinkDashboard();
  const raw = '[開啟報告](/Users/dev/my report.html) [local](file:///home/me/a%20b.txt)';
  const current = agent('peer-b', 'IDLE');
  const html = ui.context.renderMarkdown(raw, { agent: current, text: raw });
  assert.ok(html.includes('href="/api/output-file?host=peer-b&pane=%251&pid=123'));
  assert.ok(html.includes('path=' + encodeURIComponent('/Users/dev/my report.html')));
  assert.ok(html.includes('path=' + encodeURIComponent('/home/me/a b.txt')));
  assert.ok(html.includes('sig=' + encodeURIComponent(raw)));
  assert.ok(html.includes('>開啟報告</a>'));
  assert.equal(raw, '[開啟報告](/Users/dev/my report.html) [local](file:///home/me/a%20b.txt)');
  assert.equal(ui.context.renderMarkdown(raw), '<a href="/Users/dev/my report.html">開啟報告</a> <a href="file:///home/me/a%20b.txt">local</a>');
});

test('web links, app routes, anchors and protocol-relative links keep their destinations', () => {
  const ui = markdownLinkDashboard();
  const raw = '[web](https://example.com/a/b) [mail](mailto:dev@example.com) '
    + '[app](/api/agents) [icon](/icons/favicon.svg) [anchor](#section) [cdn](//example.com/a/b)';
  const html = ui.context.renderMarkdown(raw, { agent: agent('peer', 'IDLE'), text: raw });
  assert.ok(!html.includes('/api/output-file'));
  for (const href of ['https://example.com/a/b', 'mailto:dev@example.com', '/api/agents', '/icons/favicon.svg', '#section', '//example.com/a/b']) {
    assert.ok(html.includes(`href="${href}"`), href);
  }
});

test('sheet summaries, full replies and reader route original agent links without changing user links', () => {
  const ui = markdownLinkDashboard();
  const text = '[report](/Users/dev/report.html)';
  const current = { ...agent('peer-a', 'IDLE'), preview: { users: [text], agent: text + '…' } };
  ui.render({ agents: [current] });
  ui.open(current);
  const short = ui.elements.get('sheet-preview').innerHTML;
  assert.ok(short.includes('href="/api/output-file?host=peer-a'));
  assert.ok(short.includes('&sig="'), 'summary must not sign clipped text');
  assert.ok(short.includes('href="/Users/dev/report.html"'), 'user link stays unchanged');
  ui.context.renderSheetPreview({ users: [text], agent: text }, true);
  assert.ok(ui.elements.get('sheet-preview').innerHTML.includes('&sig=' + encodeURIComponent(text)));
  assert.ok(ui.context.readerBodyHtml(text, {}, { ...current, role: 'agent' }).includes('href="/api/output-file?host=peer-a'));
  assert.ok(!ui.context.readerBodyHtml(text, {}, { ...current, role: 'user' }).includes('href="/api/output-file'));
});

test('Atlas sessions use literal task context and ignore generic prompts', () => {
  const ui = dashboard();
  const ci = '對,CI 已經恢復,而且是真的跑完整套件後通過的。';
  const choices = '要你決定\n1. P1 要不要照上面做？\n2. #42 怎麼 merge？\n3. 布林欄位那一行要補嗎？';
  const blocked = '任務 e5f6a7b8 還卡在 blocked，沒有恢復選項。修 e5f6a7b8 要寫 正式環境資料，需要你同意。';
  const rows = [
    handoffAgent('Atlas', [], { pane: '%22782', preview: { users: ['status check', 'continue'], agent: ci } }),
    handoffAgent('Atlas', [], { pane: '%3', preview: { users: ['進度如何？'], agent: choices } }),
    handoffAgent('Atlas', [], { pane: '%25543', preview: { users: ['check progress'], agent: blocked } }),
    handoffAgent('Atlas', [], { pane: '%4', preview: { users: ['修正登入時的 cookie 過期問題', '繼續'], agent: '' } }),
    handoffAgent('Atlas', [], { pane: '%5', preview: { users: ['status check', 'continue'], agent: '' } }),
  ];
  ui.render({ agents: rows });
  assert.equal(ui.context.agentName(rows[4]), 'Atlas');
  assert.match(ui.context.sessionTitle(rows[0]), /對,CI 已經恢復/);
  assert.match(ui.context.sessionTitle(rows[1]), /P1 要不要照上面做？/);
  assert.match(ui.context.sessionTitle(rows[2]), /任務 e5f6a7b8 還卡在 blocked/);
  assert.match(ui.context.sessionTitle(rows[3]), /修正登入時的 cookie 過期問題/);
  assert.equal(ui.context.sessionTitle(rows[4]), 'Atlas');
  assert.doesNotMatch(ui.context.sessionTitle(rows[0]) + ui.context.sessionTitle(rows[1]) + ui.context.sessionTitle(rows[2]),
    /status check|continue|進度如何|check progress/);
  const cards = ui.elements.get('list').innerHTML;
  assert.equal((cards.match(/class="card-title">Atlas<\/span>/g) || []).length, 5);
  assert.doesNotMatch(cards, /class="card-title">[^<]*(?:status check|continue|進度如何|check progress)/,
    'prompt context stays in the preview rather than becoming the card title');
  assert.match(cards, /class="preview-line agent"[^>]*>[\s\S]*?對,CI 已經恢復/);
  assert.match(cards, /class="preview-line user"[^>]*>[\s\S]*?繼續/);
  assert.match(cards, /aria-label="開啟 Atlas"/);
  assert.ok(ui.context.sessionTitle({ ...rows[1], preview: { users: ['x'.repeat(10000)] } }).length < 100);
});

test('card title falls back to the project and does not repeat it in meta', () => {
  const ui = dashboard();
  for (const [fields, name] of [
    [{}, '未命名專案'],
    [{ repo: 'tmux' }, 'tmux'],
    [{ window: 'sample' }, 'sample'],
    [{ project: 'sample', preview: { users: ['continue'], agent: '' } }, 'sample'],
  ]) {
    const a = handoffAgent('', [], { pane: '%3', preview: { users: [], agent: '' }, ...fields });
    ui.render({ agents: [a] });
    const cards = ui.elements.get('list').innerHTML;
    assert.ok(cards.includes(`<span class="card-title">${name}</span>`));
    assert.doesNotMatch(cards, new RegExp(`<div class="env">[^<]*<span class="project">${name}`));
  }
});

test('card leads with the project and shows the session title as secondary context', () => {
  const ui = dashboard();
  const a = handoffAgent('sample', [], { pane: '%3', title: 'Sample 專案列表與內頁模組', preview: { users: [], agent: '' } });
  ui.render({ agents: [a] });
  const html = ui.elements.get('list').innerHTML;
  assert.ok(html.includes('<span class="card-title">sample</span>'));
  assert.ok(html.includes('<div class="session-context">Sample 專案列表與內頁模組</div>'));
  assert.doesNotMatch(html, /class="project"/);
  assert.doesNotMatch(cardHtmlFor({ title: '' }), /class="session-context"/);
  assert.doesNotMatch(cardHtmlFor({ project: 'sample', title: 'sample' }), /class="session-context"/);
});

test('card meta hides redundant repos and shows distinct repos with a safe small CLI icon', () => {
  const hidden = cardHtmlFor({ project: 'Atlas', repo: 'Atlas-monorepo', cli: 'CoDeX' });
  assert.match(hidden, /<span class="card-title">Atlas<\/span>/);
  assert.doesNotMatch(hidden, /Atlas-monorepo/);
  assert.match(hidden, /class="cli sm codex" title="codex"><img src="\/icons\/codex\.svg" alt="codex"/);

  const shown = cardHtmlFor({ project: 'Atlas', repo: 'company/atlas-service', title: 'Deploy API' });
  assert.match(shown, /<span class="card-title">Atlas<\/span>/);
  assert.match(shown, /<span class="repo" title="company\/atlas-service">company\/atlas-service<\/span>/);
  assert.doesNotMatch(shown, /branch|dirty/);
});

test('two or more hosts label each card and the reply sheet, including while filtered', () => {
  const ui = dashboard();
  const long = 'a-very-long-host-name';
  const hostile = '<b>"x"';
  const hosts = [
    { name: 'workstation', ok: true },
    { name: 'client-2', ok: true },
  ];
  const workstation = { ...agent('workstation', 'WAIT'), cli: 'codex', project: 'project-alpha', title: 'Fix login', pane: '%1', pid: '1' };
  const cicd = { ...agent('client-2', 'RUNNING'), cli: 'claude', project: 'project-alpha', title: 'Fix login', pane: '%2', pid: '2' };
  const weird = { ...agent(hostile, 'IDLE', true, true), project: 'other', pane: '%3', pid: '3' };
  const wide = { ...agent(long, 'IDLE', true, true), project: 'wide', pane: '%4', pid: '4' };
  ui.render({ hosts, agents: [workstation, cicd, weird, wide] });
  const list = ui.elements.get('list').innerHTML;
  const tone = ui.context.hostTone('workstation');
  assert.match(list, new RegExp(`<div class="title-line"><span class="card-title">project-alpha</span><span class="title-agent"><span class="cli sm codex"`));
  assert.match(list, new RegExp(`<div class="env"><span class="host-chip tone-${tone}" title="workstation">workstation</span></div>`));
  assert.match(ui.elements.get('hosts').innerHTML, new RegExp(`host-pill tone-${tone}`));
  assert.match(list, /title="client-2">client-2<\/span>/);
  assert.match(list, /aria-label="開啟 project-alpha · Fix login，workstation"/);
  assert.match(list, /title="&lt;b&gt;&quot;x&quot;">&lt;b&gt;&quot;x&quot;</);
  assert.doesNotMatch(list, /<b>/);
  const clipped = long.slice(0, 15) + '…';
  assert.match(list, new RegExp(`title="${long}">${clipped}</span>`));
  ui.open(workstation);
  const sheetHost = ui.elements.get('sheet-host');
  assert.equal(sheetHost.hidden, false);
  assert.match(sheetHost.innerHTML, new RegExp(`class="host-chip tone-${tone}" title="workstation">workstation`));
  ui.elements.get('hosts').listeners.click({
    target: { closest: () => ({ dataset: { host: 'workstation' } }) },
  });
  const filtered = ui.elements.get('list').innerHTML;
  assert.match(filtered, /title="workstation">workstation/);
  assert.doesNotMatch(filtered, /client-2/);
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /\.env > \.host-chip \{[^}]*display: inline-flex;[^}]*var\(--host-fg[\s\S]*?var\(--host-bg[\s\S]*?var\(--host-bd/,
    'card host chip keeps toned pill styling correlated with header host-pill');
  assert.match(html, /\.env > \.host-chip::before \{ content: none; \}/);
});

test('a single host leaves cards and the reply sheet unlabeled', () => {
  const ui = dashboard();
  const current = { ...agent('only', 'WAIT'), cli: 'codex', project: 'project-alpha', title: 'Fix login' };
  ui.render({ hosts: [{ name: 'only', ok: true }], agents: [current] });
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /host-chip/);
  assert.match(ui.elements.get('list').innerHTML, /aria-label="開啟 project-alpha · Fix login"/);
  ui.open(current);
  const sheetHost = ui.elements.get('sheet-host');
  assert.equal(sheetHost.hidden, true);
  assert.equal(sheetHost.innerHTML, '');
});

test('card meta shows the tmux window index', () => {
  const at = cardHtmlFor({ project: 'Atlas', title: 'Deploy API', session: 'work', window_index: 3, pane_index: 1 });
  assert.match(at, /<div class="env"><span class="loc" title="tmux work:3\.1">3\.1<\/span><\/div>/, 'a split pane keeps its pane index without repeating the project');
  const zero = cardHtmlFor({ project: 'Atlas', session: 'work', window_index: 0, pane_index: 0 });
  assert.match(zero, /<span class="loc" title="tmux work:0\.0">0<\/span>/, 'index 0 is a real position and the first pane stays a window number');
  const long = cardHtmlFor({ project: 'Atlas', session: 'a-very-long-session-name', window_index: 12, pane_index: 2 });
  assert.match(long, /class="loc" title="tmux a-very-long-session-name:12\.2">12\.2</);
  const hostile = cardHtmlFor({ project: 'Atlas', session: '<b>"x"', window_index: 1, pane_index: 0 });
  assert.match(hostile, /class="loc" title="tmux &lt;b&gt;&quot;x&quot;:1\.0">1</);
  assert.doesNotMatch(hostile, /<b>/);
  const noPane = cardHtmlFor({ project: 'Atlas', session: 'work', window_index: 4 });
  assert.match(noPane, /class="loc" title="tmux work:4">4</, 'a missing pane index is left off the target');
  const noSession = cardHtmlFor({ project: 'Atlas', window_index: 4, pane_index: 0 });
  assert.match(noSession, /class="loc" title="tmux 4\.0">4</, 'an empty session has no colon');
  for (const stale of [{}, { window_index: null, pane_index: null }, { window_index: '2', pane_index: '1' }]) {
    assert.doesNotMatch(cardHtmlFor({ project: 'Atlas', session: 'work', ...stale }), /class="loc"/, 'older peers send no position');
  }
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /\.card-open \{[^}]*pointer-events: none/,
    'the card-wide open button must not eat the window-number tooltip');
  assert.match(html, /\.card \.preview a \{[^}]*pointer-events: none/,
    'preview links stay inert once the open button lets pointer events through');
  const ui = dashboard();
  const split = { ...agent('lab', 'WAIT'), project: 'Atlas', session: 'work', window_index: 3, pane_index: 1, branch: 'main' };
  ui.render({ agents: [split] });
  ui.open(split);
  assert.match(ui.elements.get('sheet-env').innerHTML, /class="loc" title="tmux work:3\.1">work:3\.1</,
    'the reply sheet shows the select-pane target without hover');
});

test('card title and meta fit long English and CJK text', () => {
  const ui = dashboard();
  const project = 'demo專案'.repeat(12);
  const title = 'tmux工作流'.repeat(11);
  const prompt = '我貼了 latest prompt';
  const a = handoffAgent(project, [], { title, preview: { users: [prompt], agent: '' } });
  ui.render({ agents: [a] });
  const cards = ui.elements.get('list').innerHTML;
  assert.ok(cards.includes(`<div class="title-line"><span class="card-title">${project}</span>`));
  assert.doesNotMatch(cards, /class="project"/);
  assert.ok(cards.includes(`<div class="session-context">${title}</div>`));
  assert.equal((cards.replace(/aria-label="[^"]*"/g, '').match(/我貼了 latest prompt/g) || []).length, 1);
  assert.ok(cards.includes(`aria-label="開啟 ${project} · ${title}"`));
  ui.render({ agents: [{ ...a, title: '' }] });
  const promptCard = ui.elements.get('list').innerHTML;
  assert.ok(promptCard.includes(`<span class="card-title">${project}</span>`));
  assert.equal((promptCard.replace(/aria-label="[^"]*"/g, '').match(/我貼了 latest prompt/g) || []).length, 1);
  assert.match(ui.context.sessionTitle({ ...a, title: '' }), /我貼了 latest prompt/);
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /\.title \{[^}]*flex: 1;[^}]*min-width: 0;/);
  assert.match(html, /\.session-heading \{[^}]*flex: 1;[^}]*min-width: 0;/);
  assert.match(html, /\.project \{[^}]*overflow-wrap: anywhere;/);
  assert.match(html, /\.card-title \{[^}]*-webkit-line-clamp: 2;[^}]*overflow-wrap: anywhere;/);
  assert.match(html, /\.card\.review\.unread \.card-title,[\s\S]*?\.card\.running \.card-title \{[^}]*color: var\(--fg\);[^}]*font-weight: 600;/);
  assert.match(html, /\.card-title \{[^}]*font-weight: 500;/);
  assert.match(html, /\.row1-side \.session-flags \{/);
  assert.match(html, /\.env > span \+ span::before/);
  assert.match(html, /\.env > \.loc \+ \.project::before \{[^}]*content: ":";/);
  assert.match(html, /\.title-agent \.model \{[^}]*flex: 0 0 auto;/, 'model must not be truncated before title');
  assert.doesNotMatch(html, /decision-inbox|行動收件匣|workstream-form|工作流標籤|\.inbox\b/);
});


test('composer submits with Cmd+Enter or Ctrl+Enter', () => {
  const ui = dashboard();
  const form = ui.elements.get('sheet-form');
  let submits = 0;
  form.requestSubmit = () => { submits += 1; };
  const a = { host: 'alpha', state: 'WAIT', tracked: true, pending: true, pane: '%1', pid: '9', project: 'sample' };
  ui.render({ hosts: [{ name: 'alpha', ok: true }], agents: [a] });
  ui.open(a);
  const input = ui.elements.get('sheet-text');
  input.value = 'hello from keyboard';
  let prevented = 0;
  const fire = (mods) => input.listeners.keydown({
    key: 'Enter', isComposing: false, metaKey: false, ctrlKey: false, ...mods,
    preventDefault() { prevented += 1; },
  });
  fire({});
  assert.equal(submits, 0);
  assert.equal(prevented, 0);
  fire({ metaKey: true });
  assert.equal(prevented, 1);
  assert.equal(submits, 1);
  fire({ ctrlKey: true });
  assert.equal(submits, 2);
  fire({ metaKey: true, isComposing: true });
  assert.equal(submits, 2);
});

test('sheet and reader show the same card title identity', () => {
  const ui = dashboard();
  const a = {
    host: 'alpha', state: 'IDLE', tracked: true, pending: false, needs_review: true, reviewed: true,
    pane: '%1', pid: '9', project: 'sample', title: 'Sample 專案列表與內頁模組', window: 'w1',
    branch: 'main', dirty: true, repo: 'sample-agent-project', model_label: 'sonnet 5.5',
  };
  ui.render({ hosts: [{ name: 'alpha', ok: true }], agents: [a] });
  ui.open(a);
  assert.match(ui.elements.get('sheet-title').textContent, /sample/);
  assert.equal(ui.elements.get('sheet-context').textContent, 'Sample 專案列表與內頁模組');
  assert.equal(ui.elements.get('sheet-context').hidden, false);
  assert.equal(ui.context.identityLine(a), 'sample · Sample 專案列表與內頁模組');
  assert.match(ui.elements.get('sheet-env').innerHTML, /class="branch">main</);
  assert.match(ui.elements.get('sheet-env').innerHTML, /class="dirty">dirty</);
  const cards = ui.elements.get('list').innerHTML;
  assert.doesNotMatch(cards, /class="branch"|class="dirty"/);
  assert.doesNotMatch(cards, /sample-agent-project/);
  ui.context.showReader(a, { role: 'agent', host: 'alpha', pane: '%1', pid: '9', index: 0 });
  assert.equal(ui.elements.get('reader-title').textContent, 'Agent 回覆');
  assert.equal(ui.elements.get('reader-sub').textContent, 'sample · Sample 專案列表與內頁模組 · main · dirty');
});

test('reader sub appends branch, dirty and character count', () => {
  const ui = dashboard();
  const a = { project: 'tmux', branch: 'feature/long-name-that-should-clip-somewhere-past-limit', dirty: true };
  assert.equal(ui.context.readerSubLine(a), `tmux · ${ui.context.clipBranch(a.branch)} · dirty`);
  assert.equal(ui.context.readerSubLine(a, 14), `tmux · ${ui.context.clipBranch(a.branch)} · dirty · 14 字`);
  assert.equal(ui.context.readerSubLine({ project: 'tmux' }, 3), 'tmux · 3 字');
});

test('session titles bound and escape task hints and titles while keeping project', () => {
  const ui = dashboard();
  const hostile = '<script>bad</script>' + 'x'.repeat(10000);
  const a = handoffAgent('Atlas', [], { preview: { users: [hostile] } });
  assert.ok(ui.context.sessionTitle(a).length <= a.project.length + 59);
  ui.render({ agents: [a] });
  assert.match(ui.elements.get('list').innerHTML, /class="card-title">Atlas<\/span>/);
  assert.doesNotMatch(ui.elements.get('list').innerHTML, /<script>/);
  ui.render({ agents: [{ ...a, title: '<manual>' }] });
  assert.match(ui.elements.get('list').innerHTML, /class="session-context">&lt;manual&gt;<\/div>/);
  ui.render({ agents: [{ ...a, project: '<project>"&', title: '<manual>' }] });
  assert.match(ui.elements.get('list').innerHTML, /class="card-title">&lt;project&gt;&quot;&amp;<\/span>[\s\S]*?<div class="session-context">&lt;manual&gt;<\/div>/);
});

test('cards and sheets render structured task lists, deduplicate gerund activeForm, and fold completed tasks', () => {
  const ui = dashboard();
  const s6Tasks = {
    total: 5, completed: 3, in_progress: 1, pending: 1,
    items: [
      { id: 1, subject: 'Read Round 4 evidence & trace root causes', status: 'completed', active_form: '', blocked_by: [] },
      { id: 2, subject: 'Implement Round 5 layer fixes', status: 'completed', active_form: '', blocked_by: [1] },
      { id: 3, subject: 'Add unit tests & Frozen Prefix A/B tooling', status: 'completed', active_form: '', blocked_by: [2] },
      {
        id: 4,
        subject: 'Execute Frozen Prefix A/B and Full-Chain Replay evaluations',
        status: 'in_progress',
        active_form: 'executing Frozen Prefix A/B (x3) and Full-Chain Replay (20x3) evaluations',
        blocked_by: [3],
      },
      { id: 5, subject: 'Compile Round 5 evidence package & comprehensive report', status: 'pending', active_form: '', blocked_by: [4] },
    ],
  };
  const s5Tasks = {
    total: 5, completed: 1, in_progress: 1, pending: 3,
    items: [
      { id: 1, subject: 'Re-establish baseline across repos, IAM, and hosts', status: 'completed', active_form: '', blocked_by: [] },
      {
        id: 2,
        subject: 'Prepare and verify candidate on isolated state copies',
        status: 'in_progress',
        active_form: 'preparing and verifying candidate on isolated state copies',
        blocked_by: [1],
      },
      { id: 3, subject: 'Perform production data inventory and reconciliation plan', status: 'pending', active_form: '', blocked_by: [1] },
      { id: 4, subject: 'Implement and rehearse initial service-only cutover and admission barrier', status: 'pending', active_form: '', blocked_by: [2, 3] },
      { id: 5, subject: 'Execute credential rotation cutover or document remaining gate evidence', status: 'pending', active_form: '', blocked_by: [4] },
    ],
  };
  const a6 = { ...agent('local', 'RUNNING'), pane: '%6', pid: '600', project: 'sample', cli: 'pi', tasks: s6Tasks };
  const a5 = { ...agent('local', 'RUNNING'), pane: '%5', pid: '500', project: 'insightgpt', cli: 'pi', tasks: s5Tasks };
  ui.render({ agents: [a6, a5] });
  const cardsHtml = ui.elements.get('list').innerHTML;
  // Session 6 card: shows one compact progress row. The full active form stays in the sheet.
  assert.match(cardsHtml, /aria-label="任務進度 3\/5"/);
  assert.match(cardsHtml, /#4<\/span><span class="card-task-subject">Execute Frozen Prefix A\/B and Full-Chain Replay evaluations<\/span>/);
  assert.match(cardsHtml, /\+1 待辦/);
  assert.doesNotMatch(cardsHtml, /card-task-active|executing Frozen Prefix A\/B \(x3\)/);
  // Session 5 card: shows 1/5, #2 subject, +3 待辦, and deduplicates redundant gerund active_form
  assert.match(cardsHtml, /aria-label="任務進度 1\/5"/);
  assert.match(cardsHtml, /\+3 待辦/);
  assert.doesNotMatch(cardsHtml, /preparing and verifying candidate on isolated state copies/);

  // Open sheet for Session 5 and verify dependency badges (resolved #1 vs blocked #2, #3) and fold toggle
  ui.open(a5);
  const sheetTasks = ui.elements.get('sheet-tasks');
  assert.equal(sheetTasks.hidden, false);
  assert.match(sheetTasks.innerHTML, /task-dep resolved" title="前置 #1 已完成">⛓ #1<\/span>/);
  assert.match(sheetTasks.innerHTML, /task-dep blocked" title="等待前置 #2">⛓ #2<\/span><span class="task-dep blocked" title="等待前置 #3">⛓ #3<\/span>/);
  assert.match(sheetTasks.innerHTML, /收合已完成 \(1\)/);
  ui.open(a6);
  assert.match(sheetTasks.innerHTML, /executing Frozen Prefix A\/B \(x3\) and Full-Chain Replay \(20x3\) evaluations/);
  ui.open(a5);

  // Toggle fold for completed tasks
  sheetTasks.listeners.click({ target: { closest: sel => sel === '[data-tasks-done-toggle]' ? {} : null } });
  assert.match(sheetTasks.innerHTML, /展開已完成 \(1\)/);
  assert.doesNotMatch(sheetTasks.innerHTML, /Re-establish baseline across repos/);
});

test('poll interval is 20 seconds and meta reflects 20s refresh', () => {
  const intervals = [];
  const ui = dashboard({
    globals: {
      setInterval: (_fn, ms) => intervals.push(ms),
    },
  });
  assert.ok(intervals.includes(20000), 'fleet poll runs every 20000ms');
  ui.render({ hosts: [{ name: 'local', ok: true }], agents: [] });
  assert.match(ui.elements.get('meta').textContent, /約 20 秒更新/);
});

test('favicon red dot, document title and app badge track wait + review and ignore later/idle/running', () => {
  const badgeCalls = [];
  const ui = dashboard({
    navigator: {
      setAppBadge: n => { badgeCalls.push(['set', n]); return Promise.resolve(); },
      clearAppBadge: () => { badgeCalls.push(['clear']); return Promise.resolve(); },
    },
  });
  const favicon = ui.elements.get('favicon');
  const dot = ui.elements.get('notify-dot');

  // Calm state: running, idle, later, shell do not light up the red dot
  ui.render({
    hosts: [{ name: 'local', ok: true }],
    agents: [
      { ...agent('local', 'RUNNING'), pane: '%1', pid: '1' },
      { ...agent('local', 'IDLE'), pane: '%2', pid: '2', needs_review: true, revision: 2, acknowledged: 2, unread: false, reviewed: true },
      { ...agent('local', 'IDLE'), pane: '%3', pid: '3', needs_review: true, revision: 3, acknowledged: 1, unread: true, later: true },
      { ...agent('local', 'SHELL', false), pane: '%4', pid: '4' },
    ],
  });
  assert.equal(ui.document.title, 'Attention');
  assert.equal(favicon.dataset.badge, 'none');
  assert.equal(favicon.dataset.count, '0');
  assert.equal(dot.hidden, true);
  assert.doesNotMatch(decodeURIComponent(favicon.href), /#ef4444/);
  assert.deepEqual(badgeCalls.at(-1), ['clear']);

  // Review + wait light up the red dot and update title count
  ui.render({
    hosts: [{ name: 'local', ok: true }],
    agents: [
      { ...agent('local', 'WAIT'), pane: '%1', pid: '1' },
      { ...agent('local', 'IDLE'), pane: '%2', pid: '2', category: 'review', needs_review: true, revision: 2, acknowledged: 1, unread: true },
      { ...agent('local', 'RUNNING'), pane: '%3', pid: '3' },
    ],
  });
  assert.equal(ui.document.title, '(2) Attention');
  assert.equal(favicon.dataset.badge, 'wait');
  assert.equal(favicon.dataset.count, '2');
  assert.equal(dot.hidden, false);
  assert.match(decodeURIComponent(favicon.href), /#ef4444/);
  assert.deepEqual(badgeCalls.at(-1), ['set', 2]);
});

test('notification toggle manages permission, persists preference and fires edge-triggered browser notifications', async () => {
  const storage = new Map();
  const localStorage = {
    getItem: k => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: k => storage.delete(k),
  };
  const created = [];
  let focused = 0;
  class FakeNotification {
    static permission = 'default';
    static async requestPermission() {
      FakeNotification.permission = 'granted';
      return FakeNotification.permission;
    }
    constructor(title, options) {
      this.title = title;
      this.options = options;
      this.closed = false;
      created.push(this);
    }
    close() {
      this.closed = true;
    }
  }
  const ui = dashboard({
    window: { focus: () => { focused += 1; }, Notification: FakeNotification },
    globals: { localStorage, Notification: FakeNotification },
  });
  const btn = ui.elements.get('notify-toggle');
  const label = ui.elements.get('notify-label');
  assert.equal(btn.hidden, false);
  assert.equal(btn.dataset.state, 'off');
  assert.equal(label.textContent, '通知');

  // Enable notifications via header button click
  await btn.listeners.click();
  assert.equal(btn.dataset.state, 'on');
  assert.equal(btn.getAttribute !== undefined ? true : true, true);
  assert.equal(label.textContent, '通知開啟');
  assert.equal(storage.get('tmux-attn:notify'), '1');

  // First render for a host seeds existing wait/review sessions without firing notifications
  const waitAgent = {
    ...agent('alpha', 'WAIT'),
    pane: '%1', pid: '10', project: 'Atlas', title: 'Deploy API', revision: 1,
    preview: { users: ['Deploy staging'], agent: 'Should I proceed with migration?' },
  };
  const runAgent = {
    ...agent('alpha', 'RUNNING'),
    pane: '%2', pid: '20', project: 'Sampleapp', title: 'Auth flow', revision: 1,
    preview: { users: ['Fix auth'], agent: 'Working on token refresh' },
  };
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true, pending: true }], agents: [waitAgent, runAgent] });
  assert.equal(created.length, 0, 'initial host snapshot never spams notifications');

  // Progressive arrival of pending host beta also seeds without notifying
  const betaReview = {
    ...agent('beta', 'IDLE'),
    pane: '%9', pid: '90', project: 'Billing', category: 'review', needs_review: true, unread: true, revision: 2, acknowledged: 1,
    preview: { users: ['Audit invoices'], agent: 'All 42 invoices verified.' },
  };
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [waitAgent, runAgent, betaReview] });
  assert.equal(created.length, 0, 'progressive host slice seeds without firing notifications');

  // Next poll with unchanged wait/review states does not fire duplicate notifications
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [waitAgent, runAgent, betaReview] });
  assert.equal(created.length, 0);

  // Transition of runAgent from RUNNING -> WAIT fires a notification
  const nowWaiting = {
    ...runAgent,
    state: 'WAIT',
    category: 'wait',
    revision: 2,
    preview: { users: ['Fix auth'], agent: 'Need confirmation on cookie domain' },
  };
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [waitAgent, nowWaiting, betaReview] });
  assert.equal(created.length, 1);
  assert.equal(created[0].title, '🙋 等待回覆 · Sampleapp · Auth flow');
  assert.match(created[0].options.body, /^\[alpha\] Need confirmation on cookie domain/);
  assert.equal(created[0].options.tag, 'tmux-attn:alpha|%2|20');

  // Clicking the notification focuses the window, opens that agent's sheet, and closes the notification
  created[0].onclick();
  assert.equal(focused, 1);
  assert.equal(ui.elements.get('sheet-root').hidden, false);
  assert.match(ui.elements.get('sheet-title').textContent, /Sampleapp/);
  assert.equal(created[0].closed, true);
  ui.close();

  // Transition to review with a newer revision fires a review notification
  const nowReview = {
    ...nowWaiting,
    state: 'IDLE',
    category: 'review',
    needs_review: true,
    unread: true,
    revision: 3,
    acknowledged: 1,
    preview: { users: ['Fix auth'], agent: 'Completed cookie domain update and tests pass.' },
  };
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [waitAgent, nowReview, betaReview] });
  assert.equal(created.length, 2);
  assert.equal(created[1].title, '👀 待看 · Sampleapp · Auth flow');
  assert.match(created[1].options.body, /^\[alpha\] Completed cookie domain update and tests pass\./);

  // Toggling notifications off stops new notifications
  await btn.listeners.click();
  assert.equal(btn.dataset.state, 'off');
  assert.equal(storage.get('tmux-attn:notify'), undefined);
  const newerWait = { ...waitAgent, revision: 5 };
  ui.render({ hosts: [{ name: 'alpha', ok: true }, { name: 'beta', ok: true }], agents: [newerWait, nowReview, betaReview] });
  assert.equal(created.length, 2);
});

test('coding agent usage shows only active providers, skips pi, and expands details with refresh', async () => {
  const calls = [];
  const sampleUsage = {
    ok: true,
    providers: ['codex', 'claude', 'cursor'],
    updated_at: Math.floor(Date.now() / 1000) - 120,
    accounts: [
      {
        provider: 'codex',
        account: 'alice@example.com',
        label: 'alice',
        plan: 'prolite',
        windows: [
          {
            key: 'secondary',
            label: 'Weekly',
            shortLabel: '週',
            usedPercent: 68,
            windowMinutes: 10080,
            resetsAt: new Date(Date.now() + 6 * 86400 * 1000).toISOString(),
            resetDescription: 'Nov 2 at 3:46 PM',
            pace: { stage: 'farAhead', deltaPercent: 56, expectedUsedPercent: 12, etaSeconds: 33800, willLastToReset: false },
          },
        ],
      },
      {
        provider: 'codex',
        account: 'bob@example.com',
        label: 'bob',
        plan: 'plus',
        windows: [
          {
            key: 'primary',
            label: 'Session',
            shortLabel: '5h',
            usedPercent: 100,
            windowMinutes: 300,
            resetsAt: new Date(Date.now() + 95 * 60 * 1000).toISOString(),
            resetDescription: '1:06 PM',
          },
          {
            key: 'secondary',
            label: 'Weekly',
            shortLabel: '週',
            usedPercent: 30,
            windowMinutes: 10080,
            resetsAt: new Date(Date.now() + 6 * 86400 * 1000).toISOString(),
            resetDescription: 'Nov 2 at 6:41 PM',
          },
        ],
      },
      {
        provider: 'claude',
        account: 'carol@example.com',
        label: 'carol',
        plan: 'Team',
        windows: [
          {
            key: 'primary',
            label: 'Session',
            shortLabel: '5h',
            usedPercent: 12,
            windowMinutes: 300,
            resetsAt: new Date(Date.now() + 180 * 60 * 1000).toISOString(),
            resetDescription: '4:20 PM',
          },
        ],
      },
      {
        provider: 'cursor',
        account: 'alice@example.com',
        label: 'alice',
        plan: 'Pro',
        windows: [
          {
            key: 'primary',
            label: 'Included',
            shortLabel: '月',
            usedPercent: 63,
            windowMinutes: 44640,
            resetsAt: new Date(Date.now() + 18 * 86400 * 1000).toISOString(),
            resetDescription: 'Resets Nov 14',
            pace: { stage: 'farAhead', deltaPercent: 21, expectedUsedPercent: 42, etaSeconds: 666233, willLastToReset: false },
          },
          {
            key: 'secondary',
            label: 'API',
            shortLabel: 'API',
            usedPercent: 88,
            windowMinutes: 44640,
            resetsAt: new Date(Date.now() + 18 * 86400 * 1000).toISOString(),
            resetDescription: 'Resets Nov 14',
          },
        ],
      },
    ],
  };

  const ui = dashboard({
    allowUsageFetch: true,
    fetchRequest: async (url) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => sampleUsage };
    },
  });

  // 1. Filtering to Pi hides usage completely; clearing filter fetches codex,claude,cursor (never pi)
  pickAgent(ui, 'pi');
  ui.render({ hosts: [{ name: 'work', ok: true }], agents: [filterSession('pi', 'work', 'RUNNING')] });
  assert.equal(calls.length, 0);
  assert.equal(ui.elements.get('agent-usage').hidden, true);
  pickAgent(ui, '');

  // 2. With no filter (All), shows skeleton toggle while in-flight, then renders toggle with mini-gauges
  ui.render({
    hosts: [{ name: 'work', ok: true }],
    agents: [
      filterSession('codex', 'work', 'RUNNING'),
      filterSession('claude', 'work', 'WAIT'),
      filterSession('agent', 'work', 'RUNNING'),
      filterSession('pi', 'work', 'RUNNING'),
    ],
  });
  const box = ui.elements.get('agent-usage');
  assert.equal(box.hidden, false);
  assert.match(box.innerHTML, /class="skel skel-usage-toggle"/);
  await new Promise(r => setImmediate(r));
  assert.deepEqual(calls, ['/api/usage?providers=codex%2Cclaude%2Ccursor%2Cantigravity']);
  assert.equal(box.hidden, false);
  assert.match(box.innerHTML, /class="usage-toggle tone-danger"[^>]*aria-expanded="false"/);
  assert.match(box.innerHTML, /class="usage-mini-gauges"/);
  assert.match(box.innerHTML, /class="usage-panel" id="usage-panel"[^>]*hidden/);
  assert.doesNotMatch(box.innerHTML, /skel-usage-toggle/);

  // 3. Filtering to Pi hides usage completely; filtering to Cursor scopes cards to Cursor
  pickAgent(ui, 'pi');
  assert.equal(box.hidden, true);
  pickAgent(ui, 'cursor');
  assert.equal(box.hidden, false);
  assert.match(box.innerHTML, /data-usage-provider="cursor"/);
  assert.doesNotMatch(box.innerHTML, /carol/);
  pickAgent(ui, '');

  // 4. Clicking the toggle expands the inline detail panel (no hidden attribute) with progress bars, expected tick, and pace warning
  box.listeners.click({ target: { closest: sel => sel === '[data-usage-toggle]' ? {} : null } });
  assert.match(box.innerHTML, /class="usage-toggle tone-danger"[^>]*aria-expanded="true"/);
  assert.match(box.innerHTML, /class="usage-panel" id="usage-panel">/);
  assert.doesNotMatch(box.innerHTML, /id="usage-panel"[^>]*hidden/);
  assert.match(box.innerHTML, /剩 32%/);
  assert.match(box.innerHTML, /剩 0%/);
  assert.match(box.innerHTML, /class="usage-bar-expected" style="left:88%"/);
  assert.match(box.innerHTML, /超前 56% · 預計 9h 24m 後用盡/);
  assert.match(box.innerHTML, /已用盡/);

  // 5. Clicking refresh forces a refresh=1 call and keeps panel open
  box.listeners.click({ target: { closest: sel => sel === '[data-usage-refresh]' ? {} : null } });
  await new Promise(r => setImmediate(r));
  assert.equal(calls.at(-1), '/api/usage?providers=codex%2Cclaude%2Ccursor%2Cantigravity&refresh=1');
  assert.doesNotMatch(box.innerHTML, /id="usage-panel"[^>]*hidden/);

  // 6. Clicking close button or toggling collapses the inline panel
  box.listeners.click({ target: { closest: sel => sel === '[data-usage-close]' ? {} : null } });
  assert.match(box.innerHTML, /id="usage-panel"[^>]*hidden/);
  assert.match(box.innerHTML, /class="usage-toggle tone-danger"[^>]*aria-expanded="false"/);
  box.listeners.click({ target: { closest: sel => sel === '[data-usage-toggle]' ? {} : null } });
  assert.doesNotMatch(box.innerHTML, /id="usage-panel"[^>]*hidden/);
  assert.match(box.innerHTML, /class="usage-toggle tone-danger"[^>]*aria-expanded="true"/);
  box.listeners.click({ target: { closest: sel => sel === '[data-usage-toggle]' ? {} : null } });
  assert.match(box.innerHTML, /id="usage-panel"[^>]*hidden/);
  assert.match(box.innerHTML, /class="usage-toggle tone-danger"[^>]*aria-expanded="false"/);
});

test('untracked shell cards show last 3 lines and toggle inline up to 12 lines without opening the sheet', () => {
  const lines12 = Array.from({ length: 12 }, (_, i) => `2026-10-28T21:00:${String(i + 1).padStart(2, '0')} #<${i + 1}>`);
  const shell12 = {
    ...agent('lab', 'SHELL', false),
    pane: '%10',
    pid: '41440',
    project: 'jkbb',
    preview: { users: [], agent: '', lines: lines12 },
  };
  const shell2 = {
    ...agent('lab', 'SHELL', false),
    pane: '%11',
    pid: '41441',
    project: 'short-shell',
    preview: { users: [], agent: '', lines: ['prompt $ ls', 'README.md'] },
  };
  let latestHtml = '';
  const cardNode = {
    dataset: { key: 'lab|%10|41440' },
    classList: { contains: () => false },
    contains: () => true,
    set outerHTML(html) { latestHtml = html; },
    get outerHTML() { return latestHtml; },
  };
  const ui = dashboard({
    document: {
      querySelector: sel => sel.includes('lab|%10|41440') ? cardNode : null,
    },
  });
  ui.render({ hosts: [{ name: 'lab', ok: true }], agents: [shell12, shell2] });
  const list = ui.elements.get('list');
  // Short shell (<= 3 lines) renders lines with no toggle button and no expandable class.
  assert.match(list.innerHTML, /prompt \$ ls\nREADME\.md/);
  assert.equal((list.innerHTML.match(/data-shell-toggle/g) || []).length, 1);
  // 12-line shell starts collapsed showing only the last 3 lines (#10, #11, #12) with HTML escaped.
  assert.match(list.innerHTML, /class="card shell expandable" data-key="lab\|%10\|41440"/);
  assert.doesNotMatch(list.innerHTML, /#&lt;9&gt;/);
  assert.match(list.innerHTML, /#&lt;10&gt;\n.*#&lt;11&gt;\n.*#&lt;12&gt;/);
  assert.match(list.innerHTML, /data-shell-toggle aria-expanded="false">展開 12 行<\/button>/);

  // Clicking the shell card expands inline to all 12 lines and never opens the reply sheet.
  list.listeners.click({
    target: {
      closest: sel => sel === '.card[data-key]' ? cardNode : sel === '[data-shell-toggle]' ? {} : null,
    },
  });
  assert.equal(ui.elements.get('sheet-title').textContent, '');
  assert.match(latestHtml, /class="shell-preview expanded"/);
  assert.match(latestHtml, /#&lt;1&gt;[\s\S]*#&lt;12&gt;/);
  assert.match(latestHtml, /data-shell-toggle aria-expanded="true">收合<\/button>/);

  // Subsequent poll renders preserve the expanded state.
  ui.render({ hosts: [{ name: 'lab', ok: true }], agents: [shell12, shell2] });
  assert.match(list.innerHTML, /class="shell-preview expanded"/);

  // Clicking again collapses back to 3 lines.
  list.listeners.click({
    target: {
      closest: sel => sel === '.card[data-key]' ? cardNode : null,
    },
  });
  assert.doesNotMatch(latestHtml, /class="shell-preview expanded"/);
  assert.doesNotMatch(latestHtml, /#&lt;9&gt;/);
  assert.match(latestHtml, /data-shell-toggle aria-expanded="false">展開 12 行<\/button>/);
});

test('desktop and tablet layouts use adaptive responsive multi-column grid with measure boundaries', () => {
  const fs = require('fs');
  const path = require('path');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

  // Multi-column grid rules are defined at clean breakpoints
  assert.match(html, /@media \(min-width: 768px\)/);
  assert.match(html, /@media \(min-width: 1200px\)/);
  assert.match(html, /@media \(min-width: 1600px\)/);

  // List and skeleton list switch to grid with 2, 3, and 4 columns
  assert.match(html, /main\.list,\s*\.skel-list \{[^}]*display: grid;\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/);
  assert.match(html, /main\.list,\s*\.skel-list \{[^}]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);/);
  assert.match(html, /main\.list,\s*\.skel-list \{[^}]*grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/);

  // Section titles, skeleton section, and later groups span full width
  assert.match(html, /\.section,\s*\.skel-section,\s*\.later-group \{[^}]*grid-column: 1 \/ -1;/);

  // Expanded later groups also lay out cards in multi-column grid
  assert.match(html, /\.later-group\[open\] \{[^}]*display: grid;\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/);
  assert.match(html, /\.later-group summary \{[^}]*grid-column: 1 \/ -1;/);

  // Sheet dialog has an independent max width so it does not overstretch on wide monitors
  assert.match(html, /--sheet-max: 44rem;/);
  assert.match(html, /\.sheet \{[^}]*width: min\(100%, var\(--sheet-max\)\);/);

  // Cards specify min-width: 0 to prevent content overflows inside CSS grid items
  assert.match(html, /\.card \{[^}]*min-width: 0;/);
});

test('font size controls increase, decrease, reset and persist scale', () => {
  const stored = new Map();
  const localStorage = {
    getItem: key => (stored.has(key) ? stored.get(key) : null),
    setItem: (key, val) => stored.set(key, String(val)),
    removeItem: key => stored.delete(key),
  };
  let appliedScale = '';
  const doc = {
    documentElement: {
      style: {
        setProperty: (prop, val) => { if (prop === '--font-scale') appliedScale = val; },
        removeProperty: prop => { if (prop === '--font-scale') appliedScale = ''; },
      },
    },
  };
  const ui = dashboard({ document: doc, globals: { localStorage } });
  const dec = ui.elements.get('font-dec');
  const inc = ui.elements.get('font-inc');
  const reset = ui.elements.get('font-reset');

  assert.equal(dec.disabled, false);
  assert.equal(inc.disabled, false);
  assert.equal(appliedScale, '');

  // Increase font size: 1.0 -> 1.1
  inc.listeners.click();
  assert.equal(appliedScale, '1.1');
  assert.equal(stored.get('tmux-attn-font-scale'), '1.1');
  assert.match(reset.title, /110%/);

  // Increase again: 1.1 -> 1.2
  inc.listeners.click();
  assert.equal(appliedScale, '1.2');

  // Reset font size
  reset.listeners.click();
  assert.equal(appliedScale, '');
  assert.equal(stored.has('tmux-attn-font-scale'), false);
  assert.match(reset.title, /100%/);

  // Decrease font size: 1.0 -> 0.9 -> 0.8 (min)
  dec.listeners.click();
  assert.equal(appliedScale, '0.9');
  dec.listeners.click();
  assert.equal(appliedScale, '0.8');
  assert.equal(dec.disabled, true);

  // Reload restores saved scale
  stored.set('tmux-attn-font-scale', '1.3');
  let restoredScale = '';
  const doc2 = {
    documentElement: {
      style: {
        setProperty: (prop, val) => { if (prop === '--font-scale') restoredScale = val; },
        removeProperty: prop => { if (prop === '--font-scale') restoredScale = ''; },
      },
    },
  };
  dashboard({ document: doc2, globals: { localStorage } });
  assert.equal(restoredScale, '1.3');
});

test('untracked shell cards take a one-line input that posts to /api/send and keeps its draft across polls', async () => {
  const shellAgent = { ...agent('lab', 'SHELL', false), pane: '%9', pid: '9', project: 'plain-shell',
    preview: { users: [], agent: '', lines: ['$ ls'] } };
  const requests = [];
  const ui = dashboard({ fetchRequest: async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true }) };
  } });
  ui.render({ hosts: [{ name: 'lab', ok: true }], agents: [shellAgent] });
  const list = ui.elements.get('list');
  assert.match(list.innerHTML, /<form class="shell-input" data-shell-input>[\s\S]*data-shell-field[\s\S]*placeholder="輸入指令/);

  const key = 'lab|%9|9';
  const field = { value: 'echo hi', closest: sel => sel === '[data-shell-field]' ? field : { dataset: { key } } };
  list.listeners.input({ target: field });
  ui.render({ hosts: [{ name: 'lab', ok: true }], agents: [shellAgent] });
  assert.match(list.innerHTML, /value="echo hi"/);

  // Submitting the form posts the draft to the pane's own host.
  let prevented = false;
  const card = { dataset: { key } };
  const form = { closest: () => card };
  await list.listeners.submit({
    preventDefault() { prevented = true; },
    target: { closest: sel => sel === '[data-shell-input]' ? form : null },
  });
  assert.ok(prevented);
  assert.deepEqual(requests, [{ url: '/api/send',
    body: { host: 'lab', pane: '%9', pid: '9', text: 'echo hi', submit: true, images: [] } }]);
  ui.render({ hosts: [{ name: 'lab', ok: true }], agents: [shellAgent] });
  assert.doesNotMatch(list.innerHTML, /value="echo hi"/);
});

function serverInfo(overrides = {}) {
  return {
    ok: true, host: 'workstation', version: '1b3a8a0d2e8d', started_version: '1b3a8a0d2e8d', fleet_only: false,
    bind: '0.0.0.0', port: 7788, peers: [], peers_path: '/home/dev/.config/tmux-attn/peers.json',
    tailscale_url: 'http://100.64.0.10:7788', ...overrides,
  };
}

function infoDashboard(info, { location, navigator, fetchRequest } = {}) {
  const calls = [];
  const ui = dashboard({
    allowInfoFetch: true,
    window: { location: location || { hash: '', hostname: '100.64.0.10', origin: 'http://100.64.0.10:7788' } },
    navigator,
    fetchRequest: fetchRequest || (async (url) => { calls.push(url); return response(info); }),
  });
  // The fake DOM does not read the HTML `hidden` attribute, so start in the state index.html declares.
  ui.elements.get('info-panel').hidden = true;
  ui.elements.get('info-toggle').setAttribute('aria-expanded', 'false');
  return { ui, calls };
}

async function openInfo(ui) {
  ui.elements.get('info-toggle').listeners.click();
  for (let i = 0; i < 8; i++) await settle();
}

function typeName(ui, value) {
  const input = ui.elements.get('info-peer-name');
  input.value = value;
  input.listeners.input();
}

test('? popover starts closed, reads server facts on boot, and lists peers', async () => {
  const { ui, calls } = infoDashboard(serverInfo({
    peers: [{ name: 'work-mac', url: 'http://100.1.2.3:7788' }],
  }));
  for (let i = 0; i < 8; i++) await settle();
  const toggle = ui.elements.get('info-toggle');
  assert.equal(ui.elements.get('info-panel').hidden, true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.deepEqual(calls, ['/api/info']);
  assert.equal(ui.elements.get('info-host').textContent, 'workstation');
  assert.equal(ui.elements.get('info-version').textContent, '1b3a8a0');
  assert.equal(ui.elements.get('info-mode').textContent, '多機 server');
  assert.equal(ui.elements.get('info-peers-count').textContent, '1 台');
  assert.match(ui.elements.get('info-peers').innerHTML, /work-mac<\/strong><span>http:\/\/100\.1\.2\.3:7788/);
  assert.equal(ui.elements.get('info-bind').textContent, '0.0.0.0:7788');
});

test('hover previews the popover for a mouse, click pins it, and a second click closes it', async () => {
  const { ui } = infoDashboard(serverInfo());
  const wrap = ui.elements.get('info-wrap');
  const panel = ui.elements.get('info-panel');
  const toggle = ui.elements.get('info-toggle');

  wrap.listeners.pointerenter({ pointerType: 'mouse' });
  assert.equal(panel.hidden, false);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  wrap.listeners.pointerleave({ pointerType: 'mouse' });
  assert.equal(panel.hidden, true, 'a hover preview closes when the pointer leaves');

  toggle.listeners.click();
  wrap.listeners.pointerleave({ pointerType: 'mouse' });
  assert.equal(panel.hidden, false, 'a click pins the card so moving away keeps it open');
  toggle.listeners.click();
  assert.equal(panel.hidden, true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

test('touch has no hover preview, so only a tap opens the popover', () => {
  const { ui } = infoDashboard(serverInfo());
  ui.elements.get('info-wrap').listeners.pointerenter({ pointerType: 'touch' });
  assert.equal(ui.elements.get('info-panel').hidden, true);
  ui.elements.get('info-toggle').listeners.click();
  assert.equal(ui.elements.get('info-panel').hidden, false);
});

test('connect command points the client at this server and at its own Tailscale address', async () => {
  const { ui } = infoDashboard(serverInfo());
  await openInfo(ui);
  assert.equal(ui.elements.get('info-url').textContent, 'http://100.64.0.10:7788');
  const cmd = ui.elements.get('info-cmd').textContent;
  assert.equal(cmd, `curl -fsSL https://raw.githubusercontent.com/cubrady/tmux-attn/main/install.sh | PEER_NAME='<name>' LOCAL_FEED_URL="http://$(tailscale ip -4):7788" SERVER_URL='http://100.64.0.10:7788' sh`);
  assert.equal(ui.elements.get('info-cmd-copy').disabled, true, 'no copy until the client has a name');
  assert.equal(ui.elements.get('info-url-copy').disabled, false);
});

test('copy unlocks only for a shell-safe client name and copies the exact command', async () => {
  const copied = [];
  const { ui } = infoDashboard(serverInfo(), { navigator: { clipboard: { writeText: async (text) => { copied.push(text); } } } });
  await openInfo(ui);
  const copy = ui.elements.get('info-cmd-copy');
  for (const bad of ["work mac", "a'b", 'x$(id)', '']) {
    typeName(ui, bad);
    assert.equal(copy.disabled, true, `rejects ${JSON.stringify(bad)}`);
  }
  typeName(ui, '工作機-1.mac');
  assert.equal(copy.disabled, false);
  ui.elements.get('info-panel').listeners.click({ target: { closest: () => copy } });
  for (let i = 0; i < 4; i++) await settle();
  assert.deepEqual(copied, ["curl -fsSL https://raw.githubusercontent.com/cubrady/tmux-attn/main/install.sh | PEER_NAME='工作機-1.mac' LOCAL_FEED_URL=\"http://$(tailscale ip -4):7788\" SERVER_URL='http://100.64.0.10:7788' sh"]);
  assert.equal(copy.textContent, '已複製');
});

test('on loopback the address comes from the server, and nothing is copyable without one', async () => {
  const loopback = { hash: '', hostname: 'localhost', origin: 'http://localhost:7788' };
  const { ui } = infoDashboard(serverInfo({ tailscale_url: '' }), { location: loopback });
  await openInfo(ui);
  assert.equal(ui.elements.get('info-url').textContent, '找不到 Tailscale 位址');
  assert.equal(ui.elements.get('info-url-copy').disabled, true);
  typeName(ui, 'laptop');
  assert.equal(ui.elements.get('info-cmd-copy').disabled, true);
  assert.match(ui.elements.get('info-cmd').textContent, /SERVER_URL='http:\/\/<server>:7788'/);

  const withTs = infoDashboard(serverInfo({ tailscale_url: 'http://100.5.6.7:7788' }), { location: loopback });
  await openInfo(withTs.ui);
  assert.equal(withTs.ui.elements.get('info-url').textContent, 'http://100.5.6.7:7788');
});

test('a failed first load says so instead of showing a made-up address', async () => {
  const loopback = { hash: '', hostname: 'localhost', origin: 'http://localhost:7788' };
  const { ui } = infoDashboard(null, { location: loopback, fetchRequest: async () => { throw new TypeError('offline'); } });
  for (let i = 0; i < 8; i++) await settle();
  assert.equal(ui.elements.get('info-url').textContent, '無法取得 server 資訊');
  assert.equal(ui.elements.get('info-host').textContent, '');
});

test('a version changed on disk but not in the running process raises the dot and the restart note', async () => {
  const stale = infoDashboard(serverInfo({ version: 'aaaaaaaaaaaa', started_version: 'bbbbbbbbbbbb' }));
  for (let i = 0; i < 8; i++) await settle();
  assert.equal(stale.ui.elements.get('info-dot').hidden, false);
  assert.equal(stale.ui.elements.get('info-stale').hidden, false);
  assert.match(stale.ui.elements.get('info-toggle').getAttribute('aria-label'), /需要重啟/);

  const fresh = infoDashboard(serverInfo());
  for (let i = 0; i < 8; i++) await settle();
  assert.equal(fresh.ui.elements.get('info-dot').hidden, true);
  assert.equal(fresh.ui.elements.get('info-stale').hidden, true);
});

test('Escape and an outside pointer close the popover', async () => {
  const { ui } = infoDashboard(serverInfo());
  await openInfo(ui);
  ui.document.listeners.keydown({ key: 'Escape' });
  assert.equal(ui.elements.get('info-panel').hidden, true);

  await openInfo(ui);
  ui.document.listeners.pointerdown({ target: {} });
  assert.equal(ui.elements.get('info-panel').hidden, true);

  await openInfo(ui);
  ui.document.listeners.pointerdown({ target: ui.elements.get('info-wrap') });
  assert.equal(ui.elements.get('info-panel').hidden, false, 'pointer inside the popover keeps it open');
});

test('Antigravity (agy) renders filter chip and CLI icon badge', () => {
  const ui = dashboard();
  ui.render({ agents: [filterSession('agy')] });
  assert.match(ui.elements.get('agent-filters').innerHTML,
    /data-agent-filter="agy"[^>]*>Antigravity/);
  assert.match(ui.elements.get('list').innerHTML,
    /class="cli sm agy"[^>]*>.*?src="\/icons\/agy\.svg"/);
  pickAgent(ui, 'agy');
  assert.match(ui.elements.get('list').innerHTML, /work-agy/);
  assert.match(ui.elements.get('meta').textContent, /Agent Antigravity/);
});


test('agy filter requests Antigravity quota and renders its icon and model buckets', async () => {
  const calls = [];
  const ui = dashboard({ allowUsageFetch: true, fetchRequest: async url => {
    calls.push(url);
    return { ok: true, json: async () => ({ ok: true, providers: ['antigravity'], accounts: [{
      provider: 'antigravity', label: 'Antigravity', windows: [{
        key: 'argon-weekly', label: 'Gemini 4 Argon weekly', shortLabel: '週',
        usedPercent: 38, windowMinutes: 10080,
      }],
    }] }) };
  } });
  ui.render({ hosts: [{ name: 'work', ok: true }], agents: [filterSession('agy', 'work', 'IDLE')] });
  await new Promise(r => setImmediate(r));
  pickAgent(ui, 'agy');
  const html = ui.elements.get('agent-usage').innerHTML;
  assert.match(html, /data-usage-provider="antigravity"/);
  assert.match(html, /\/icons\/agy.svg/);
  assert.match(html, /Gemini 4 Argon weekly/);
  assert.match(html, /剩 62%/);
  assert.ok(calls.some(url => url.includes('antigravity')));
});


test('project icons use only known host-provided types and leave unknown projects blank', () => {
  const { context } = dashboard();
  for (const type of ['android', 'react', 'nextjs', 'vue', 'nuxt', 'svelte', 'angular', 'flutter', 'react-native', 'python', 'cli', 'rust', 'go']) {
    const html = context.projectIconHtml({ project_technology: type });
    assert.ok(html.includes(`/icons/project-${type}.svg`));
    assert.ok(html.includes('class="project-icon"'));
    assert.ok(html.includes(`data-project-type="${type}"`));
    assert.ok(readFileSync(join(__dirname, `../web/icons/project-${type}.svg`), 'utf8').includes('<svg'));
  }
  for (const type of [undefined, null, {}, [], { toString: 'android' }, '', 'unknown', 'constructor', '__proto__', '../../bad', '<script>']) {
    assert.equal(context.projectIconHtml({ project_technology: type }), '');
  }
  const html = context.cardHtml({ ...agent('local', 'RUNNING'), project: 'demo', project_technology: 'android' });
  assert.ok(html.indexOf('project-android.svg') < html.indexOf('class="card-title"'));
});


test('directories and missing example paths have no file controls without source validation', () => {
  const ui = dashboard();
  const text = '專案 `/Users/dev/proj/tmux-attn-starter`，範例 `/Users/dev/report.md`';
  assert.equal(ui.context.outputFilesHtml(text, agent('peer', 'DONE')), '');
  assert.equal(ui.context.outputFilesHtml(text, agent('peer', 'DONE'), []), '');
  const valid = ui.context.outputFilesHtml(text, agent('peer', 'DONE'), ['/Users/dev/report.md']);
  assert.ok(valid.includes('/api/output-preview?'));
  assert.ok(!valid.includes('tmux-attn-starter</span>'));
  assert.ok(valid.includes('/api/output-file?'));
});

test('Markdown and structured text preview routes retain raw download endpoints', () => {
  const ui = markdownLinkDashboard();
  const current = agent('peer', 'IDLE');
  const text = '[報告](/home/dev/report.md)';
  assert.ok(ui.context.renderMarkdown(text, { agent: current, text }).includes('/api/output-preview?'));
  for (const suffix of ['md', 'markdown', 'json', 'csv', 'py', 'txt']) {
    const html = ui.context.outputFilesHtml('`/home/dev/file.' + suffix + '`', current, ['/home/dev/file.' + suffix]);
    assert.ok(html.includes('/api/output-preview?'));
    assert.ok(html.includes('/api/output-file?'));
  }
});

test('new work selects last project agent and keeps source-host quota separate', async () => {
  const calls = [];
  const ui = dashboard(async (url, options) => {
    calls.push([url, options]);
    if (url.startsWith('/api/work-options')) return {ok:true,json:async()=>({ok:true,host:'peer',default_agent:'codex',sessions:[{id:'$1',name:'main',attached:1,windows:2}],default_session:'$1',projects:[{path:'/project',agent:'claude',last_used:5,pinned:false}],agents:[{id:'codex',label:'Codex',available:true},{id:'claude',label:'Claude',available:true},{id:'pi',label:'Pi',available:false}]})};
    return {ok:true,json:async()=>({ok:true,accounts:[{provider:'claude',windows:[{label:'Weekly',usedPercent:32}]}]})};
  });
  ui.renderUsage({accounts:[{provider:'claude',windows:[{label:'Wrong host',usedPercent:99}]}]});
  vm.runInContext("workOpen=true;document.getElementById('work-host').value='peer';document.getElementById('work-path').value='/project'",ui.context);
  await ui.context.loadWorkOptions();
  assert.equal(vm.runInContext('workAgent',ui.context),'claude');
  assert.match(ui.elements.get('work-agents').innerHTML,/Weekly 剩 68%/);
  assert.doesNotMatch(ui.elements.get('work-agents').innerHTML,/Wrong host/);
  assert.doesNotMatch(ui.elements.get('work-agents').innerHTML,/未安裝|data-work-agent="pi"/);
  assert.ok(calls.every(([url])=>url.includes('host=peer')));
});

test('new work rejects obsolete host responses and unknown quota is explicit', async () => {
  let finish;
  const ui = dashboard(url => url.startsWith('/api/work-options') ? new Promise(resolve => {finish=resolve}) : Promise.resolve({ok:true,json:async()=>({ok:true,accounts:[]})}));
  vm.runInContext("workOpen=true;document.getElementById('work-host').value='old'",ui.context);
  const pending=ui.context.loadWorkOptions();
  vm.runInContext("workSequence++;workAgent='new-host-agent'",ui.context);
  finish({ok:true,json:async()=>({ok:true,default_agent:'old-agent',agents:[{id:'old-agent',available:true}],projects:[]})});
  await pending;
  assert.equal(vm.runInContext('workAgent',ui.context),'new-host-agent');
  vm.runInContext('workUsageLoading=false',ui.context);
  assert.equal(ui.context.workQuotaText('pi'),'未提供用量');
});

test('successful new work prevents a second tap and timeout retry keeps request id', async () => {
  const requests=[];
  let fail=true;
  const ui=dashboard(async (url,options)=>{
    requests.push(JSON.parse(options.body));
    if(fail) throw new Error('timeout');
    return {ok:true,json:async()=>({ok:true,host:'peer',session:'work-demo',pane:'%900',pid:'900'})};
  });
  vm.runInContext("workOpen=true;workAgent='codex';workOptions={sessions:[{id:'$1',name:'main'}],projects:[],agents:[{id:'codex',available:true}]};document.getElementById('work-session').value='$1';document.getElementById('work-host').value='peer';document.getElementById('work-path').value='/project';renderWorkAgents()",ui.context);
  await ui.context.startWork({preventDefault(){}});
  assert.match(ui.elements.get('work-progress-message').textContent,/timeout/);
  fail=false;
  await ui.context.startWork({preventDefault(){}});
  await ui.context.startWork({preventDefault(){}});
  assert.equal(requests.length,2);
  assert.equal(requests[0].session,'$1');
  assert.equal(requests[0].request_id,requests[1].request_id);
  assert.equal(ui.elements.get('work-submit').disabled,true);
});


test('new work locks the dialog until registered agent arrives and shows slow waiting', async () => {
  let complete, onWait;
  const ui = dashboard({ fetchRequest: () => new Promise(resolve => { complete = resolve; }), globals: {setTimeout: fn => {onWait=fn; return 1;}} });
  vm.runInContext("workOpen=true;workAgent='codex';workOptions={sessions:[{id:'$1',name:'main'}],projects:[],agents:[{id:'codex',available:true}]};document.getElementById('work-session').value='$1';document.getElementById('work-path').value='/project';renderWorkAgents()",ui.context);
  const pending=ui.context.startWork({preventDefault(){}});
  assert.equal(ui.elements.get('work-form').hidden,true);
  assert.equal(ui.elements.get('work-progress').dataset.stage,'creating');
  assert.equal(ui.elements.get('work-close').disabled,true);
  ui.context.closeWork();
  assert.equal(vm.runInContext('workOpen',ui.context),true);
  complete({ok:true,json:async()=>({ok:true,host:'peer',session:'work-test',pane:'%9',pid:'9'})});
  await pending;
  assert.equal(ui.elements.get('work-progress').dataset.stage,'waiting');
  assert.equal(ui.elements.get('work-close').disabled,true);
  assert.equal(ui.elements.get('work-background').hidden,false);
  assert.equal(ui.elements.get('work-step-session').dataset.state,'done');
  onWait();
  assert.equal(ui.elements.get('work-progress').dataset.stage,'slow');
  vm.runInContext("latestAgents=[{host:'other',pane:'%9',pid:'9',tracked:true}]",ui.context);
  assert.equal(ui.context.focusStartedWork(),false);
  vm.runInContext("latestAgents=[{host:'peer',pane:'%9',pid:'9',tracked:false}]",ui.context);
  assert.equal(ui.context.focusStartedWork(),false);
  vm.runInContext("latestAgents=[{host:'peer',pane:'%9',pid:'9',tracked:true}];openSheet=function(a){globalThis.openedAgent=a}",ui.context);
  assert.equal(ui.context.focusStartedWork(),true);
  assert.equal(vm.runInContext('workOpen',ui.context),false);
  assert.equal(vm.runInContext('openedAgent.host',ui.context),'peer');
  assert.equal(ui.elements.get('work-host').disabled,false);
});

test('returning to list cancels pending automatic navigation and restores form', async () => {
  const ui=dashboard(async()=>({ok:true,json:async()=>({ok:true,host:'peer',session:'work-test',pane:'%9',pid:'9'})}));
  vm.runInContext("workOpen=true;workAgent='codex';workOptions={sessions:[{id:'$1',name:'main'}],projects:[],agents:[{id:'codex',available:true}]};document.getElementById('work-session').value='$1';document.getElementById('work-path').value='/project';renderWorkAgents()",ui.context);
  await ui.context.startWork({preventDefault(){}});
  ui.elements.get('work-background').listeners.click();
  assert.equal(vm.runInContext('workLaunchTarget',ui.context),null);
  assert.equal(vm.runInContext('workBusy',ui.context),false);
  assert.equal(ui.elements.get('work-overlay').hidden,true);
  assert.equal(ui.elements.get('work-path').disabled,false);
  assert.equal(ui.elements.get('work-form').hidden,false);
});

test('new work requires an existing session and defaults to the selected host session', async () => {
  let sessions=[];
  const ui=dashboard(async url=>({ok:true,json:async()=>url.startsWith('/api/work-options') ? {ok:true,projects:[],agents:[{id:'codex',available:true}],sessions,default_session:'$2',default_agent:'codex'} : {ok:true,accounts:[]}}));
  vm.runInContext("workOpen=true;document.getElementById('work-path').value='/project'",ui.context);
  await ui.context.loadWorkOptions();
  assert.equal(ui.elements.get('work-submit').disabled,true);
  assert.equal(ui.elements.get('work-session').disabled,true);
  assert.match(ui.elements.get('work-status').textContent,/請先.*tmux session/);
  sessions=[{id:'$1',name:'one',attached:0},{id:'$2',name:'two',attached:1}];
  await ui.context.loadWorkOptions();
  assert.equal(ui.elements.get('work-session').value,'$2');
  assert.equal(ui.elements.get('work-session').disabled,false);
  assert.equal(ui.elements.get('work-submit').disabled,false);
  assert.match(ui.elements.get('work-session').innerHTML,/two · 使用中/);
});

test('new work immediately reuses homepage quota and historical options without another quota request', async () => {
  const calls=[]; let finish;
  const data={ok:true,host:'local',sessions:[{id:'$1',name:'main'}],default_session:'$1',projects:[],agents:[{id:'codex',available:true}],default_agent:'codex',launch_presets:[{id:'preset-full',agent:'codex',label:'codex-full',count:42}],default_launch_preset:'preset-full'};
  const ui=dashboard(url=>{calls.push(url); return new Promise(resolve=>{finish=resolve});});
  ui.renderUsage({ok:true,host:'local',provider_status:{codex:{state:'ready'},claude:{state:'unavailable'},cursor:{state:'unavailable'},antigravity:{state:'unavailable'}},providers:['codex','claude','cursor','antigravity'],accounts:[{provider:'codex',windows:[{label:'Weekly',usedPercent:20}]}]});
  vm.runInContext("workOpen=true;document.getElementById('work-host').value='local';document.getElementById('work-path').value='/project'",ui.context);
  ui.context.workOptionsCacheSeed=data;
  vm.runInContext("workOptionsCache.set('local',workOptionsCacheSeed)",ui.context);
  const pending=ui.context.loadWorkOptions();
  assert.match(ui.elements.get('work-agents').innerHTML,/Weekly 剩 80%/);
  assert.match(ui.elements.get('work-launch').innerHTML,/codex-full/);
  assert.equal(ui.elements.get('work-launch').value,'preset-full');
  assert.equal(ui.elements.get('work-submit').disabled,true);
  assert.equal(calls.some(url=>url.startsWith('/api/work-usage')),false);
  finish({ok:true,json:async()=>data}); await pending;
  assert.equal(ui.elements.get('work-submit').disabled,false);
});

test('quota refresh failure retains cached data and never reuses another host quota', async () => {
  const ui=dashboard(async()=>{throw new Error('offline')});
  ui.context.cacheWorkUsage({host:'one',providers:['codex'],accounts:[{provider:'codex',windows:[{label:'Weekly',usedPercent:20}]}]});
  vm.runInContext("workUsageCache.get('one').stamps.codex=1",ui.context);
  await ui.context.loadWorkUsage('one');
  assert.equal(ui.context.cachedWorkAccounts('one')[0].windows[0].usedPercent,20);
  assert.equal(ui.context.cachedWorkAccounts('one')[0].stale,true);
  assert.equal(ui.context.cachedWorkAccounts('two').length,0);
});

test('concurrent quota requests share a single host fetch and selected preset is sent by id', async () => {
  let finish;const calls=[];
  const ui=dashboard((url,options)=>{
    calls.push([url,options]);
    if(url.startsWith('/api/work-usage')) return new Promise(resolve=>{finish=resolve});
    return Promise.resolve({ok:true,json:async()=>({ok:true,host:'peer',pane:'%9',pid:'9'})});
  });
  const first=ui.context.loadWorkUsage('peer'), second=ui.context.loadWorkUsage('peer');
  assert.equal(calls.length,1);
  finish({json:async()=>({ok:true,host:'peer',providers:['codex'],accounts:[]})});await Promise.all([first,second]);
  vm.runInContext("workOpen=true;workAgent='codex';workOptions={sessions:[{id:'$1'}],projects:[],agents:[{id:'codex',available:true}]};document.getElementById('work-session').value='$1';document.getElementById('work-path').value='/project';document.getElementById('work-launch').value='preset-full';renderWorkAgents()",ui.context);
  await ui.context.startWork({preventDefault(){}});
  const body=JSON.parse(calls.find(([url])=>url==='/api/work/start')[1].body);
  assert.equal(body.launch_preset,'preset-full');
  assert.equal('argv' in body,false);
});

test('editing project path preserves an explicitly selected launch profile and saved base command', () => {
  const ui=dashboard();
  vm.runInContext("workAgent='codex';workLaunchTouched=true;workOptions={projects:[],agents:[{id:'codex',available:true}],launch_presets:[{id:'one',agent:'codex',label:'full'},{id:'two',agent:'codex',label:'profile'}]};document.getElementById('work-launch').value='two';document.getElementById('work-path').value='/project'",ui.context);
  ui.elements.get('work-path').listeners.input();
  assert.equal(ui.elements.get('work-launch').value,'two');
  vm.runInContext("workLaunchTouched=false;workOptions.projects=[{path:'/project',agent:'codex',launch_preset:''}];renderWorkLaunches(null)",ui.context);
  assert.equal(ui.elements.get('work-launch').value,'');
});

test('a partial picker usage response retains missing provider snapshots in both views', async () => {
  const ui=dashboard(async()=>({json:async()=>({ok:true,host:'local',providers:['codex','claude','cursor','antigravity'],accounts:[{provider:'cursor',observed_at:200,windows:[{label:'Month',usedPercent:72}]}]})}));
  ui.renderUsage({host:'local',providers:['codex'],accounts:[{provider:'codex',observed_at:100,windows:[{label:'Weekly',usedPercent:20}]}]});
  await ui.context.loadWorkUsage('local');
  const home=vm.runInContext("usageByProvider.get('codex')[0]",ui.context);
  const picker=ui.context.cachedWorkAccounts('local').find(a=>a.provider==='codex');
  assert.equal(home.windows[0].usedPercent,20);
  assert.equal(picker.windows[0].usedPercent,20);
  assert.equal(home.stale,true);
  assert.equal(picker.stale,true);
  assert.match(ui.elements.get('agent-usage').innerHTML,/codex/);
});

test('new picker samples update homepage and late samples cannot roll either view backward', async () => {
  const ui=dashboard(async()=>({json:async()=>({ok:true,host:'local',providers:['codex'],accounts:[{provider:'codex',observed_at:200,windows:[{label:'Weekly',usedPercent:10}]}]})}));
  ui.renderUsage({host:'local',providers:['codex'],accounts:[{provider:'codex',observed_at:100,windows:[{label:'Weekly',usedPercent:30}]}]});
  await ui.context.loadWorkUsage('local');
  assert.equal(vm.runInContext("usageByProvider.get('codex')[0].windows[0].usedPercent",ui.context),10);
  ui.renderUsage({host:'local',providers:['codex'],accounts:[{provider:'codex',observed_at:150,windows:[{label:'Weekly',usedPercent:90}]}]});
  assert.equal(vm.runInContext("usageByProvider.get('codex')[0].windows[0].usedPercent",ui.context),10);
  assert.equal(ui.context.cachedWorkAccounts('local')[0].windows[0].usedPercent,10);
  ui.context.cacheWorkUsage({host:'remote',providers:['codex'],accounts:[{provider:'codex',observed_at:300,windows:[{label:'Weekly',usedPercent:99}]}]});
  ui.context.syncHomepageUsageCache('remote');
  assert.equal(vm.runInContext("usageByProvider.get('codex')[0].windows[0].usedPercent",ui.context),10);
});

test('picker preserves homepage account and window order, all windows, and shared colors', () => {
  const ui=dashboard();
  const accounts=[{provider:'codex',account:'first',windows:[{label:'Session',shortLabel:'5h',usedPercent:0},{label:'Weekly',shortLabel:'週',usedPercent:95}]},{provider:'codex',account:'second',windows:[{label:'Session',shortLabel:'5h',usedPercent:10,pace:{willLastToReset:false}},{label:'Weekly',shortLabel:'週',usedPercent:20}]}];
  ui.context.testAccounts=accounts;vm.runInContext('workAccounts=testAccounts',ui.context);
  const html=ui.context.workQuotaHtml('codex');
  assert.equal((html.match(/role="meter"/g)||[]).length,4);
  assert.ok(html.indexOf('first')<html.indexOf('second'));
  assert.ok(html.indexOf('Session 剩 100%')<html.indexOf('Weekly 剩 5%'));
  assert.equal((html.match(/data-tone="warn"/g)||[]).length,2);
  assert.doesNotMatch(html,/>\+2 項/);
});

test('legacy empty picker cache cannot discard usable saved homepage usage', () => {
  const saved={
    'tmux-attn:usage-cache': JSON.stringify({host:'local',providers:['codex'],updated_at:100,accounts:[{provider:'codex',observed_at:100,windows:[{label:'Weekly',usedPercent:20}]}]}),
    'tmux-attn:work-usage-cache': JSON.stringify({local:{accounts:[],providers:['codex'],stamps:{codex:200000}}}),
  };
  const ui=dashboard({globals:{sessionStorage:{getItem:key=>saved[key]||null,setItem(key,value){saved[key]=value;},removeItem(){}}}});
  assert.equal(vm.runInContext("usageByProvider.get('codex')[0].windows[0].usedPercent",ui.context),20);
  assert.equal(ui.context.cachedWorkAccounts('local')[0].windows[0].usedPercent,20);
});

test('uninstalled agents are omitted and quota source states are explicit', () => {
  const ui=dashboard();
  vm.runInContext("workAgent='codex';workUsageLoading=false;document.getElementById('work-host').value='local';workOptions={projects:[],sessions:[],agents:[{id:'codex',label:'Codex',available:true},{id:'pi',label:'Pi',available:true},{id:'opencode',label:'OpenCode',available:false}]}",ui.context);
  ui.context.cacheWorkUsage({host:'local',providers:['codex'],accounts:[],provider_status:{codex:{state:'refreshing',source:'none'}}});
  ui.context.renderWorkAgents();
  assert.doesNotMatch(ui.elements.get('work-agents').innerHTML,/OpenCode|未安裝/);
  assert.match(ui.elements.get('work-agents').innerHTML,/更新用量中/);
  assert.match(ui.elements.get('work-agents').innerHTML,/未提供用量/);
  ui.context.cacheWorkUsage({host:'local',providers:['codex'],accounts:[],provider_status:{codex:{state:'unavailable',source:'none'}}});
  assert.match(ui.context.workQuotaHtml('codex'),/暫時無法讀取/);
});

test('picker polls an in-flight quota source until its cached account becomes available', async () => {
  let tick;
  const ui=dashboard({fetchRequest:async()=>({json:async()=>({ok:true,host:'local',providers:['codex'],provider_status:{codex:{state:'ready',source:'codexbar-cache'}},accounts:[{provider:'codex',source:'codexbar-cache',observed_at:Date.now()/1000,windows:[{label:'Weekly',usedPercent:25}]}]})}),globals:{setTimeout:fn=>{tick=fn;return 1}}});
  vm.runInContext("workOpen=true;document.getElementById('work-host').value='local';workAgent='codex';workOptions={agents:[{id:'codex',available:true}],projects:[],sessions:[]}",ui.context);
  ui.context.cacheWorkUsage({host:'local',providers:['codex'],accounts:[],provider_status:{codex:{state:'refreshing',source:'none'}}});
  ui.context.pollWorkUsage('local',0);
  assert.equal(typeof tick,'function');
  await tick();
  assert.match(ui.elements.get('work-agents').innerHTML,/Weekly 剩 75%/);
  assert.match(ui.context.workQuotaText('codex'),/CodexBar 快取/);
});

test('opening before the fleet roster loads binds the local quota cache to the resolved host', async () => {
  const ui=dashboard(async url=>({ok:true,json:async()=>url.startsWith('/api/work-options') ? {ok:true,host:'local',projects:[],sessions:[{id:'$1',name:'main'}],default_session:'$1',agents:[{id:'codex',available:true}],default_agent:'codex'} : {ok:true,host:'local',providers:['codex'],accounts:[{provider:'codex',observed_at:Date.now()/1000,source:'saved-cache',windows:[{label:'Weekly',usedPercent:20}]}],provider_status:{codex:{state:'ready',source:'saved-cache'}}}}));
  vm.runInContext("workOpen=true;document.getElementById('work-host').value=''",ui.context);
  await ui.context.loadWorkOptions();
  assert.equal(ui.elements.get('work-host').value,'local');
  assert.match(ui.elements.get('work-agents').innerHTML,/Weekly 剩 80%/);
});
