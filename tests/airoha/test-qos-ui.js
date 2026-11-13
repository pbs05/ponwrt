#!/usr/bin/env node
// Host-only view/config contract tests; no browser or router connection.
const fs = require('fs');
const path = require('path');
const assert = require('assert').strict;
const root = path.resolve(__dirname, '../..');
const pkg = path.join(root, 'package/luci-app-airoha-qos');
const calls = [];
const rpc = { declare: spec => (...args) => {
	calls.push([spec.object, spec.method, args]);
	return Promise.resolve({});
}};
const view = { extend: object => object };
const ui = { addNotification: () => {}, createHandlerFn: () => {} };
const code = fs.readFileSync(path.join(pkg, 'htdocs/luci-static/resources/view/airoha_qos/settings.js'), 'utf8');
const page = new Function('view', 'form', 'rpc', 'uci', 'ui', 'dom', '_', 'L', 'E', code)(
	view, {}, rpc, {}, ui, {}, s => s, {bind: (fn, owner) => fn.bind(owner)}, (...args) => args);

const defaults = [0, 0, 2, 2, 6, 6, 4, 4];
assert.equal(page.profile('compat', []).mode, 'priority');
assert.equal(page.profile(null, []).mode, 'priority');
assert.deepEqual(page.profile('dscp', []).map, Array.from({length: 64}, (_, d) => defaults[d % 8]));
assert.equal(page.profile('dscp', ['7:0', '63:7']).map[7], 0);
assert.equal(page.profile('dscp', ['7:0', '63:7']).map[63], 7);
for (const values of [['1:2', '01:3'], ['64:0'], ['1:8'], ['-1:2'], ['1:2:3'], ['1:'], ['1:$(id)']])
	assert.throws(() => page.profile('dscp', values));
assert.throws(() => page.profile('bogus', []));
assert.equal(page.validStatus({}), false);
assert.equal(page.validStatus({mode: 'dscp', map: [0]}), false);
assert.equal(page.validStatus(page.profile('dscp', [])), true);
assert.equal(page.handleSaveApply, null);
assert(!code.includes('ui.changes.apply'));
assert(!code.includes('poll.add'));
const acl = JSON.parse(fs.readFileSync(path.join(pkg, 'root/usr/share/rpcd/acl.d/luci-app-airoha-qos.json')));
assert.deepEqual(acl['luci-app-airoha-qos'].write.uci, ['airoha_qos']);
assert.equal(acl['luci-app-airoha-qos'].write.file, undefined);
assert.deepEqual(acl['luci-app-airoha-qos'].read.ubus['luci.airoha_qos'], ['getStatus']);
page.map = {save: async () => { calls.push('save'); }};
page.refresh = async () => { calls.push('refresh'); };
page.handleSave().then(() => {
	assert.deepEqual(calls, ['save', ['uci', 'commit', ['airoha_qos']], 'refresh']);
	console.log('PASS: QoS view validation, legacy alias, queue zero, scoped save and read-only status contracts');
}).catch(e => { console.error(e); process.exitCode = 1; });
