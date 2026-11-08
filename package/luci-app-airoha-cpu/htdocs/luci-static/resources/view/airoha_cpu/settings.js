'use strict';
'require view';
'require form';
'require rpc';
'require uci';
'require ui';
'require dom';

var getStatus = rpc.declare({ object: 'luci.airoha_cpu', method: 'getStatus', expect: {}, nobatch: true });
var applyPolicy = rpc.declare({ object: 'luci.airoha_cpu', method: 'apply', expect: {}, nobatch: true });
var commit = rpc.declare({ object: 'uci', method: 'commit', params: [ 'config' ], reject: true });
var keys = [ 'wan_cpu', 'lan_cpu', 'special_cpu', 'other_irq_cpu', 'service_cpus', 'unbound_cpus' ];

function mask(value) {
	if (!/^[0-3](-[0-3])?(,[0-3](-[0-3])?)*$/.test(value || '')) return null;
	var result = 0;
	for (var part of value.split(',')) {
		var ends = part.split('-').map(Number), hi = ends.length == 2 ? ends[1] : ends[0];
		if (ends[0] > hi) return null;
		for (var i = ends[0]; i <= hi; i++) result |= 1 << i;
	}
	return result;
}

return view.extend({
	load: function() {
		return Promise.all([ uci.load('airoha_cpu'), L.resolveDefault(getStatus(), {}) ]);
	},
	status: function() {
		var r = this.runtime, state = r && r.state, nodes = [ E('h3', {}, _('Running policy')) ];
		if (!r || r.error || !r.snapshot)
			return E('p', { 'class': 'alert-message warning' }, _('CPU policy status unavailable. Saved settings do not prove that the policy is active.'));
		var enabled = uci.get('airoha_cpu', 'main', 'enabled') == '1', active = !!(state && state.active);
		var changed = enabled != active || (enabled && keys.some(function(k) {
			var value = uci.get('airoha_cpu', 'main', k);
			return k.endsWith('_cpus') ? mask(value) != mask(state && state.config && state.config[k]) :
				value != (state && state.config && state.config[k]);
		}));
		if (state && state.trial && active)
			nodes.push(E('p', { 'class': 'alert-message warning' }, _('A temporary trial is active. Finish or restore the trial before applying persistent settings.')));
		else nodes.push(E('p', {}, active ? _('CPU policy active') : _('CPU policy inactive')));
		if (changed) nodes.push(E('p', { 'class': 'alert-message warning' }, _('Saved settings differ from the running policy. Apply them online; no reboot is needed.')));
		if (active) {
			var actual = r.actual || [], missing = actual.filter(function(o) { return o.value == null; });
			var wrong = actual.filter(function(o) { return o.value != null && (o.value != o.want || (o.effective != null && o.effective != o.want)); });
			nodes.push(E('p', { 'class': wrong.length ? 'alert-message warning' : '' },
				_('Affinity entries checked: %s; mismatches: %s; exited or unavailable objects: %s.').format(actual.length - missing.length, wrong.length, missing.length)));
			var rows = keys.map(function(k) { return E('tr', {}, [ E('td', {}, this.labels[k]), E('td', {}, (state.config || {})[k] || '?') ]); }, this);
			nodes.push(E('table', { 'class': 'table' }, rows));
		}
		if (state && ((state.failed || []).length || (state.conflicts || []).length))
			nodes.push(E('p', { 'class': 'alert-message warning' }, _('Restore failures or externally changed affinities were recorded. Inspect the controller status and system log.')));
		nodes.push(E('button', { 'class': 'cbi-button', 'click': ui.createHandlerFn(this, 'refresh') }, _('Refresh status')));
		return E('div', {}, nodes);
	},
	refresh: function() {
		return L.resolveDefault(getStatus(), {}).then(L.bind(function(r) {
			this.runtime = r;
			dom.content(this.statusNode, this.status());
		}, this));
	},
	validatePolicy: function() {
		var cfg = {}, s = 'main';
		for (var k of keys) {
			var option = this.map.lookupOption(k, s)[0];
			cfg[k] = option ? option.formvalue(s) : uci.get('airoha_cpu', s, k);
			if (mask(cfg[k]) == null) return _('Use CPU numbers 0–3, or a list such as 0,3.');
		}
		var network = mask(cfg.wan_cpu) | mask(cfg.lan_cpu);
		if (cfg.wan_cpu == cfg.lan_cpu || [ 'special_cpu', 'other_irq_cpu', 'service_cpus', 'unbound_cpus' ].some(function(k) { return !!(mask(cfg[k]) & network); }))
			return _('WAN and LAN must use different CPUs. Other roles must exclude both of those CPUs.');
		return true;
	},
	render: function(data) {
		this.runtime = data[1];
		this.labels = { wan_cpu: _('WAN receive and completion CPU'), lan_cpu: _('LAN receive and completion CPU'),
			special_cpu: _('Special receive queues CPU'), other_irq_cpu: _('Other device interrupts CPU'),
			service_cpus: _('Ordinary applications CPUs'), unbound_cpus: _('Unbound workqueue CPUs') };
		var m = this.map = new form.Map('airoha_cpu', _('Airoha CPU scheduling'),
			_('Keep ingress IRQs and threaded NAPI together. Applying changes is online and may briefly increase jitter while tasks move between CPUs.'));
		var s = m.section(form.NamedSection, 'main', 'policy');
		s.addremove = false;
		var o = s.option(form.Flag, 'enabled', _('Enable CPU scheduling policy'));
		o.rmempty = false;
		o.description = _('Disabling restores affinities owned by this controller. Existing custom application affinities are preserved.');
		keys.forEach(function(k, i) {
			var o = s.option(i < 4 ? form.ListValue : form.Value, k, this.labels[k]);
			if (i < 4) for (var cpu = 0; cpu < 4; cpu++) o.value('' + cpu, 'CPU' + cpu);
			o.rmempty = false;
			o.validate = L.bind(this.validatePolicy, this);
		}, this);
		o = s.option(form.DummyValue, '_limits', _('Scope'));
		o.cfgvalue = function() { return _('WAN/LAN describe packet ingress and completion queues, not every transmit instruction. Hardware forwarding is independent. Per-CPU workers, including WireGuard crypto workers, are not confined by the unbound-workqueue setting.'); };
		this.statusNode = E('div', {}, this.status());
		return m.render().then(L.bind(function(node) { return E('div', {}, [ node, this.statusNode ]); }, this));
	},
	handleSave: function() {
		return this.map.save().then(function() { return commit('airoha_cpu'); }).then(L.bind(function() {
			ui.addNotification(null, E('p', {}, _('Settings saved. Use Save & Apply to apply them online.')), 'info');
			return this.refresh();
		}, this));
	},
	handleSaveApply: function() {
		return this.map.save().then(function() { return commit('airoha_cpu'); }).then(applyPolicy).then(L.bind(function(r) {
			if (!r.ok) throw new Error(r.error == 'trial_active' ? _('A temporary trial is active.') : _('Apply failed. Settings remain saved; refresh status and inspect the system log.'));
			ui.addNotification(null, E('p', {}, _('CPU policy applied online.')), 'info');
			return this.refresh();
		}, this)).catch(L.bind(function(e) {
			ui.addNotification(null, E('p', {}, e.message), 'error');
			return this.refresh();
		}, this));
	}
});
