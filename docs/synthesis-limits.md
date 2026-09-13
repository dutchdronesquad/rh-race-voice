# Busy-race synthesis limits

Lap synthesis keeps at most four pending callouts plus one active job. A newer pending lap for the same pilot replaces the older one; otherwise overload drops the oldest pending lap. Expired jobs are skipped before synthesis and between segments. Skips are summarized in the logs at most once every five seconds. Race signals and other voice announcements are outside this lap limit.

Synthesis uses one executor worker and at most two ONNX compute threads (one on a single- or dual-core host). This is a conservative starting point, not a measured Raspberry Pi capacity guarantee. Pre-cache preparation shares that worker; prepare audio before racing. Validate CPU load and RH responsiveness on the target Pi before adjusting the defaults.
