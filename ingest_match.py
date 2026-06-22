#!/usr/bin/env python3
"""Fold an already-played simulate.py match log into the league's season.json.

Usage:
    python ingest_match.py logs/<match>.json [more_logs.json ...]
    python ingest_match.py --config season.config.json --out season.json logs/*.json

Why this exists: season.py plays a *full* round-robin in one run. Some models
(e.g. Gemini 3.1 Pro) hit their API rate limit within a single match, so we play
them one game per day via `simulate.py`. That writes a replay log but never
touches season.json, so the model never shows up in the standings. This script
ingests such a log — building the same match record season.py would, and
recomputing standings/aggregate_stats — so daily one-off games fold into the
league without replaying anything.

Idempotent: a fixture already present in season.json (same home__away id) is
skipped unless --replace is given.
"""
import argparse
import json
import os
import sys

import season  # reuse normalize_models / compute_* / write_season


def model_by_name(models: list[dict], name: str) -> dict:
    for m in models:
        if m["name"] == name:
            return m
    raise SystemExit(
        f"Log references model '{name}', which is not in the season config. "
        f"Add it to the config's models list first (known: "
        f"{', '.join(m['name'] for m in models)})."
    )


def record_from_log(log_path: str, models: list[dict]) -> dict:
    with open(log_path) as f:
        log = json.load(f)
    summary = log["summary"]
    # final_score preserves player order: first key = player1 (home), second = away.
    names = list(summary["final_score"].keys())
    if len(names) != 2:
        raise SystemExit(f"{log_path}: expected 2 players, got {names}")
    home = model_by_name(models, names[0])
    away = model_by_name(models, names[1])
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
        "log": log_path,
        "played_at": log.get("generated_at"),
        "summary": summary,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest a simulate.py log into season.json")
    ap.add_argument("logs", nargs="+", help="path(s) to match log JSON files")
    ap.add_argument("--config", default="season.config.json")
    ap.add_argument("--out", default="season.json")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite an existing record for the same fixture id")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    cfg.setdefault("match_duration_seconds", 60)
    cfg.setdefault("decision_every_n_frames", 10)
    cfg.setdefault("fps", 30)
    models = season.normalize_models(cfg)

    matches = []
    if os.path.exists(args.out):
        matches = json.load(open(args.out)).get("matches", [])
    by_id = {m["id"]: m for m in matches}

    added, skipped = 0, 0
    for log_path in args.logs:
        rec = record_from_log(log_path, models)
        if rec["id"] in by_id and not args.replace:
            print(f"  skip  {rec['id']} — already in {args.out} (use --replace to overwrite)")
            skipped += 1
            continue
        verb = "replace" if rec["id"] in by_id else "add"
        by_id[rec["id"]] = rec
        hg, ag = rec["score"]
        print(f"  {verb:7}{rec['home_name']} {hg} - {ag} {rec['away_name']}")
        added += 1

    if not added:
        print(f"\nNothing to do ({skipped} skipped).")
        return

    merged = list(by_id.values())
    data = season.write_season(args.out, cfg, models, merged)
    print(f"\n{args.out}: {len(merged)} match(es) total; {added} ingested, {skipped} skipped.")
    print(f"Models now in standings: {len(data['standings'])}")
    season.print_standings(data["standings"])


if __name__ == "__main__":
    main()
