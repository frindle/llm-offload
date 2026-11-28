# TASK: plex-newznab-tv-search-params

## Confirmed defect (observed, not suspected)

Two problems in `tiering/offload/newznab.py`.

1. TV searches find no episodes. `NewznabClient.search_indexer` sends the title (`q`), `imdbid` and `tvdbid` together on a TV search, and the indexer returns nothing for that mix. The indexer's tv-search takes `tvdbid` with `season` and `ep` (what Sonarr sends). Movie searches work.
2. `_clean`, used for title matching, turns an apostrophe into a word break and keeps `&` as a break, but release names drop apostrophes (`Greys.Anatomy`, `Bobs.Burgers`) and spell out `&` as `and` (`Law.and.Order`), so those releases do not match their titles.

## Entry point

`tiering/offload/newznab.py` `NewznabClient.search_indexer` and `_clean`.

## Required change

- A TV search (season given, or a tvdb id given) sends `tvdbid` plus season and episode, sends the title only when there is no tvdb id, and never sends `imdbid`. Movie searches are unchanged.
- Title cleaning drops apostrophes (straight, curly and backtick) without leaving a gap, and reads `&` as `and`. Everything else about it stays.

## Scope

Only edit `tiering/offload/newznab.py`. Do not edit the tests.
