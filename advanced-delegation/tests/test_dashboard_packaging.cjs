'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const dashboard = path.resolve(__dirname, '../dashboard');

// Model the dashboard's manifest-selected script load and registry callback.
function loadDashboard(dir) {
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const script = fs.readFileSync(path.join(dir, manifest.entry), 'utf8');
  const registrations = [];
  const slots = [];
  const window = { __HERMES_PLUGINS__: {
    register: (name, component) => registrations.push({ name, component }),
    registerSlot: (...args) => slots.push(args),
  } };
  vm.runInNewContext(script, { window }, { filename: manifest.entry, timeout: 1000 });
  return { manifest, registrations, slots };
}

test('manifest entry loads and registers a hidden API-only placeholder', () => {
  const { manifest, registrations, slots } = loadDashboard(dashboard);
  assert.equal(manifest.name, 'advanced-delegation');
  assert.equal(manifest.tab.path, '/advanced-delegation');
  assert.equal(manifest.tab.hidden, true);
  assert.equal(manifest.entry, 'dist/index.js');
  assert.equal(manifest.api, 'api.py');
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].name, manifest.name);
  assert.equal(typeof registrations[0].component, 'function');
  assert.equal(registrations[0].component(), null);
  assert.deepEqual(slots, []);
});

test('loader rejects a package missing its declared entry', (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'delegation-dashboard-'));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  fs.copyFileSync(path.join(dashboard, 'manifest.json'), path.join(temp, 'manifest.json'));
  assert.throws(() => loadDashboard(temp), { code: 'ENOENT' });
});
