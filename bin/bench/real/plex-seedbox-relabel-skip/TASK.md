# TASK: plex-seedbox-relabel-skip

## Confirmed defect (observed, not suspected)

When Sonarr or Radarr reports an import whose download came from the remote (seedbox) download client, `handle_upgrade_import` and `handle_import_relabel` in `arr-webhook.py` still ask the HOME Deluge about that download's hash (the relabel-to-base lookup). The home Deluge does not have that torrent, so it prints a KeyError traceback each time (about 10 a day in the error digest).

## Entry point

`arr-webhook.py` `handle_upgrade_import` and `handle_import_relabel`. The module already has a helper that says whether an import came from a remote download client.

## Required change

For an import that came from a remote download client, never call the relabel lookup for the download id, in either function. Imports from the home client behave exactly as before, and the rest of the upgrade handling (finding and superseding the old torrents) is unchanged.

## Scope

Only edit `arr-webhook.py`. The tests in `test_supersede_remote_import.py` already describe the expected behaviour and must not be edited.
