# Memecoin Desk – runbook (used by the scheduled Claude session)

Dashboard: https://claude.ai/artifact/5kq5CkFPBGW8z5ESCjNJNd  (db doc `desk/state`, trigger id in `desk/config`)

The full engine state (cash, positions, closed trades, dedupe memory) lives in the dashboard db doc
`desk/state` under the `engine` field. Scheduled sessions cannot push to GitHub, so git is code only.

Each hourly run (every hour, 24/7, at :02):
1. `git fetch origin claude/relaxed-fermat-rvzszo && git checkout claude/relaxed-fermat-rvzszo && git pull --ff-only`
2. ArtifactData `get` desk/state with `out_dir` memedesk/state/db -> file memedesk/state/db/desk/state.json, note version V.
3. Five foreground blocks: block 1 `python3 memedesk/desk.py --import memedesk/state/db/desk/state.json --minutes 9`,
   blocks 2-5 `python3 memedesk/desk.py --minutes 9`. After EACH block: ArtifactData `set` desk/state from
   `memedesk/state/snapshot.json` with if_version V (new V from the result).
4. No git commit or push.

Reset (wipe paper account): `python3 memedesk/desk.py --reset --once`, then push snapshot.json to desk/state.
Jupiter key: network secret for api.jup.ag on the "trading-signals" environment (proxy injects it).
Optional keys (env vars): BIRDEYE_API_KEY (paid plan), RUGCHECK_API_KEY.
Live trading is disabled in code (`mode` must be `paper`).
