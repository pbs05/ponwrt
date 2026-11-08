'use strict';
'require baseclass';

return baseclass.extend({
	trigger: _('Default route connection'),
	description: _('The LED is on while an installed IPv4 or IPv6 default route has carrier. Interface and device names are detected automatically. This does not check Internet reachability.'),
	kernel: false,
	addFormOptions: function(s) {}
});
