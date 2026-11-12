#!/usr/bin/env python3
"""RETIRED 2026-10-08. This one-off 2026-10-06 experiment (does a local request cold-load an
advertised-but-unloaded model?) answered its question (public-demand eviction, see
docs/darkbloom-keepwarm.md) and crashed on a missing ~/.darkbloom/local.json after
`darkbloom start` rewrote it. It also drained and held the LIVE queue and bounced the provider, which
the event-driven bloom_control integration (docs/bloom-control.md) makes unsafe to run by hand.
The hermetic cold-load logic is covered by test-darkbloom-coldload.py. Original: *.retired-20261008."""
import sys
print(__doc__)
sys.exit(2)
