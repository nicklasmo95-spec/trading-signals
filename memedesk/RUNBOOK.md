# Night Desk – runbook (used by the scheduled Claude session)

Dashboard: https://claude.ai/artifact/5kq5CkFPBGW8z5ESCjNJNd  (db doc `desk/state`, trigger id in `desk/config`)

Each hourly run (23:00–07:00 Europe/Copenhagen):
1. `git fetch origin claude/relaxed-fermat-rvzszo && git checkout claude/relaxed-fermat-rvzszo && git pull --ff-only`
2. `python3 memedesk/desk.py --minutes 50`
3. `git add memedesk/state && git commit -m "Night desk: run $(date -u +%H:%M)" && git push -u origin claude/relaxed-fermat-rvzszo`
4. ArtifactData `get` collection `desk`, doc_id `state` → take `version`; then `set` with `file_path`
   `memedesk/state/snapshot.json` and `if_version` = that version (no version if the doc does not exist).

Reset (wipe paper account): `python3 memedesk/desk.py --reset --once`
Offline test: `python3 memedesk/desk.py --demo --minutes 1 --reset` (writes demo state; delete memedesk/state after).

Jupiter key: saved as a network secret for api.jup.ag on the "trading-signals" environment (proxy injects it).
Optional keys (env vars): BIRDEYE_API_KEY (paid plan), RUGCHECK_API_KEY.
Live trading is disabled in code (`mode` must be `paper`).
