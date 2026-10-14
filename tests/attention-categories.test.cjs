const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const cases = JSON.parse(readFileSync(join(__dirname, 'fixtures/attention-categories.json'), 'utf8'));
const html = readFileSync(join(__dirname, '../web/index.html'), 'utf8');

function categories() {
  // Run the production helpers without duplicating the dashboard's DOM harness.
  const functions = ['agentKey', 'category', 'isUnread'].map(name => {
    const match = html.match(new RegExp(`^    function ${name}\\([\\s\\S]*?^    \\}`, 'm'));
    assert.ok(match, `Missing production function: ${name}`);
    return match[0];
  });
  const emoji = html.match(/^    const SECTION_EMOJI = .*;$/m);
  assert.ok(emoji, 'Missing production category emoji map');
  const context = vm.createContext({});
  vm.runInContext(`const acknowledgedVersions = new Map();\n${emoji[0]}\n${functions.join('\n')}`, context);
  return vm.runInContext('({ category, isUnread, agentKey, acknowledgedVersions, SECTION_EMOJI })', context);
}

for (const fixture of cases) {
  test(`shared attention contract: ${fixture.id}`, () => {
    const ui = categories();
    const legacy = { host: 'work', pane: '%1', pid: '123', ...fixture.row };
    const supplied = { ...legacy, category: fixture.category };
    assert.equal(ui.category(legacy), fixture.category, 'legacy fallback');
    assert.equal(ui.category(supplied), fixture.category, 'server category');
    assert.equal(ui.SECTION_EMOJI[ui.category(legacy)], fixture.emoji);
    assert.equal(ui.SECTION_EMOJI[ui.category(supplied)], fixture.emoji);
  });
}

test('server category takes precedence over legacy IDLE flags', () => {
  const ui = categories();
  const row = { host: 'work', pane: '%1', pid: '123', tracked: true, state: 'IDLE',
    needs_review: true, unread: true, pending: true, revision: 3, acknowledged: 2 };
  assert.equal(ui.category(row), 'review');
  assert.equal(ui.category({ ...row, category: 'idle' }), 'idle');
});

test('a newer revision without a completion is not unread', () => {
  const ui = categories();
  const row = { host: 'work', pane: '%1', pid: '123', tracked: true, state: 'IDLE',
    needs_review: false, unread: false, pending: false, revision: 3, acknowledged: 0 };
  assert.equal(ui.isUnread(row), false);
  assert.equal(ui.category(row), 'idle');
  assert.equal(ui.category({ ...row, category: 'idle' }), 'idle');
});

for (const serverCategory of [false, true]) {
  test(`version acknowledgement moves review to idle and preserves newer results (${serverCategory ? 'server' : 'legacy'})`, () => {
    const ui = categories();
    const row = { host: 'work', pane: '%1', pid: '123', tracked: true, state: 'IDLE',
      needs_review: true, unread: true, pending: true, reviewed: false, revision: 7, acknowledged: 3,
      ...(serverCategory ? { category: 'review' } : {}) };
    const key = ui.agentKey(row);
    assert.equal(ui.category(row), 'review');
    ui.acknowledgedVersions.set(key, 6);
    assert.equal(ui.category(row), 'review', 'older acknowledgement cannot clear version 7');
    ui.acknowledgedVersions.set(key, 7);
    assert.equal(ui.isUnread(row), false);
    assert.equal(ui.category(row), 'idle', 'stale fleet remains locally acknowledged');
    assert.equal(ui.category({ ...row }), 'idle', 'acknowledgement survives replacement fleet objects');
    const newer = { ...row, revision: 8 };
    assert.equal(ui.isUnread(newer), true);
    assert.equal(ui.category(newer), 'review', 'a newer completion needs review again');
    for (const identity of [{ host: 'other' }, { pane: '%2' }, { pid: '456' }]) {
      assert.equal(ui.category({ ...row, ...identity }), 'review', 'acknowledgements are scoped to pane identity');
    }
    assert.equal(row.unread, true, 'classification does not mutate the fleet payload');
  });
}

test('acknowledgement does not change WAIT or RUNNING categories', () => {
  const ui = categories();
  for (const [state, category] of [['WAIT', 'wait'], ['RUNNING', 'running']]) {
    const row = { host: 'work', pane: '%1', pid: '123', tracked: true, state,
      category, needs_review: true, unread: true, pending: true, revision: 7, acknowledged: 0 };
    ui.acknowledgedVersions.set(ui.agentKey(row), 7);
    assert.equal(ui.category(row), category);
    assert.equal(ui.category({ ...row, category: undefined }), category);
  }
});
