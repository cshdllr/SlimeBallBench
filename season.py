#!/usr/bin/env python3
"""
AI Slime Soccer — Season runner (round-robin league)
====================================================

Plays a round-robin league between the models listed in a season config, using
the same physics/decision engine as `simulate.py`. Writes one replay log per
match to `logs/` and a combined `season.json` (standings, per-match results,
aggregate per-model stats) that the website pages (index/model/stats) read.

Resumable: re-running skips fixtures already recorded in season.json, so a long
or paid season can be interrupted and continued without replaying games.

Usage:
    python season.py [--config season.config.json] [--out season.json]
                     [--no-verify] [--fresh]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from datetime import datetime, timezone

import simulate as sim


# ---------------------------------------------------------------------------
def provider_of(base_url: str) -> str:
    b = (base_url or "").lower()
    if "anthropic" in b:
        return "Anthropic"
    if "googleapis" in b:
        return "Gemini"
    if "openai.com" in b:
        return "OpenAI"
    return "Custom"


def normalize_models(cfg: dict) -> list[dict]:
    """Attach an id / abbrev / provider to each model entry."""
    models = []
    seen = set()
    for entry in cfg["models"]:
        mid = entry.get("id") or sim.slugify(entry["name"])
        if mid in seen:
            raise SystemExit(f"Duplicate model id '{mid}' — give models distinct names/ids.")
        seen.add(mid)
        e = dict(entry)
        e["id"] = mid
        e["provider"] = entry.get("provider") or provider_of(entry.get("base_url", ""))
        e.setdefault("abbrev", mid[:3].upper())
        models.append(e)
    return models


def verify_models(models: list[dict]) -> None:
    """One tiny probe call per model so a bad id/key fails fast, not mid-season."""
    from openai import OpenAI
    print("Verifying models...")
    bad = []
    for m in models:
        if m.get("policy") in ("heuristic", "random"):
            print(f"  {m['name']:24} (built-in {m['policy']}) — ok")
            continue
        env_name = m.get("api_key_env")
        key = (m.get("api_key")
               or (os.environ.get(env_name) if env_name else None)
               or os.environ.get("ANTHROPIC_API_KEY")
               or os.environ.get("OPENAI_API_KEY"))
        if not key:
            print(f"  {m['name']:24} NO KEY (env {env_name})", file=sys.stderr)
            bad.append(m["name"]); continue
        try:
            client = OpenAI(api_key=key, base_url=m.get("base_url") or None, timeout=30)
            client.chat.completions.create(
                model=m["model"], max_tokens=8,
                messages=[{"role": "user", "content": "Reply with one word: ok"}],
            )
            print(f"  {m['name']:24} {m['model']:24} — ok")
        except Exception as exc:
            print(f"  {m['name']:24} FAILED ({type(exc).__name__}: {str(exc)[:120]})", file=sys.stderr)
            bad.append(m["name"])
    if bad:
        raise SystemExit(f"\nAborting: {len(bad)} model(s) failed verification: {', '.join(bad)}\n"
                         f"Fix the model id / key / base_url, or pass --no-verify to skip this check.")


def build_fixtures(models: list[dict], fmt: str) -> list[tuple[dict, dict]]:
    pairs = list(itertools.combinations(models, 2))
    if fmt == "home_and_away":
        pairs += [(b, a) for (a, b) in pairs]
    return pairs


def play_fixture(home: dict, away: dict, settings: dict) -> dict:
    match_config = {
        "match_duration_seconds": settings["match_duration_seconds"],
        "decision_every_n_frames": settings["decision_every_n_frames"],
        "fps": settings["fps"],
        "player1": home,
        "player2": away,
    }
    out_path = sim.default_log_path(match_config)
    log = sim.run_match(match_config, out_path, quiet=True)
    summary = log["summary"]
    hg = summary["final_score"][home["name"]]
    ag = summary["final_score"][away["name"]]
    winner_id = "draw" if hg == ag else (home["id"] if hg > ag else away["id"])
    return {
        "id": f"{home['id']}__{away['id']}",
        "home": home["id"], "away": away["id"],
        "home_name": home["name"], "away_name": away["name"],
        "home_color": home.get("color", "#888"), "away_color": away.get("color", "#888"),
        "score": [hg, ag],
        "winner": summary["winner"],
        "winner_id": winner_id,
        "log": out_path,
        "played_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
def compute_standings(models, matches):
    rows = {m["id"]: dict(id=m["id"], name=m["name"], color=m.get("color", "#888"),
                          abbrev=m["abbrev"], provider=m["provider"],
                          played=0, w=0, d=0, l=0, gf=0, ga=0, gd=0, points=0)
            for m in models}
    for mt in matches:
        h, a = mt["home"], mt["away"]
        hg, ag = mt["score"]
        for tid, gf, ga in ((h, hg, ag), (a, ag, hg)):
            r = rows[tid]
            r["played"] += 1; r["gf"] += gf; r["ga"] += ga
        if hg > ag:
            rows[h]["w"] += 1; rows[a]["l"] += 1
        elif ag > hg:
            rows[a]["w"] += 1; rows[h]["l"] += 1
        else:
            rows[h]["d"] += 1; rows[a]["d"] += 1
    for r in rows.values():
        r["gd"] = r["gf"] - r["ga"]
        r["points"] = r["w"] * 3 + r["d"]
    table = sorted(rows.values(), key=lambda r: (r["points"], r["gd"], r["gf"]), reverse=True)
    for i, r in enumerate(table, 1):
        r["rank"] = i
    return table


def compute_aggregate_stats(models, matches):
    agg = {}
    for m in models:
        agg[m["id"]] = dict(
            games=0, goals_for=0, goals_against=0, ball_touches=0, saves=0,
            own_goals=0, jumps=0, distance=0.0, possession_sum=0.0,
            latency_weighted=0.0, decisions=0, tokens_in=0, tokens_out=0, total_cost=0.0,
        )
    name_to_id = {m["name"]: m["id"] for m in models}
    for mt in matches:
        players = mt["summary"]["players"]
        for name, st in players.items():
            a = agg[name_to_id[name]]
            a["games"] += 1
            a["goals_for"] += st["goals_for"]
            a["goals_against"] += st["goals_against"]
            a["ball_touches"] += st["ball_touches"]
            a["saves"] += st["saves"]
            a["own_goals"] += st["own_goals"]
            a["jumps"] += st["jumps"]
            a["distance"] += st["distance_travelled"]
            a["possession_sum"] += st["possession_pct"]
            a["decisions"] += st.get("decisions", 0) or 0
            lat = st.get("avg_decision_latency_ms")
            if lat is not None:
                a["latency_weighted"] += lat * (st.get("decisions", 0) or 0)
            tok = st.get("tokens", {})
            a["tokens_in"] += tok.get("prompt", 0)
            a["tokens_out"] += tok.get("completion", 0)
            a["total_cost"] += st.get("estimated_cost_usd", 0.0)
    # finalize derived fields
    out = {}
    for mid, a in agg.items():
        g = a["games"] or 1
        out[mid] = {
            "games": a["games"],
            "goals_for": a["goals_for"],
            "goals_against": a["goals_against"],
            "goal_diff": a["goals_for"] - a["goals_against"],
            "ball_touches": a["ball_touches"],
            "goals_per_touch": round(a["goals_for"] / a["ball_touches"], 3) if a["ball_touches"] else 0.0,
            "saves": a["saves"],
            "own_goals": a["own_goals"],
            "jumps": a["jumps"],
            "distance": round(a["distance"], 0),
            "avg_possession_pct": round(a["possession_sum"] / g, 1),
            "avg_latency_ms": round(a["latency_weighted"] / a["decisions"], 0) if a["decisions"] else None,
            "tokens_in": a["tokens_in"],
            "tokens_out": a["tokens_out"],
            "total_cost_usd": round(a["total_cost"], 4),
        }
    return out


def write_season(out_path, cfg, models, matches):
    data = {
        "season_name": cfg.get("season_name", "AI Slime Soccer League"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "match_duration_seconds": cfg["match_duration_seconds"],
            "decision_every_n_frames": cfg["decision_every_n_frames"],
            "fps": cfg["fps"],
            "format": cfg.get("format", "round_robin"),
        },
        "models": [{"id": m["id"], "name": m["name"], "color": m.get("color", "#888"),
                    "abbrev": m["abbrev"], "provider": m["provider"],
                    "model": m.get("model") or m.get("policy", "")}
                   for m in models],
        "matches": matches,
        "standings": compute_standings(models, matches),
        "aggregate_stats": compute_aggregate_stats(models, matches),
    }
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    return data


def print_standings(table):
    print("\n" + "=" * 64)
    print("  LEAGUE TABLE")
    print("=" * 64)
    print(f"  {'#':<2} {'Team':<22} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'Pts':>4}")
    for r in table:
        print(f"  {r['rank']:<2} {r['name']:<22} {r['played']:>2} {r['w']:>2} {r['d']:>2} {r['l']:>2}"
              f" {r['gf']:>3} {r['ga']:>3} {r['gd']:>+4} {r['points']:>4}")
    print("=" * 64)


# ---------------------------------------------------------------------------
def main() -> None:
    sim.load_dotenv()
    ap = argparse.ArgumentParser(description="AI Slime Soccer season runner")
    ap.add_argument("--config", default="season.config.json")
    ap.add_argument("--out", default="season.json")
    ap.add_argument("--no-verify", action="store_true", help="skip the pre-season model probe")
    ap.add_argument("--fresh", action="store_true", help="ignore any existing season.json and start over")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    cfg.setdefault("match_duration_seconds", 60)
    cfg.setdefault("decision_every_n_frames", 10)
    cfg.setdefault("fps", 30)

    models = normalize_models(cfg)
    fmt = cfg.get("format", "round_robin")

    # resume: load previously-played matches unless --fresh
    matches = []
    done_ids = set()
    if not args.fresh and os.path.exists(args.out):
        try:
            prev = json.load(open(args.out))
            matches = prev.get("matches", [])
            done_ids = {m["id"] for m in matches}
            if matches:
                print(f"Resuming — {len(matches)} match(es) already played; will skip those.")
        except Exception:
            pass

    if not args.no_verify:
        verify_models(models)

    fixtures = build_fixtures(models, fmt)
    to_play = [(h, a) for (h, a) in fixtures if f"{h['id']}__{a['id']}" not in done_ids]
    print(f"\nSeason: {cfg.get('season_name', 'League')}  |  {len(models)} models  |  "
          f"{len(fixtures)} fixtures ({fmt})  |  {len(to_play)} to play  |  "
          f"{cfg['match_duration_seconds']}s games\n")

    for i, (home, away) in enumerate(to_play, 1):
        print(f"  [{i}/{len(to_play)}] {home['name']} (home) vs {away['name']} (away) ...", flush=True)
        record = play_fixture(home, away, cfg)
        matches.append(record)
        write_season(args.out, cfg, models, matches)  # incremental save -> resumable
        hg, ag = record["score"]
        result = record["winner"] if record["winner_id"] != "draw" else "draw"
        print(f"        FT: {home['name']} {hg} - {ag} {away['name']}   ({result})", flush=True)

    data = write_season(args.out, cfg, models, matches)
    print_standings(data["standings"])
    total_cost = sum(s["total_cost_usd"] for s in data["aggregate_stats"].values())
    print(f"\n  Season written to: {args.out}")
    print(f"  Total estimated cost: ${total_cost:.4f}")
    print(f"  View it: run `python -m http.server` here, then open http://localhost:8000/index.html")


if __name__ == "__main__":
    main()
