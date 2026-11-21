'use strict';
'require view';
'require form';
'require rpc';
'require uci';
'require ui';
'require dom';

var getStatus = rpc.declare({ object: 'luci.airoha_qos', method: 'getStatus', expect: {} });
var getDefaults = rpc.declare({ object: 'luci.airoha_qos', method: 'getDefaults', expect: {} });
var commit = rpc.declare({ object: 'uci', method: 'commit', params: [ 'config' ], reject: true });
var basePolicy;

function profile(mode, entries) {
	mode = mode == 'compat' ? 'priority' : (mode || 'priority');
	if (mode != 'priority' && mode != 'dscp')
		throw new Error(_('Unknown mode'));
	if (!validStatus(basePolicy) || basePolicy.preset != 'ppe-legacy-v1')
		throw new Error(_('The base mapping is unavailable. Update the QoS controller and refresh this page.'));
	var seen = {}, map = basePolicy.map.slice();
	(Array.isArray(entries) ? entries : entries ? [ entries ] : []).forEach(function(value) {
		var match = /^(\d+):(\d+)$/.exec(value);
		if (!match || +match[1] > 63 || +match[2] > 7)
			throw new Error(_('Use DSCP:QUEUE, with DSCP 0–63 and queue 0–7.'));
		var d = +match[1], q = +match[2];
		if (seen[d]) throw new Error(_('Each DSCP may appear only once.'));
		seen[d] = true;
		map[d] = q;
	});
	return { mode: mode, map: map, overrides: seen };
}

function validStatus(s) {
	return s && [ 'priority', 'compat', 'dscp' ].indexOf(s.mode) >= 0 &&
		Array.isArray(s.map) && s.map.length == 64 &&
		s.map.every(function(q) { return Number.isInteger(q) && q >= 0 && q < 8; });
}

return view.extend({
	profile: profile,
	validStatus: validStatus,
	load: function() {
		return Promise.all([ uci.load('airoha_qos'), L.resolveDefault(getStatus(), {}), L.resolveDefault(getDefaults(), {}) ]);
	},
	status: function() {
		var s = this.runtime, desired;
		try {
			var preset = uci.get('airoha_qos', 'main', 'preset') || 'ppe-legacy-v1';
			if (preset != 'ppe-legacy-v1') throw new Error(_('Unknown base mapping: %s').format(preset));
			desired = profile(uci.get('airoha_qos', 'main', 'mode'), uci.get('airoha_qos', 'main', 'map'));
		}
		catch (e) { return E('p', { 'class': 'alert-message warning' }, e.message); }
		var nodes = [ E('h3', {}, _('Running policy')) ];
		if (!validStatus(s)) {
			nodes.push(E('p', {}, _('Driver status is unavailable. The saved configuration does not prove that a hardware policy is active.')));
		} else {
			var names = { priority: _('Traditional priority'), dscp: _('DSCP'), compat: _('Legacy kernel: default mapping unknown') };
			nodes.push(E('p', {}, names[s.mode]));
			if (s.broken) nodes.push(E('p', { 'class': 'alert-message warning' }, _('The driver reports a QoS fault. Hardware policy is not confirmed. Base networking remains enabled.')));
			if (s.frozen) nodes.push(E('p', {}, _('The running policy is locked until the next boot.')));
			var changed = (s.mode == 'compat' ? desired.mode != 'priority' : s.mode != desired.mode) ||
				(desired.mode == 'dscp' && JSON.stringify(s.map) != JSON.stringify(desired.map));
			if (changed) {
				nodes.push(E('div', { 'class': 'alert-message warning' }, [
					E('strong', {}, _('Reboot required')),
					E('p', {}, _('Saved policy differs from the running policy. Reboot the device to apply it and rebuild the flow offload entries.')),
					E('a', { 'class': 'cbi-button', 'href': L.url('admin/system/reboot') }, _('Open reboot page'))
				]));
			} else if (!s.broken && s.mode != 'compat') {
				nodes.push(E('p', {}, _('Saved and running hardware mappings match. This does not verify external traffic marking rules.')));
			}
			if (Array.isArray(s.priority_map) && s.priority_map.length == 16) {
				nodes.push(E('p', {}, _('Default priority → queue (priority 0–15):') + ' ' + s.priority_map.map(function(q) { return 'Q' + q; }).join(', ')));
			} else {
				nodes.push(E('p', {}, _('This kernel does not report the revised default priority map.')));
			}
		}
		nodes.push(E('p', {}, _('Base mapping: original PPE compatibility map (ppe-legacy-v1). Empty overrides still use all 64 entries below. This is not a standard DiffServ or WMM mapping.')));
		if (!uci.get('airoha_qos', 'main', 'preset'))
			nodes.push(E('p', {}, _('This older configuration inherits ppe-legacy-v1. Saving makes the preset explicit without changing its mapping.')));
		var rows = [ E('tr', {}, [ E('th', {}, _('DSCP')), E('th', {}, _('Base queue')), E('th', {}, _('Configured DSCP queue')), E('th', {}, _('Running DSCP queue')) ]) ];
		for (var d = 0; d < 64; d++)
			rows.push(E('tr', {}, [ E('td', {}, '' + d), E('td', {}, 'Q' + basePolicy.map[d]),
				E('td', {}, 'Q' + desired.map[d] + (desired.overrides[d] ? ' (' + _('Override') + ')' : '')),
				E('td', {}, validStatus(s) && s.mode == 'dscp' ? 'Q' + s.map[d] : _('Inactive or unknown')) ]));
		nodes.push(E('details', { 'open': '' }, [ E('summary', {}, _('Complete DSCP table')), E('table', { 'class': 'table' }, rows) ]));
		nodes.push(E('button', { 'class': 'cbi-button', 'click': ui.createHandlerFn(this, 'refresh') }, _('Refresh status')));
		return E('div', {}, nodes);
	},
	refresh: function() {
		return L.resolveDefault(getStatus(), {}).then(L.bind(function(s) {
			this.runtime = s;
			dom.content(this.statusNode, this.status());
		}, this));
	},
	render: function(data) {
		this.runtime = data[1];
		basePolicy = data[2];
		var m = this.map = new form.Map('airoha_qos', _('Airoha QoS'),
			_('One policy covers the shared FE/QDMA/switch hardware. Save changes for the next boot. This page does not restart networking.'));
		var s = m.section(form.NamedSection, 'main', 'profile');
		s.addremove = false;
		var o = s.option(form.ListValue, 'mode', _('Classification mode'));
		o.value('priority', _('Traditional priority (default)'));
		o.value('dscp', _('DSCP determines hardware queues'));
		o.default = 'priority';
		o.rmempty = false;
		o.cfgvalue = function(section) { var v = uci.get('airoha_qos', section, 'mode'); return v == 'compat' ? 'priority' : v; };
		o.description = _('Traditional mode maps Linux priority to three QDMA classes: interactive/control, ordinary, and bulk/filler. PPE retains its existing TOS input; different forwarding paths may classify differently.');
		o = s.option(form.DummyValue, '_explanation', _('DSCP mode'));
		o.rawhtml = false;
		o.cfgvalue = function() { return _('DSCP directly selects Q0–Q7 across CPU, PPE and switch paths. This avoids legacy TOS classification, stale skb priority and software-flowtable differences. The original skb priority is preserved, but does not choose the hardware queue for parsed IP packets. Mark traffic consistently in both directions before offload; established PPE flows do not follow per-packet DSCP changes.'); };
		o = s.option(form.DummyValue, '_application', _('Applying policy changes'));
		o.cfgvalue = function() { return _('Save stores the hardware mode and queue mapping for the next device boot. Restarting the QoS service or the firewall does not apply this hardware profile.'); };
		o = s.option(form.DummyValue, '_marking', _('Traffic marking rules'));
		o.cfgvalue = function() { return _('Configure traffic-to-DSCP rules in Firewall → Traffic Rules and describe their purpose in each rule name. A full firewall reload rebuilds its software and hardware flowtables. Updating a separate nft marking table alone does not retire old flows. Supported multicast firmware also retires its own cache through the firewall include. None of these actions applies a pending hardware mode or queue-map change.'); };
		o = s.option(form.DummyValue, '_firewall', _('Business classification'));
		o.renderWidget = function() { return E('a', { 'href': L.url('admin/network/firewall/rules'), 'target': '_blank', 'rel': 'noopener' }, _('Open firewall traffic rules')); };
		o = s.option(form.ListValue, 'preset', _('Base DSCP mapping'));
		o.value('ppe-legacy-v1', _('Original PPE compatibility map (v1)'));
		o.default = 'ppe-legacy-v1';
		o.rmempty = false;
		o.retain = true;
		o.depends('mode', 'dscp');
		o.description = _('The named preset fixes the meaning of unspecified DSCP values across upgrades. Overrides below only replace selected entries.');
		o = s.option(form.DynamicList, 'map', _('DSCP → queue overrides'));
		o.depends('mode', 'dscp');
		o.retain = true;
		o.placeholder = '46:6';
		o.description = _('Format DSCP:QUEUE. Unspecified values retain the original PPE numeric map; this is not a standard DiffServ/WMM mapping. Q0 is valid. With default strict scheduling, Q7 is highest and Q0 lowest; no minimum bandwidth is guaranteed.');
		o.validate = function(section, value) {
			try {
				var entries = this.formvalue(section) || [];
				profile('dscp', entries);
				if (value) profile('dscp', Array.isArray(value) ? value : [ value ]);
				return true;
			} catch (e) { return e.message; }
		};
		o = s.option(form.Button, '_restore_map', _('Restore base mapping'));
		o.depends('mode', 'dscp');
		o.inputtitle = _('Clear overrides');
		o.description = _('Clears the edits in this form. Save to use the base mapping at the next boot.');
		o.onclick = L.bind(function() {
			var option = this.map.lookupOption('map', 'main')[0];
			if (option) option.getUIElement('main').setValue([]);
		}, this);
		this.statusNode = E('div', {}, this.status());
		return m.render().then(L.bind(function(node) { return E('div', {}, [ node, this.statusNode ]); }, this));
	},
	handleSave: function() {
		return this.map.save().then(function() { return commit('airoha_qos'); }).then(L.bind(function() {
			ui.addNotification(null, E('p', {}, _('QoS configuration saved for the next device boot. Use System → Reboot when ready to apply it.')), 'info');
			return this.refresh();
		}, this));
	},
	handleSaveApply: null
});
