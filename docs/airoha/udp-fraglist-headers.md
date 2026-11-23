# UDP fraglist output header propagation

GRO checks tuple, full DS field and TTL/hop limit before aggregation. Routing,
NAT and policy updates subsequently modify the aggregate header. UDP fraglist
segmentation must copy the final values into the surviving member headers;
otherwise one output batch can contain mixed markings or hop counts.

The first patch extends existing tuple repair with DSCP/ECN and writable-header
handling. The second includes IPv4 TTL and IPv6 hop limit, copying the final
value rather than applying another fixed decrement. IPv4 checksum updates are
incremental and IPv6 flow labels are preserved. Shared local/other-output
copies must remain unchanged. Allocation failures release the output list.

The existing first-child comparison exploits GRO homogeneity: an unchanged
batch returns in constant time. A changed batch uses the existing repair walk;
copy-on-write can still allocate and copy shared data. No unconditional payload
copy or extra per-member traversal is added. This is not a measured performance
guarantee relative to disabling GRO.

KUnit cases embedded in the patches cover DS fields, tuple rewriting, checksums,
shared data, failures, routed/local copies and GRO homogeneity. Enable
`CONFIG_UDP_FRAGLIST_KUNIT_TEST` only in a suitable KUnit build. Test hooks are
compiled out in ordinary production configurations. The fixtures are retained
from the local implementation and were not rerun during this export.

The patches apply in order to the prepared Linux 6.18.55 source subset of
PonWrt master `03c1345447`, independently of QoS/PPE features. Airoha's upstream
fraglist-GRO opt-in default is not changed. Reproduction needs rx-gro-list enabled
and aggregates that later undergo policy/routing changes. This proposal is not
a repair for PPE bidirectional DSCP learning. No new build or device validation
on the submission baseline is claimed.
