#!/usr/bin/env python3
"""Memecoin Desk - 6-agent Solana memecoin desk (paper first).

Agents (hard handoffs): Scout -> Filter -> Analyst -> Executor -> Risk -> Logger

Data:
  DexScreener   (no key)  new pairs, price, liq, mcap, volume, txns
  RugCheck      (no key, optional RUGCHECK_API_KEY)  mint/freeze, LP lock, holders, insiders, creator
  Birdeye       (BIRDEYE_API_KEY, optional)  unique buyers 1h, holder count, smart-money proxy
  Jupiter       (JUPITER_API_KEY, optional)  realistic fill quotes in paper mode / live swaps later

Usage:
  python3 memedesk/desk.py --minutes 55            # run a session, state persists in memedesk/state/
  python3 memedesk/desk.py --once                  # one scan cycle
  python3 memedesk/desk.py --demo --minutes 1      # offline simulated market (no network) for UI tests
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "desk.json"
SNAPSHOT_FILE = STATE_DIR / "snapshot.json"   # compact doc pushed to the artifact db

UA = {"User-Agent": "memedesk/1.0", "Accept": "application/json"}
SOL_USDC_PRICE_FALLBACK = 1.0


# ---------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_json(url: str, headers: dict | None = None, timeout: int = 12):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt * 3)
                continue
            if e.code in (404, 400):
                return None
            time.sleep(1 + attempt)
        except Exception:
            time.sleep(1 + attempt)
    return None


def f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    eq = CONFIG["start_equity_usd"]
    return {
        "mode": CONFIG["mode"],
        "started": now_iso(),
        "start_equity": eq,
        "cash": eq,
        "positions": {},          # mint -> position
        "closed": [],             # closed trades
        "seen": {},               # mint -> {first_seen, verdict, holders:[...]} (dedupe)
        "counters": {"scanned": 0, "killed": 0, "scored": 0, "passed": 0, "traded": 0, "reasons": 0},
        "kills_by_rule": {k: 0 for k in RULE_LABELS},
        "log": [],
        "equity_curve": [],
        "agents": {a: "" for a in AGENTS},
        "watch": [],              # top scored coins for ticker
    }


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # compact dedupe memory so the whole engine state fits in one dashboard db document (~200 KB)
    now = time.time()
    keep = {}
    for m, v in st["seen"].items():
        t, verdict = seen_entry(v)
        if verdict in HARD_RULES or verdict == "pass" or now - t < RECHECK_MIN * 60:
            keep[m] = [int(t), verdict]
    st["seen"] = dict(list(keep.items())[-1200:])
    st["log"] = st["log"][-120:]
    st["equity_curve"] = st["equity_curve"][-600:]
    STATE_FILE.write_text(json.dumps(st, separators=(",", ":")))
    snap = snapshot(st)
    snap["engine"] = st   # full state travels with the dashboard doc; the next session imports it
    SNAPSHOT_FILE.write_text(json.dumps(snap, separators=(",", ":")))


def import_state(path: str) -> bool:
    """Load engine state from a dashboard db dump (desk/state). Returns False if it holds none."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False
    eng = doc.get("engine") if isinstance(doc, dict) else None
    if not isinstance(eng, dict) or "cash" not in eng:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(eng, separators=(",", ":")))
    return True


AGENTS = ["scout", "filter", "analyst", "executor", "risk", "logger"]
RULE_LABELS = {
    "liq_vol": "Liq <$40k · 1h vol <$50k",
    "mint_freeze": "Mint/freeze not revoked",
    "lp": "LP not burned/locked",
    "top10": "Top 10 holders >25%",
    "dev": "Dev >3% · past rugs",
    "bundled": "Bundled launch >10%",
    "pumped": "Already +80% in 1h",
    "age_mcap": "Outside age/mcap window",
}


def seen_entry(v) -> tuple[float, str]:
    """(epoch_seconds, verdict) from a compact [t, verdict] entry or a legacy {"t": iso, "verdict"} dict."""
    if isinstance(v, list):
        return float(v[0]), v[1]
    return datetime.fromisoformat(v["t"].replace("Z", "+00:00")).timestamp(), v["verdict"]


HARD_RULES = {"mint_freeze", "lp", "top10", "dev", "bundled"}
RECHECK_MIN = 60


def log(st: dict, agent: str, msg: str, sym: str = "", kind: str = "info") -> None:
    st["log"].append({"t": now_iso(), "agent": agent, "msg": msg, "sym": sym, "kind": kind})
    st["agents"][agent] = (f"${sym} " if sym else "") + msg
    st["counters"]["reasons"] += 1
    print(f"{datetime.now().strftime('%H:%M:%S')} {agent.upper():8} {('$'+sym) if sym else ''} {msg}", flush=True)


def equity(st: dict) -> float:
    return st["cash"] + sum(p["qty"] * p["last"] for p in st["positions"].values())


# ---------------------------------------------------------------- data sources
class Market:
    """Live data. Every method returns None/[] on failure so the desk never crashes."""

    DS = "https://api.dexscreener.com"

    def new_solana_tokens(self) -> list[str]:
        mints: list[str] = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/community-takeovers/latest/v1"):
            data = http_json(self.DS + path) or []
            for t in data if isinstance(data, list) else []:
                if t.get("chainId") == "solana" and t.get("tokenAddress"):
                    mints.append(t["tokenAddress"])
        # GeckoTerminal: pools trending in the last 1h/6h + newest pools (free, no key, ~30 req/min)
        gt = "https://api.geckoterminal.com/api/v2/networks/solana"
        for path in ("trending_pools?duration=1h&page=1", "trending_pools?duration=1h&page=2",
                     "trending_pools?duration=6h&page=1", "trending_pools?duration=6h&page=2",
                     "new_pools?page=1", "new_pools?page=2", "new_pools?page=3"):
            data = http_json(f"{gt}/{path}") or {}
            for pool in data.get("data") or []:
                tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if tid.startswith("solana_"):
                    mints.append(tid[7:])
        # Pump.fun graduations / Raydium & PumpSwap fresh pairs via search
        for q in ("pump", "raydium", "pumpswap"):
            data = http_json(f"{self.DS}/latest/dex/search?q={q}") or {}
            for p in data.get("pairs") or []:
                if p.get("chainId") == "solana":
                    mints.append(p["baseToken"]["address"])
        return list(dict.fromkeys(mints))

    def pairs(self, mints: list[str]) -> dict[str, dict]:
        """Best (highest-liquidity) pair per mint. Batches of 30."""
        best: dict[str, dict] = {}
        for i in range(0, len(mints), 30):
            chunk = ",".join(mints[i:i + 30])
            data = http_json(f"{self.DS}/tokens/v1/solana/{chunk}") or []
            for p in data if isinstance(data, list) else []:
                m = (p.get("baseToken") or {}).get("address")
                if not m:
                    continue
                if m not in best or f((p.get("liquidity") or {}).get("usd")) > f((best[m].get("liquidity") or {}).get("usd")):
                    best[m] = p
        return best

    def rug_report(self, mint: str) -> dict | None:
        h = {}
        if os.getenv("RUGCHECK_API_KEY"):
            h["Authorization"] = "Bearer " + os.environ["RUGCHECK_API_KEY"]
        return http_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", h, timeout=20)

    def birdeye_overview(self, mint: str) -> dict | None:
        key = os.getenv("BIRDEYE_API_KEY")
        if not key:
            return None
        d = http_json(f"https://public-api.birdeye.so/defi/token_overview?address={mint}",
                      {"X-API-KEY": key, "x-chain": "solana"})
        return (d or {}).get("data")

    def quote_fill(self, mint: str, usd: float, price: float, side: str) -> float:
        """Paper fill price. Uses Jupiter quote when a key is present, else price +/- slippage."""
        slip = CONFIG["paper_slippage_pct"] / 100
        key = os.getenv("JUPITER_API_KEY", "proxy-injected")  # real key is injected by the environment proxy
        if key and price > 0:
            # Real route quote for this USD size; price impact + 0.3% AMM fees replace the fixed slippage.
            q = http_json(f"https://api.jup.ag/swap/v1/quote?inputMint={USDC_MINT}&outputMint={mint}"
                          f"&amount={int(usd * 1e6)}&slippageBps=300", {"x-api-key": key})
            if q and q.get("outAmount"):
                slip = abs(f(q.get("priceImpactPct"))) + 0.003
        return price * (1 + slip) if side == "buy" else price * (1 - slip)


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class DemoMarket(Market):
    """Offline simulator so the pipeline and dashboard can be tested without network."""

    NAMES = ["MICRODUCK", "JEANPHIL", "DREAMS", "BUN", "CETS", "SIGMA", "PUMPKIN", "SAFEGEM", "HAMSTER", "JEETS",
             "DOOD", "DOGEAI", "MANIFEST", "FROGMAN", "BABYWIF", "BAGZ", "ELONCAT", "WAGMI", "SOLDOG", "GRITTY"]

    def __init__(self):
        self.tokens: dict[str, dict] = {}
        self.t0 = time.time()

    def _mk(self):
        i = len(self.tokens)
        mint = f"DEMO{i:04d}" + "x" * 36
        sym = random.choice(self.NAMES) + (str(i) if i >= len(self.NAMES) else "")
        good = random.random() < 0.12
        self.tokens[mint] = {
            "sym": sym, "price": random.uniform(0.0005, 0.02), "drift": random.uniform(0.002, 0.02) if good else random.uniform(-0.02, 0.006),
            "liq": random.uniform(45e3, 400e3) if good else random.uniform(5e3, 120e3),
            "mcap": random.uniform(4e5, 8e6), "age_h": random.uniform(0.5, 60),
            "vol1h": random.uniform(60e3, 500e3) if good else random.uniform(5e3, 150e3),
            "mint": random.random() < 0.15 and not good, "freeze": random.random() < 0.1 and not good,
            "lp": random.uniform(95, 100) if good or random.random() < 0.6 else random.uniform(0, 60),
            "top10": random.uniform(8, 22) if good else random.uniform(10, 60),
            "dev": random.uniform(0, 2) if good else random.uniform(0, 8), "insider": random.uniform(0, 6) if good else random.uniform(0, 25),
            "chg1h": random.uniform(5, 40) if good else random.uniform(-30, 150),
            "buys": random.randint(300, 1500) if good else random.randint(20, 800), "sells": random.randint(100, 600),
            "holders": random.randint(300, 3000),
        }
        return mint

    def new_solana_tokens(self):
        for _ in range(random.randint(8, 25)):
            self._mk()
        return list(self.tokens)[-40:]

    def pairs(self, mints):
        out = {}
        for m in mints:
            t = self.tokens.get(m)
            if not t:
                continue
            t["price"] *= max(0.5, 1 + random.gauss(t["drift"], 0.04))
            t["holders"] += random.randint(-5, 40)
            out[m] = {"baseToken": {"address": m, "symbol": t["sym"]}, "priceUsd": str(t["price"]),
                      "liquidity": {"usd": t["liq"]}, "marketCap": t["mcap"], "fdv": t["mcap"],
                      "pairCreatedAt": int((time.time() - t["age_h"] * 3600) * 1000),
                      "volume": {"h1": t["vol1h"]}, "priceChange": {"h1": t["chg1h"], "m5": random.uniform(-5, 10)},
                      "txns": {"h1": {"buys": t["buys"], "sells": t["sells"]}}, "url": "", "dexId": "raydium"}
        return out

    def rug_report(self, mint):
        t = self.tokens[mint]
        return {"mintAuthority": "x" if t["mint"] else None, "freezeAuthority": "x" if t["freeze"] else None,
                "markets": [{"lp": {"lpLockedPct": t["lp"]}}],
                "topHolders": [{"pct": t["top10"] / 10, "insider": False} for _ in range(10)],
                "creatorBalance": 0, "creator_pct": t["dev"], "insider_pct": t["insider"],
                "totalHolders": t["holders"], "risks": []}

    def birdeye_overview(self, mint):
        return None

    def quote_fill(self, mint, usd, price, side):
        slip = CONFIG["paper_slippage_pct"] / 100
        return price * (1 + slip) if side == "buy" else price * (1 - slip)


# ---------------------------------------------------------------- agents
def scout(st, mkt) -> dict[str, dict]:
    def skip(m):  # hard rug flags are permanent; soft kills (age/liq/vol/pump) get re-checked after RECHECK_MIN
        v = st["seen"].get(m)
        if not v:
            return False
        t, verdict = seen_entry(v)
        if verdict in HARD_RULES or verdict == "pass":
            return True
        return (time.time() - t) / 60 < RECHECK_MIN
    mints = [m for m in mkt.new_solana_tokens() if not skip(m) and m not in st["positions"]]
    pairs = mkt.pairs(mints) if mints else {}
    st["counters"]["scanned"] += sum(1 for m in pairs if m not in st["seen"])
    if pairs:
        sym = next(iter(pairs.values()))["baseToken"].get("symbol", "?")
        st["agents"]["scout"] = f"${sym} new pair · {len(pairs)} this cycle"
    return pairs


def rug_metrics(rep: dict | None) -> dict:
    """Normalise a RugCheck report into the fields the Filter needs."""
    if not rep:
        return {}
    risks = " ".join((r.get("name") or "").lower() for r in rep.get("risks") or [])
    lp_pcts = [f((mk.get("lp") or {}).get("lpLockedPct")) for mk in rep.get("markets") or []]
    top = rep.get("topHolders") or []
    top10 = sum(f(h.get("pct")) for h in top[:10])
    insider = rep.get("insider_pct")
    if insider is None:
        insider = sum(f(h.get("pct")) for h in top if h.get("insider"))
    supply = f((rep.get("token") or {}).get("supply"))
    creator_pct = rep.get("creator_pct")
    if creator_pct is None:
        creator_pct = (f(rep.get("creatorBalance")) / supply * 100) if supply else 0.0
    return {
        "mint_auth": bool(rep.get("mintAuthority")) or "mint authority" in risks,
        "freeze_auth": bool(rep.get("freezeAuthority")) or "freeze authority" in risks,
        "lp_locked": max(lp_pcts) if lp_pcts else (0.0 if "lp unlocked" in risks else 100.0),
        "top10": top10, "insider": insider, "creator_pct": creator_pct,
        "past_rugs": "rugged" in risks or "rug" in risks,
        "holders": int(f(rep.get("totalHolders"))),
    }


def filt(st, mkt, pairs: dict[str, dict]) -> list[dict]:
    r = CONFIG["filter"]
    survivors = []
    for mint, p in pairs.items():
        sym = p["baseToken"].get("symbol", "?")
        liq = f((p.get("liquidity") or {}).get("usd"))
        vol1h = f((p.get("volume") or {}).get("h1"))
        mcap = f(p.get("marketCap") or p.get("fdv"))
        age_h = (time.time() * 1000 - f(p.get("pairCreatedAt"), time.time() * 1000)) / 3.6e6
        chg1h = f((p.get("priceChange") or {}).get("h1"))
        cand = {"mint": mint, "sym": sym, "pair": p, "liq": liq, "vol1h": vol1h, "mcap": mcap, "age_h": age_h, "chg1h": chg1h}

        reason = rule = None
        if not (r["min_age_min"] / 60 <= age_h <= r["max_age_h"]) or not (r["min_mcap"] <= mcap <= r["max_mcap"]):
            rule, reason = "age_mcap", f"age {age_h:.1f}h / mcap ${mcap/1e3:.0f}k outside window"
        elif liq < r["min_liq"] or vol1h < r["min_vol_1h"]:
            rule, reason = "liq_vol", f"liq ${liq/1e3:.0f}k" if liq < r["min_liq"] else f"1h vol ${vol1h/1e3:.0f}k"
        elif chg1h > r["max_pump_1h_pct"]:
            rule, reason = "pumped", f"+{chg1h:.0f}% in 1h"
        else:
            rm = rug_metrics(mkt.rug_report(mint))
            cand["rug"] = rm
            if not rm:
                rule, reason = "mint_freeze", "no RugCheck report (unverifiable)"
            elif rm["mint_auth"] or rm["freeze_auth"]:
                rule, reason = "mint_freeze", "mint not revoked" if rm["mint_auth"] else "freeze not revoked"
            elif rm["lp_locked"] < r["min_lp_locked_pct"]:
                rule, reason = "lp", f"LP {rm['lp_locked']:.0f}% locked"
            elif rm["top10"] > r["max_top10_pct"]:
                rule, reason = "top10", f"top10 {rm['top10']:.0f}%"
            elif rm["creator_pct"] > r["max_dev_pct"] or rm["past_rugs"]:
                rule, reason = "dev", "dev has past rugs" if rm["past_rugs"] else f"dev holds {rm['creator_pct']:.1f}%"
            elif rm["insider"] > r["max_bundled_pct"]:
                rule, reason = "bundled", f"bundled launch {rm['insider']:.0f}%"

        recheck = mint in st["seen"]
        st["seen"][mint] = [int(time.time()), rule or "pass"]
        if rule:
            if recheck and rule in ("age_mcap", "liq_vol", "pumped"):
                continue          # still not ready: silent, already counted once
            st["counters"]["killed"] += 1
            st["kills_by_rule"][rule] += 1
            log(st, "filter", f"killed · {reason}", sym, "kill")
        else:
            survivors.append(cand)
    return survivors


def analyst(st, mkt, cands: list[dict]) -> list[dict]:
    w = CONFIG["analyst"]["weights"]
    passed = []
    for c in cands:
        p = c["pair"]
        tx = (p.get("txns") or {}).get("h1") or {}
        buys, sells = f(tx.get("buys")), f(tx.get("sells"))
        be = mkt.birdeye_overview(c["mint"]) or {}
        uniq = f(be.get("uniqueWallet1h"), buys * 0.6)          # proxy without Birdeye
        holders_now = int(f(be.get("holder"), c.get("rug", {}).get("holders", 0)))
        m5 = f((p.get("priceChange") or {}).get("m5"))

        s = {
            "momentum": clamp((c["chg1h"] / 40) * 0.7 + (m5 / 10) * 0.3),
            "unique_buyers": clamp(uniq / 500),
            "buy_sell": clamp(((buys / max(sells, 1)) - 1) / 1.5),
            "holder_growth": clamp(holders_now / max(c["age_h"], 0.5) / 150),
            "liq_mcap": clamp((c["liq"] / max(c["mcap"], 1)) / 0.15),
            "smart_wallets": clamp(f(be.get("smartMoneyCount"), 0) / 5) if be else None,
        }
        active = {k: v for k, v in s.items() if v is not None}
        wsum = sum(w[k] for k in active)
        score = round(100 * sum(w[k] * v for k, v in active.items()) / wsum)
        c.update(score=score, parts={k: round(v * 100) for k, v in active.items()})
        st["counters"]["scored"] += 1
        st["watch"] = ([{"sym": c["sym"], "price": f(p.get("priceUsd")), "chg": c["chg1h"], "score": score}] + st["watch"])[:12]
        if score >= CONFIG["analyst"]["min_score"]:
            st["counters"]["passed"] += 1
            log(st, "analyst", f"passed · score {score}/100", c["sym"], "pass")
            passed.append(c)
        else:
            log(st, "analyst", f"rejected · score {score} < {CONFIG['analyst']['min_score']}", c["sym"], "reject")
    return sorted(passed, key=lambda c: -c["score"])


def executor(st, mkt, picks: list[dict]) -> None:
    ex = CONFIG["executor"]
    for c in picks:
        if len(st["positions"]) >= ex["max_open_positions"] or c["mint"] in st["positions"]:
            break
        eq = equity(st)
        usd = min(eq * ex["max_pct_per_trade"] / 100, st["cash"])
        if usd < ex["min_order_usd"]:
            log(st, "executor", "skipped · not enough cash", c["sym"], "info")
            break
        price = f(c["pair"].get("priceUsd"))
        if price <= 0:
            continue
        fill = mkt.quote_fill(c["mint"], usd, price, "buy")
        qty = usd / fill
        st["cash"] -= usd
        st["positions"][c["mint"]] = {
            "sym": c["sym"], "mint": c["mint"], "entry": fill, "qty": qty, "cost": usd, "last": price,
            "peak": price, "stop": fill * (1 + CONFIG["risk"]["stop_pct"] / 100), "trailing": False,
            "opened": now_iso(), "score": c["score"], "url": c["pair"].get("url", ""),
        }
        st["counters"]["traded"] += 1
        log(st, "executor", f"bought · ${usd:.2f} · {ex['max_pct_per_trade']}% of stack · score {c['score']}", c["sym"], "buy")
        log(st, "risk", f"stop set {CONFIG['risk']['stop_pct']}% hard", c["sym"], "risk")


def risk(st, mkt) -> None:
    if not st["positions"]:
        st["agents"]["risk"] = "no open risk"
        return
    rk = CONFIG["risk"]
    live = mkt.pairs(list(st["positions"]))
    for mint, pos in list(st["positions"].items()):
        p = live.get(mint)
        if p:
            pos["last"] = f(p.get("priceUsd"), pos["last"])
        pos["peak"] = max(pos["peak"], pos["last"])
        gain = pos["last"] / pos["entry"] - 1
        if not pos["trailing"] and gain >= rk["trail_activate_pct"] / 100:
            pos["trailing"] = True
            log(st, "risk", f"trail armed at +{gain*100:.0f}%", pos["sym"], "risk")
        if pos["trailing"]:
            pos["stop"] = max(pos["stop"], pos["peak"] * (1 - rk["trail_distance_pct"] / 100))
        held_h = (time.time() - datetime.fromisoformat(pos["opened"].replace("Z", "+00:00")).timestamp()) / 3600
        why = None
        if pos["last"] <= pos["stop"]:
            why = "trail → sold" if pos["trailing"] else "stop → sold"
        elif held_h >= rk["max_hold_h"]:
            why = "time stop → sold"
        if why:
            fill = mkt.quote_fill(mint, pos["qty"] * pos["last"], min(pos["last"], pos["stop"]) if not pos["trailing"] else pos["last"], "sell")
            proceeds = pos["qty"] * fill
            pnl = proceeds - pos["cost"]
            st["cash"] += proceeds
            st["closed"].append({**pos, "exit": fill, "closed": now_iso(), "pnl": round(pnl, 2),
                                 "pnl_pct": round((fill / pos["entry"] - 1) * 100, 1), "why": why})
            del st["positions"][mint]
            log(st, "risk", f"{why} · {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}", pos["sym"], "win" if pnl >= 0 else "loss")


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------- snapshot for the dashboard
def snapshot(st: dict) -> dict:
    eq = equity(st)
    wins = sum(1 for t in st["closed"] if t["pnl"] >= 0)
    pos = [{k: p[k] for k in ("sym", "entry", "last", "cost", "stop", "trailing", "opened", "score")} |
           {"pnl_pct": round((p["last"] / p["entry"] - 1) * 100, 1)} for p in st["positions"].values()]
    closed = [{k: t.get(k) for k in ("sym", "entry", "exit", "cost", "pnl", "pnl_pct", "why", "opened", "closed", "score")}
              for t in st["closed"][-60:]]
    pnls = [t["pnl"] for t in st["closed"]]
    return {
        "updated": now_iso(), "mode": st["mode"], "source": st.get("source", "live"), "started": st["started"],
        "start_equity": st["start_equity"], "equity": round(eq, 2), "cash": round(st["cash"], 2),
        "wins": wins, "losses": len(st["closed"]) - wins,
        "best": round(max(pnls), 2) if pnls else 0, "worst": round(min(pnls), 2) if pnls else 0,
        "counters": st["counters"], "kills_by_rule": st["kills_by_rule"], "rule_labels": RULE_LABELS,
        "agents": st["agents"], "positions": pos, "closed": closed,
        "log": st["log"][-60:], "equity_curve": st["equity_curve"][-300:], "watch": st["watch"][:10],
        "config": CONFIG,
    }


# ---------------------------------------------------------------- main loop
def cycle(st, mkt) -> None:
    pairs = scout(st, mkt)
    survivors = filt(st, mkt, pairs)
    picks = analyst(st, mkt, survivors)
    executor(st, mkt, picks)
    if not picks:
        st["agents"]["executor"] = st["agents"]["executor"] or "nothing passed yet"
    risk(st, mkt)
    st["agents"]["logger"] = f"{st['counters']['killed']} killed · {len(st['log'])} reasons in log"
    st["equity_curve"].append([now_iso(), round(equity(st), 2)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=55)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--demo", action="store_true", help="offline simulated market")
    ap.add_argument("--reset", action="store_true", help="wipe state and start from start_equity")
    ap.add_argument("--import", dest="imp", help="load engine state from a dashboard db dump of desk/state")
    a = ap.parse_args()
    if a.imp:
        print("imported state from dashboard" if import_state(a.imp) else "no engine state in dump; using repo state", flush=True)

    if CONFIG["mode"] != "paper":
        print("Live mode is not enabled in this build. Set mode=paper.", file=sys.stderr)
        return 2
    if a.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
    random.seed()
    mkt = DemoMarket() if a.demo else Market()
    st = load_state()
    st["source"] = "demo" if a.demo else "live"
    end = time.time() + a.minutes * 60
    scan_every, risk_every = CONFIG["scan_interval_s"], CONFIG["risk_interval_s"]
    if a.demo:
        scan_every, risk_every = 3, 1
    last_scan = 0.0
    while True:
        t = time.time()
        if t - last_scan >= scan_every:
            cycle(st, mkt)
            last_scan = t
            save_state(st)
        else:
            risk(st, mkt)
        if a.once or time.time() >= end:
            break
        time.sleep(risk_every)
    save_state(st)
    s = snapshot(st)
    print(f"\nEquity ${s['equity']:.2f} (start ${s['start_equity']:.2f}) · {s['wins']}W/{s['losses']}L · "
          f"{s['counters']['scanned']} scanned · {s['counters']['killed']} killed · {s['counters']['traded']} traded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
