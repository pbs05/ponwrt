"""Read-only policy and numeric hook-order preflight for HG regression."""

import json


def check_qos_profile(raw, expected=None):
    """Archive observations; expectations come from config, never the result."""
    try:
        observed = json.loads(raw)
        if not isinstance(observed, dict):
            raise ValueError('profile must be an object')
    except (ValueError, TypeError):
        observed = {'mode': 'unknown'}
    result = {'runtime': observed, 'expected': expected,
              'checked': expected is not None}
    if expected is None:
        return result
    if expected.get('mode') not in ('priority', 'dscp'):
        raise ValueError('expected_qos.mode must be priority or dscp')
    if observed.get('mode') != expected['mode'] or observed.get('broken'):
        raise ValueError('QoS configuration preflight: running mode differs or driver reports a fault')
    if expected['mode'] == 'priority':
        if observed.get('priority_map') != [4, 0, 0, 0, 4, 0, 7, 7] + [4] * 8:
            raise ValueError('QoS configuration preflight: revised priority map not confirmed')
        if expected.get('map'):
            raise ValueError('DSCP map expectations require dscp mode')
    else:
        actual = observed.get('map')
        if not isinstance(actual, list) or len(actual) != 64 or any(type(q) is not int or not 0 <= q < 8 for q in actual):
            raise ValueError('QoS configuration preflight: complete DSCP map unavailable')
        for d, queue in expected.get('map', {}).items():
            if not str(d).isdigit() or not 0 <= int(d) <= 63 or type(queue) is not int or not 0 <= queue <= 7:
                raise ValueError('Invalid expected DSCP:queue pair')
            if actual[int(d)] != queue:
                raise ValueError(f'QoS configuration preflight: DSCP {d} expected Q{queue}, got Q{actual[int(d)]}')
    return result


def check_hook_order(chains, *, test_override=False):
    """Reject an incompatible setup; bridge 'filter' is -200, inet is 0.

    This checks this installation's named fw4 forwarding chains. The caller
    archives the full ruleset too; it is not proof of packet delivery/queuing.
    """
    entries = [x["chain"] for x in chains["nftables"] if "chain" in x]
    result = {}
    for family, test_prio in (("bridge", -205), ("inet", -150)):
        def priority(table):
            matches = [x for x in entries if x.get("family") == family
                       and x.get("table") == table and x.get("name") == "forward"
                       and x.get("hook") == "forward"]
            if len(matches) != 1 or type(matches[0].get("prio")) is not int:
                raise ValueError(f"Missing numeric {family} {table} forward priority")
            return matches[0]["prio"]

        policy, flow = priority("hg_qos"), priority("fw4")
        if policy >= flow:
            raise ValueError(f"Configuration error: {family} classification {policy} must precede fw4 flow add {flow}")
        if test_override and not policy < test_prio < flow:
            raise ValueError(f"Test setup error: need {family} policy {policy} < test {test_prio} < fw4 {flow}")
        result[family] = {"policy": policy, "fw4_forward": flow}
        if test_override:
            result[family]["test_classifier"] = test_prio
    return result
