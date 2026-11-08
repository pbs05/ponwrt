# Airoha NPU identity and PPE diagnostic decoding

## Cached NPU identity

The NPU driver already queries the firmware version during probe. Cache its
successful response and expose read-only `fw_version` and `num_cores` device
attributes. Readers do not send new mailbox requests or touch registers.
A failed version query returns `ENODATA`; zero is a valid version. Core count
comes from the actual initialized driver array, not the host CPU count.
These are boot-time identity, not firmware health or flow-offload status.

On the previously deployed Nokia XG-040G-MD / AN7581 image, the attributes
returned `1456.62` and `8`. This observation predates the submission rebase.
The existing host fixture extracts the actual show functions and supplies
controlled device stubs. It does not validate a physical NPU.

## IPv6 PPE source address

IPv6 PPE entries store a source-MAC update-table selector and PPPoE session ID
in `src_mac_hi`, rather than four source address bytes. Print `smac_id` for
bound entries and `pppoe_sid` when encapsulation is enabled. Selector 15 means
preserve the original source MAC. No source address is guessed; destination,
IPv4 and native L2 bridge decoding are unchanged. The output is debugfs text,
not a stable machine ABI. No extra hardware lookup is performed.

## Submission boundary

Both kernel patches apply independently in sequence to the prepared Linux
6.18.55 source subset of PonWrt master `03c1345447`. This is not a new full
kernel build. Existing fixtures are retained; no new tests, firmware build,
flash or hardware access were performed for this submission.
