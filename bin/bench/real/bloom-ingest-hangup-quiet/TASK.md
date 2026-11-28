# TASK: bloom-ingest-hangup-quiet

## Confirmed defect (observed, not suspected)

When a client hangs up before the server writes its reply (a probe with a short timeout on `/v1/hello`, about 25 times a day), the `Handler` in `ingest/server.py` prints a BrokenPipeError traceback each time, and the container error digest counts it as an error. The access line is already logged.

## Entry point

`ingest/server.py` class `Handler` (a `BaseHTTPRequestHandler` subclass).

## Required change

A client that hangs up (BrokenPipeError or ConnectionResetError while a request is being handled) must end the request quietly: no traceback, and the connection is marked to close. Any other exception must still propagate unchanged. Do not change how requests are routed or replied to.

## Scope

Only edit `ingest/server.py`. Do not edit the tests.
