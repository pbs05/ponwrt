// SPDX-License-Identifier: GPL-2.0-only
// Pure policy + transaction engine. All device I/O is injected by the caller.
'use strict';

export function cpumask(value) {
	if (type(value) != 'string' || !match(value, /^[0-3](-[0-3])?(,[0-3](-[0-3])?)*$/))
		die(`Invalid four-core CPU list: ${value}`);
	let mask = 0;
	for (let part in split(value, ',')) {
		let ends = split(part, '-'), lo = int(ends[0]), hi = int(ends[1] ?? ends[0]);
		if (lo > hi) die('Reversed CPU range');
		for (let cpu = lo; cpu <= hi; cpu++) mask |= 1 << cpu;
	}
	return mask;
}

export function cpulist(mask) {
	let cpus = [];
	for (let cpu = 0; cpu < 4; cpu++) if (mask & (1 << cpu)) push(cpus, cpu);
	return join(',', cpus);
}

export function taskstat(text) {
	// comm may contain spaces and ')'; fields after its last ')' are stable.
	let m = match(text ?? '', /^(\d+) \((.*)\) (.*)$/s);
	if (!m) return null;
	let f = split(trim(m[3]), /\s+/);
	if (length(f) < 20) return null;
	return { tid: int(m[1]), name: m[2], start: f[19], user: !(int(f[6]) & 0x200000), ppid: int(f[1]) };
}

export function parse_map(text) {
	if (index(text ?? '', '# airoha-napi-map 1\n') != 0) die('Missing/unsupported Airoha NAPI map');
	let rows = [], ids = {};
	for (let line in split(text, '\n')) {
		if (!length(line) || substr(line, 0, 1) == '#') continue;
		let f = split(trim(line), /\s+/);
		if (length(f) != 9 || !match(line, /^[01] (rx|tx) [0-9]+ -?[0-9]+ -?[0-9]+ [0-9]+ [0-9]+ [0-9]+ [0-9]+$/))
			die('Malformed NAPI map row');
		let r = { qdma: int(f[0]), kind: f[1], queue: int(f[2]), bank: int(f[3]), irq: int(f[4]),
			pid: int(f[5]), descriptors: int(f[6]), buffer_bytes: int(f[7]), queued: int(f[8]) };
		let id = `${r.qdma}:${r.kind}:${r.queue}`;
		if (ids[id] || r.bank < -1 || r.bank > 3 || r.queue > (r.kind == 'rx' ? 31 : 1))
			die('Duplicate/out of range NAPI map row');
		ids[id] = true;
		push(rows, r);
	}
	if (length(rows) != 68) die('Incomplete QDMA map (expected 64 RX + 4 TX completion instances)');
	return rows;
}

export function policy_masks(cfg) {
	let masks = {};
	for (let key in ['wan_cpu', 'lan_cpu', 'special_cpu', 'other_irq_cpu']) {
		if (!match(cfg[key] ?? '', /^[0-3]$/)) die(`Invalid ${key}`);
		masks[key] = 1 << int(cfg[key]);
	}
	masks.service_cpus = cpumask(cfg.service_cpus);
	masks.unbound_cpus = cpumask(cfg.unbound_cpus);
	if (masks.wan_cpu == masks.lan_cpu || masks.special_cpu & (masks.wan_cpu | masks.lan_cpu) ||
	    masks.other_irq_cpu & (masks.wan_cpu | masks.lan_cpu) ||
	    masks.service_cpus & (masks.wan_cpu | masks.lan_cpu) || masks.unbound_cpus & (masks.wan_cpu | masks.lan_cpu))
		die('Housekeeping/special/service masks must exclude distinct WAN/LAN CPUs');
	return masks;
}

export function make_plan(cfg, snap, stage) {
	if (index(['network', 'services', 'full'], stage) < 0) die('Stage must be network, services or full');
	if (!match(snap.board ?? '', /^nokia,xg-040g-md-ubi(-usb-sfp)?$/)) die('Unsupported board; no affinity changed');
	if (snap.online != 15) die('All four CPUs must be online');
	let masks = policy_masks(cfg);
	let ops = [], warnings = [], seen_irqs = {}, seen_tasks = {};
	function add(op) { if (op.was == null) die(`Missing value: ${op.id}`); push(ops, op); }
	for (let r in snap.rows) {
		if (r.bank == -1) { if (r.kind != 'rx') die('Unrouted TX completion'); continue; }
		let irq = snap.irqs[`${r.irq}`], task = snap.tasks[`${r.pid}`];
		let index = r.qdma * 4 + r.bank;
		if (!irq || irq.action != `airoha_eth.${index}`) die(`IRQ identity mismatch for QDMA${r.qdma}/${r.bank}`);
		if (!task || task.user || !match(task.name, /^napi\/qdma_eth/)) die('Threaded NAPI missing; refusing IRQ-only policy');
		if (r.kind == 'tx' && r.bank != 0) die('Unexpected TX completion bank');
		let want = r.bank >= 2 ? masks.special_cpu : masks[r.qdma ? 'wan_cpu' : 'lan_cpu'];
		if (!seen_irqs[index]) {
			add({ id: `irq:${r.irq}`, kind: 'irq', irq: r.irq, action: irq.action, was: irq.mask, want });
			seen_irqs[index] = true;
		}
		if (seen_tasks[r.pid]) die('Duplicate NAPI thread');
		seen_tasks[r.pid] = true;
		add({ id: `task:${r.pid}:${task.start}`, kind: 'task', tid: r.pid, start: task.start,
			role: 'napi', was: task.mask, want });
	}
	if (length(keys(seen_irqs)) != 8) die('Expected all eight QDMA IRQs');
	if (!length(snap.queues)) die('No Airoha receive queues discovered');
	for (let q in snap.queues) add({ id: q.path, kind: q.kind, path: q.path, was: q.value, want: 0 });
	if (stage != 'network') {
		if (!snap.tasks['1']?.user) die('Cannot identify userspace init');
		for (let id, irq in snap.irqs) {
			if (!irq.housekeeping) continue;
			add({ id: `irq:${id}`, kind: 'irq', irq: int(id), action: irq.action, was: irq.mask, want: masks.other_irq_cpu });
		}
		add({ id: '/proc/irq/default_smp_affinity', kind: 'mask', path: '/proc/irq/default_smp_affinity',
			was: snap.default_irq, want: masks.other_irq_cpu });
		for (let id, t in snap.tasks) {
			if (!t.user) continue;
			if (t.mask != snap.online && t.mask != masks.service_cpus) {
				push(warnings, `Preserved custom affinity of ${id} (${t.name}): ${cpulist(t.mask)}`);
				continue;
			}
			// Already-restricted tasks are only tracked if previously owned; the
			// transaction merger supplies that entry on reapply.
			if (t.mask != snap.online) continue;
			add({ id: `task:${id}:${t.start}`, kind: 'task', tid: int(id), start: t.start,
				role: 'service', was: t.mask, want: masks.service_cpus });
		}
		if (snap.tasks['1'].mask != snap.online && snap.tasks['1'].mask != masks.service_cpus)
			die('Init has a custom affinity; cannot safely establish service inheritance');
	}
	if (stage == 'full') {
		if (snap.unbound == null) die('Unbound workqueue mask unavailable');
		add({ id: '/sys/devices/virtual/workqueue/cpumask', kind: 'mask',
			path: '/sys/devices/virtual/workqueue/cpumask', was: snap.unbound, want: masks.unbound_cpus });
	}
	return { stage, ops, warnings, masks, online: snap.online };
}

export function restore(io, state) {
	let conflicts = [], failed = [];
	// Include surviving children born while procd was restricted. Affinity has
	// no provenance; a newly set *identical* mask cannot be distinguished from
	// inheritance. Different custom masks are always left alone.
	if (state.service_from != null) {
		for (let id, t in io.tasks()) {
			if (!t.user || +t.start < state.service_from || t.mask != state.service_mask) continue;
			let op = { id: `task:${id}:${t.start}`, kind: 'task', tid: int(id), start: t.start,
				role: 'service', was: state.online, want: state.service_mask };
			if (state.preexisting?.[op.id]) continue;
			if (!length(filter(state.ops, (o) => o.id == op.id))) push(state.ops, op);
		}
	}
	for (let op in reverse(slice(state.ops))) {
		let value = io.read(op);
		if (value == null) continue; // vanished PID/device; never target a reused PID
		if (value != op.want) {
			if (value != op.was) push(conflicts, op.id);
			continue;
		}
		try { io.write(op, op.was); if (io.read(op) != op.was) die('Restore readback failed'); }
		catch (e) { push(failed, op.id); }
	}
	state.conflicts = conflicts;
	state.failed = failed;
	state.active = false;
	state.restored = !length(failed);
	io.save(state);
	return { restored: state.restored, conflicts, failed };
}

export function apply(io, plan, state) {
	// Keep the first value for rollback over interface-triggered reapplications.
	let indexed = {};
	for (let op in state.ops) indexed[op.id] = op;
	for (let op in plan.ops) {
		let old = indexed[op.id];
		if (old) {
			if (op.was != old.want && op.was != old.was) die(`Externally changed affinity: ${op.id}`);
			op.was = old.was;
		}
		indexed[op.id] = op;
	}
	state.ops = values(indexed);
	state.active = true;
	state.restored = false;
	io.save(state); // write-ahead journal, including yet-to-be-applied operations
	try {
		for (let op in state.ops) {
			let value = io.read(op);
			if (value == null && op.role == 'service') continue;
			if (value == null && op.kind == 'task' && !length(filter(plan.ops, (p) => p.id == op.id))) continue;
			if (value == null) die(`Object disappeared: ${op.id}`);
			if (value != op.was && value != op.want) die(`Concurrent affinity change: ${op.id}`);
			if (value != op.want) io.write(op, op.want);
			if (io.read(op) != op.want && !(op.role == 'service' && io.read(op) == null)) die(`Readback failed: ${op.id}`);
			if (op.kind == 'irq' && io.effective(op) != op.want) die(`IRQ effective affinity mismatch: ${op.id}`);
		}
	} catch (e) {
		let result = restore(io, state);
		die(`${e}\nRollback: ${sprintf('%J', result)}`);
	}
	return state;
}
