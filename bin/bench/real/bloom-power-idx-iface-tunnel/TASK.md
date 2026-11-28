# TASK: bloom-power-idx-iface-tunnel

## Confirmed defect (observed, not suspected)

The power sampler's network accounting counts flows on `idx-22`, the name `nettop` prints when an interface index no longer exists (a socket left on a torn-down tunnel), as traffic on the wire. `is_tunnel_iface("idx-22")` returns False. Those flows must be treated like any other tunnel interface: not "the wire", but still attributed to their process.

## Entry point

`lib/bloom_power_lib.py`, the tunnel-interface pattern (`NET_TUNNEL_IFACE_RE`) used by `is_tunnel_iface`.

## Required change

Make interface names of the form `idx-` followed by digits count as tunnel interfaces. `idx-` with no digits, real interface names such as `en0` or `en12`, the empty string and None stay non-tunnel. Everything else about the pattern stays as it is.

## Scope

Only edit `lib/bloom_power_lib.py`. Do not edit the tests.
