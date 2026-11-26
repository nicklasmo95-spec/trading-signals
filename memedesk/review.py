#!/usr/bin/env python3
"""Daily check of the Memecoin Desk (Danish report).

Usage: python3 memedesk/review.py <dump of dashboard db doc desk/state>

Counts only trades opened under the current risk rules (since BASELINE) and prints:
  STATUS: OK | ALERT_EQUITY | LOW_TRADES | REVIEW_READY  (one or more)
followed by a short report. At REVIEW_READY it adds the full Filter/Analyst analysis.
"""
import json
import sys
from datetime import datetime, timezone

BASELINE = "2026-11-26T10:02:00Z"      # new risk rules live (11:02 Copenhagen)
LOW_TRADES_CHECK = "2026-11-28T10:00:00Z"
LOW_TRADES_MIN = 5
EQUITY_ALERT = 110.0
REVIEW_AT = 40


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pf(trades):
    g = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    l = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return g / l if l else float("inf") if g else 0.0


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def fmt(x, d=1):
    return "–" if x is None else f"{x:.{d}f}"


def main(path):
    doc = json.load(open(path))
    eng = doc.get("engine") or {}
    closed = [t for t in eng.get("closed", []) if t.get("opened", "") >= BASELINE]
    now = datetime.now(timezone.utc)
    eq = doc.get("equity", 0)
    wins = [t for t in closed if t["pnl"] >= 0]
    losses = [t for t in closed if t["pnl"] < 0]

    status = []
    if eq < EQUITY_ALERT:
        status.append("ALERT_EQUITY")
    if now >= ts(LOW_TRADES_CHECK) and len(closed) < LOW_TRADES_MIN:
        status.append("LOW_TRADES")
    if len(closed) >= REVIEW_AT:
        status.append("REVIEW_READY")
    print("STATUS:", " ".join(status) or "OK")

    pnl = sum(t["pnl"] for t in closed)
    print(f"Equity ${eq:.2f} (start ${doc.get('start_equity', 150):.0f}) · opdateret {doc.get('updated')}")
    print(f"Handler med nye regler: {len(closed)}/{REVIEW_AT} · {len(wins)}W/{len(losses)}L · P/L ${pnl:+.2f} · "
          f"profit factor {fmt(pf(closed), 2)} · åbne nu {len(eng.get('positions', {}))}")
    if closed:
        worst = min(closed, key=lambda t: t["pnl"])
        best = max(closed, key=lambda t: t["pnl"])
        print(f"Bedste ${best['sym']} {best['pnl']:+.2f} · værste ${worst['sym']} {worst['pnl']:+.2f}")
    if "ALERT_EQUITY" in status:
        print(f"ADVARSEL: equity under ${EQUITY_ALERT:.0f}. Stop og gennemgå før flere handler.")
    if "LOW_TRADES" in status:
        print(f"FÅ HANDLER: under {LOW_TRADES_MIN} siden de nye regler. Aftalt tiltag: sænk filter.min_mcap 300000 -> 150000.")

    if "REVIEW_READY" not in status:
        return

    # ---------------- full review
    print("\n== ANALYSE (40+ handler) ==")
    no_best = sorted(closed, key=lambda t: -t["pnl"])[1:]
    print(f"Resultat uden bedste handel: ${sum(t['pnl'] for t in no_best):+.2f}")
    eqc = [p[1] for p in eng.get("equity_curve", [])]
    peak, dd = 0, 0
    for v in eqc:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak else 0)
    print(f"Max drawdown (sidste {len(eqc)} målinger): {dd*100:.1f}%")

    print("\nExit-årsager:")
    reasons = {}
    for t in closed:
        r = t.get("why", "?").split(" → ")[0].split(" (")[0]
        r = "liquidity drain" if r.startswith("liquidity") else r
        reasons.setdefault(r, []).append(t["pnl"])
    for r, v in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"  {r:18} {len(v):3} handler · ${sum(v):+.2f}")
    partial = [t for t in closed if t.get("partial")]
    print(f"Nåede +12% (halvt solgt): {len(partial)}/{len(closed)}")
    gb = [((t["peak"] / t["mid0"]) - 1) * 100 - t["pnl_pct"] for t in partial if t.get("mid0")]
    print(f"Gennemsnitlig tilbagegivet fra top (handler med halvt salg): {fmt(avg(gb))} procentpoint")

    print("\nAnalyst – vinder høj score oftere?")
    for lo, hi in ((70, 75), (75, 80), (80, 90), (90, 101)):
        b = [t for t in closed if lo <= (t.get("score") or 0) < hi]
        if b:
            wr = sum(1 for t in b if t["pnl"] >= 0) / len(b) * 100
            print(f"  score {lo}-{hi-1}: {len(b):3} handler · win rate {wr:4.0f}% · ${sum(t['pnl'] for t in b):+.2f}")

    print("\nGennemsnit ved køb, vindere vs tabere:")
    keys = [("score", "score"), ("age_h", "alder (t)"), ("mcap0", "mcap $"), ("chg1h0", "1h ændring %"), ("vol1h0", "1h volumen $")]
    for k, label in keys:
        print(f"  {label:14} vindere {fmt(avg([t.get(k) for t in wins]), 0):>10} · tabere {fmt(avg([t.get(k) for t in losses]), 0):>10}")
    parts = sorted({p for t in closed for p in (t.get("parts") or {})})
    if parts:
        print("\nScore-dele, vindere vs tabere (0-100):")
        for p in parts:
            print(f"  {p:14} vindere {fmt(avg([(t.get('parts') or {}).get(p) for t in wins]), 0):>4} · "
                  f"tabere {fmt(avg([(t.get('parts') or {}).get(p) for t in losses]), 0):>4}")

    print("\nKrav for live: >=40 handler, profit factor > 1.5, drawdown < 20%, positiv uden bedste handel.")


if __name__ == "__main__":
    main(sys.argv[1])
