#!/bin/bash
# Deliver durable queue events (log written by com.example.queue-event-log) to this session.
# Global cursor + per-session cursor; logic in ~/bin/queue-events-deliver.py.
# Prior version backed up in ~/.ollama-dispatch/backups/queue-events-20261101/.
exec python3 ~/bin/queue-events-deliver.py 2>/dev/null
