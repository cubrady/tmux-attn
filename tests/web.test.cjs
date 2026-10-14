const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

function dashboard() {
  const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    innerHTML: '', textContent: '', hidden: false, dataset: {}, listeners: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, listener) { this.listeners[type] = listener; },
    setAttribute() {},
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
  const context = vm.createContext({
    window: {}, document, performance: { now: () => 0 },
    fetch: () => new Promise(() => {}), setInterval() {},
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script, context);
  return { elements, render: data => context.render(data) };
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
