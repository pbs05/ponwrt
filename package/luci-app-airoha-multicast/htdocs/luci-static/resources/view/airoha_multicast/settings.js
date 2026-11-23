'use strict';
'require view';
'require form';
'require rpc';
'require uci';
'require ui';
'require dom';
'require tools.widgets as widgets';

var getStatus = rpc.declare({ object: 'luci.airoha_multicast', method: 'getStatus', expect: {}, nobatch: true });
var applyPolicy = rpc.declare({ object: 'luci.airoha_multicast', method: 'apply', expect: {}, nobatch: true });
var commit = rpc.declare({ object: 'uci', method: 'commit', params: [ 'config' ], reject: true });

return view.extend({
	load: function() {
		return Promise.all([ uci.load('airoha_multicast'), L.resolveDefault(getStatus(), {}) ]);
	},
	status: function() {
		var r = this.runtime, nodes = [ E('h3', {}, _('Running policy')) ];
		if (!r || r.error || !Number.isInteger(r.enabled))
			return E('p', { 'class': 'alert-message warning' }, _('Multicast status unavailable. Saved settings do not prove that hardware offload is active.'));
		var text;
		if (r.broken) text = _('Hardware fault; reboot is required for recovery. Delivery may be interrupted.');
		else if (r.scope_lost) text = r.live_apply ? _('Interface was recreated. Waiting for interface recovery; Apply can rebind the configured scope online.') : _('Interface was removed. This older kernel requires a reboot.');
		else if (!r.frozen) text = _('Waiting for the configured interfaces and policy.');
		else if (!r.enabled) text = _('Offload disabled; multicast uses software forwarding.');
		else if (r.paused) text = _('Temporarily paused by a diagnostic tool.');
		else if (!r.running) text = _('Policy accepted; waiting for hardware initialization.');
		else text = _('Offload ready; active hardware flows: %s.').format(r.active);
		nodes.push(E('p', { 'class': r.broken || r.scope_lost ? 'alert-message warning' : '' }, text));
		var changed = String(r.enabled) != (uci.get('airoha_multicast', 'main', 'enabled') || '0');
		if (uci.get('airoha_multicast', 'main', 'enabled') == '1')
			changed = changed || [ 'upstream', 'downstream' ].some(function(k) { return r['applied_' + k] != uci.get('airoha_multicast', 'main', k); });
		if (changed) nodes.push(E('p', { 'class': 'alert-message warning' }, r.live_apply ? _('Saved settings differ from the accepted policy. Use Save & Apply; no reboot is needed.') : _('This kernel only accepts policy at boot. Save settings and reboot to apply them.')));
		nodes.push(E('p', {}, _('Accepted scope: %s → %s; interface indices: %s → %s; VLAN IDs: %s → %s.').format(
			r.applied_upstream || '?', r.applied_downstream || '?', r.ingress, r.egress, r.ingress_vid, r.egress_vid)));
		nodes.push(E('p', {}, _('Pending flows: %s; invalidated: %s; errors: %s.').format(r.pending, r.invalidated, r.errors)));
		if (r.broken || (!r.live_apply && (changed || r.scope_lost)))
			nodes.push(E('a', { 'class': 'cbi-button', 'href': L.url('admin/system/reboot') }, _('Open reboot page')));
		nodes.push(E('button', { 'class': 'cbi-button', 'click': ui.createHandlerFn(this, 'refresh') }, _('Refresh status')));
		return E('div', {}, nodes);
	},
	refresh: function() {
		return L.resolveDefault(getStatus(), {}).then(L.bind(function(r) {
			this.runtime = r;
			dom.content(this.statusNode, this.status());
		}, this));
	},
	render: function(data) {
		this.runtime = data[1];
		var m = this.map = new form.Map('airoha_multicast', _('Multicast offload'),
			_('Accelerate supported IPv4 multicast forwarding from the PON interface to a LAN bridge. Multicast routing and firewall permissions must already be configured.'));
		var s = m.section(form.NamedSection, 'main', 'offload');
		s.addremove = false;
		var o = s.option(form.Flag, 'enabled', _('Enable multicast hardware offload'));
		o.rmempty = false;
		o.description = _('Disabling offload keeps software multicast routing available. This switch does not disable IPTV or filter unsolicited multicast.');
		[ [ 'upstream', _('Upstream logical interface') ], [ 'downstream', _('Downstream logical interface') ] ].forEach(function(field) {
			var o = s.option(widgets.NetworkSelect, field[0], field[1]);
			o.multiple = false;
			o.nocreate = true;
			o.rmempty = false;
			o.description = field[0] == 'upstream' ? _('Select the PON IPoE interface, optionally with one VLAN. PPPoE and QinQ are not supported by this backend.') : _('Select the LAN bridge or its VLAN interface. Subscriptions determine the actual output ports.');
			o.validate = function(section, value) {
				if (!/^[a-zA-Z0-9_.:-]+$/.test(value || '')) return _('Select an existing logical interface.');
				var other = this.map.lookupOption(field[0] == 'upstream' ? 'downstream' : 'upstream', section)[0];
				return other && other.formvalue(section) == value ? _('Upstream and downstream must differ.') : true;
			};
		});
		o = s.option(form.DummyValue, '_apply', _('Applying changes'));
		o.cfgvalue = function() { return _('On a kernel with online-policy support, Save & Apply withdraws old multicast flows and relearns them in the new scope. Brief software forwarding and playback jitter are possible. WAN and LAN interfaces are not restarted. Save alone stores settings without changing the running policy.'); };
		o = s.option(form.DummyValue, '_scope', _('Interface recovery'));
		o.cfgvalue = function() { return _('When an applied logical interface is recreated, its ifup event revalidates the PON/bridge devices and restores offload. Saved-but-unapplied edits are not used by this recovery. Hardware invalidation faults still require a reboot.'); };
		this.statusNode = E('div', {}, this.status());
		return m.render().then(L.bind(function(node) { return E('div', {}, [ node, this.statusNode ]); }, this));
	},
	handleSave: function() {
		return this.map.save().then(function() { return commit('airoha_multicast'); }).then(L.bind(function() {
			ui.addNotification(null, E('p', {}, _('Settings saved; the running multicast policy was not changed.')), 'info');
			return this.refresh();
		}, this));
	},
	handleSaveApply: function() {
		return this.map.save().then(function() { return commit('airoha_multicast'); }).then(applyPolicy).then(L.bind(function(r) {
			if (!r.ok) throw new Error(r.error == 'reboot_required' ? _('This kernel requires a reboot. Settings have been saved.') : _('Apply failed. Check interface availability, driver status and the system log.'));
			ui.addNotification(null, E('p', {}, _('Policy accepted online. Refresh status to confirm hardware readiness.')), 'info');
			return this.refresh();
		}, this)).catch(L.bind(function(e) {
			ui.addNotification(null, E('p', {}, e.message), 'error');
			return this.refresh();
		}, this));
	}
});
