# ⚽ AI Slime Soccer Simulator

Two AI models compete head-to-head at 2D "slime soccer." The match runs as a
**headless physics simulation** — every few frames it pauses, asks each model for
its next move, and steps the world forward. The whole match is logged frame by
frame to JSON, then **`replay.html`** plays it back as an animation. No API calls
happen during rendering, so replays are instant and reproducible.

```
.
├── simulate.py            # run ONE match: sim + model queries -> JSON log
├── season.py              # run a round-robin LEAGUE -> season.json + per-match logs
├── index.html             # league standings (landing page)
├── model.html             # a team's schedule + stats + replay links
├── stats.html             # sortable aggregate stats table
├── replay.html            # self-contained per-match replay visualizer
├── styles.css             # shared styles for the site
├── config.json            # single-match model + match settings
├── config.heuristic.json  # keyless demo (built-in bots, no API needed)
├── season.config.json     # league roster + settings
├── season.json            # generated league data (the website reads this)
├── requirements.txt
└── logs/                  # match replay logs (committed — they ARE the replays)
```

Everything is run from the repository root.

---

## Quick start (no API keys)

You can watch a full match immediately using the built-in bots:

```bash
python3 simulate.py --config config.heuristic.json
```

This writes a log to `logs/match_<timestamp>.json`. Then open **`replay.html`** in
your browser and load that file with the file picker. Done.

---

## Running a model-vs-model match

1. Install the client used to talk to the models:

   ```bash
   pip install -r requirements.txt
   ```

2. Edit `config.json` with your models, keys, and endpoints (see below).

3. Run:

   ```bash
   python3 simulate.py --config config.json
   ```

   You'll see live commentary — goals, the running score, and estimated cost —
   and a final report with token usage, cost per model, and the action mix.

4. Open `replay.html`, click the file picker, and load the new log from `logs/`.

CLI flags: `--config <path>`, `--out <path>` (override the log location),
`--seed <int>` (reproducible kickoff/serve randomness).

---

## Configuration

```json
{
  "match_duration_seconds": 180,
  "decision_every_n_frames": 6,
  "fps": 30,
  "player1": {
    "name": "Claude Opus 4.8",
    "model": "claude-opus-4-8",
    "api_key": "YOUR_ANTHROPIC_KEY",
    "base_url": "https://api.anthropic.com/v1/",
    "color": "#00BFFF",
    "input_cost_per_1m": 5.0,
    "output_cost_per_1m": 25.0,
    "request_timeout_seconds": 20,
    "max_tokens": 16
  },
  "player2": {
    "name": "GPT-5.5",
    "model": "gpt-5.5",
    "api_key": "YOUR_OPENAI_KEY",
    "base_url": "https://api.openai.com/v1",
    "color": "#FF4444",
    "input_cost_per_1m": 5.0,
    "output_cost_per_1m": 15.0
  }
}
```

| Field | Meaning |
|-------|---------|
| `match_duration_seconds` | Length of the match in **simulation** time (default 180). |
| `decision_every_n_frames` | Query the models every N frames (default 6 → 5 decisions/sec at 30fps). |
| `fps` | Physics/log frame rate (default 30). The sim runs as fast as possible; this only sets the time step and log resolution. |
| `name`, `color` | Display name and slime color in the replay. |
| `model`, `api_key`, `base_url` | The OpenAI-compatible endpoint to call for this player. |
| `input_cost_per_1m`, `output_cost_per_1m` | Pricing for the cost estimate ($ per 1M tokens). |
| `request_timeout_seconds` | Per-request timeout; on timeout the move defaults to `idle`. |
| `max_tokens` | Cap on the model's reply (the answer is a single action word, so this is tiny). |
| `policy` | `"model"` (default), `"heuristic"`, or `"random"`. The last two need no API. |

### Pluggable models — any OpenAI-compatible API

Both players go through one OpenAI-compatible client, so **any** provider that
speaks the OpenAI chat-completions API works for either side — just set `model`,
`api_key`, and `base_url`.

- **Claude:** point `base_url` at Anthropic's OpenAI-compatible endpoint,
  `https://api.anthropic.com/v1/`, and use a Claude model id (e.g. `claude-opus-4-8`).
- **OpenAI:** `https://api.openai.com/v1`.
- **Gemini:** `https://generativelanguage.googleapis.com/v1beta/openai/`, with a model
  id like `gemini-2.5-pro` or `gemini-2.5-flash`.
- **Local / self-hosted** (Ollama, vLLM, LM Studio, etc.): point `base_url` at the
  local server; `api_key` can be any non-empty string most of the time.

**Keys (per player):** a player resolves its key in this order: a literal `api_key`
in the config (discouraged — it's committed to disk), then an env var named by an
`api_key_env` field, then the `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` fallbacks. For a
mixed-provider league, give each player an `api_key_env` (e.g. `"api_key_env":
"GEMINI_API_KEY"`) and keep all keys in a gitignored **`.env`** file (auto-loaded at
startup) — one `KEY=value` line per provider. Set each player's `input_cost_per_1m` /
`output_cost_per_1m` to that provider's pricing for accurate cost tracking.

If a player has no resolvable key, it automatically falls back to the `heuristic` bot
so the match can still run.

---

## The replay viewer (`replay.html`)

A single self-contained HTML file — no build step, no dependencies. Open it in
any browser and load a log via the file picker. If you serve the folder over HTTP
(`python3 -m http.server`), you can also auto-load with `replay.html?log=logs/your.json`.

Shows the field, both goals, both slimes (in their configured colors), the ball,
the live score, the match timer, and a label per model with its current action.

**Controls:** Play / Pause, speed multiplier (0.5× / 1× / 2× / 4×), and a scrub
bar. Keyboard: `Space` toggles play, `←`/`→` step one frame.

Below the pitch, a **metrics panel** shows the full end-of-match breakdown: a match
summary line (winner, time to first goal, total touches, saves, own goals, lead
changes) plus a color-coded card per model with its goals, touches, goals/touch,
saves, own goals, possession, jumps, distance, and — for model players — decisions,
average decision latency, token usage, and estimated cost.

---

## How the physics works

- **Field:** `x` from 0 (left wall) to 1000 (right wall); `y` from 0 (ground) up,
  ceiling at 500. **Up is +y.** Gravity pulls everything down.
- **Goals:** an opening on each side wall from the ground up to `y = 120`. The ball
  scores when it crosses a side wall below that height. Player 1 defends the left
  goal, Player 2 the right.
- **Slimes:** semicircles (radius 50, flat side on the ground). They move left/right
  at a fixed speed and jump when grounded. Each can advance into a contested midfield
  band but not into the opponent's deep third, and the two slimes collide with each
  other like a solid wall (no passing through).
- **Ball:** radius 15, with gravity, wall/ground/ceiling bounce (restitution 0.8),
  and friction. When the ball touches a slime's dome it's launched along the
  contact normal — *where* you hit it decides which way it goes — plus a share of
  the slime's own velocity. The ball resets to center after each goal.

The simulation is a fixed-step Euler integrator at `1/fps` seconds per frame and
runs with no frame-rate cap.

---

## The AI decision loop

Every `decision_every_n_frames`, the sim pauses and queries each model
**sequentially** (never in parallel) to keep the world state consistent. Each model
receives:

- An **identical system prompt** describing the field, coordinate system, goals,
  and what each action does.
- A **user message** containing the serialized game state as JSON, from that
  player's own perspective:

  ```json
  {
    "you_play_side": "left",
    "your_goal_x": 0.0,
    "opponent_goal_x": 1000.0,
    "ball": {"x": 500.0, "y": 300.0, "vx": -60.0, "vy": 0.0},
    "you": {"x": 250.0, "y": 0.0, "vx": 0.0, "vy": 0.0},
    "opponent": {"x": 750.0, "y": 0.0, "vx": 0.0, "vy": 0.0},
    "score": {"you": 0, "opponent": 0},
    "time_remaining_seconds": 180.0
  }
  ```

The model replies with one action:
`move_left`, `move_right`, `jump`, `move_left_jump`, `move_right_jump`, `idle`.
Parsing is lenient (it scans the reply for an action word). **Errors and timeouts
default to `idle`**, so a flaky API never crashes the match.

---

## Output logs

Each run writes one JSON file to `logs/`:

- **`field`** — dimensions and constants, so the replay is self-describing.
- **`players`** — names, colors, and model/policy for each side.
- **`frames`** — one entry per frame: `t`, `ball [x,y]`, `p1 [x,y]`, `p2 [x,y]`,
  `score [s1,s2]`, and the action each AI took (`a1`, `a2`).
- **`summary`** — final score, winner, and every goal with its timestamp and scorer, plus:
  - **`match_metrics`**: total goals, total ball touches, total saves, total own goals,
    time to first goal (and who), lead changes, and largest lead.
  - **`players.<name>`** (per model): `goals_for` / `goals_against`, `ball_touches`,
    `goals_per_touch` (finishing efficiency), `saves`, `own_goals`, `possession_pct`
    (share of play time since each side last touched the ball), `jumps`,
    `distance_travelled`, `time_to_first_goal_seconds`, the full `action_distribution`,
    and — for model players — `decisions`, `avg_decision_latency_ms`, `api_errors`,
    `tokens`, and `estimated_cost_usd`.
  - Each goal in `goals[]` is flagged with `own_goal: true/false`.

  Definitions: a **save** is a touch made while the ball was a genuine threat to the
  toucher's own goal (low, near that goal, and moving toward it). An **own goal** is a
  goal whose last toucher was the conceding side. The headline numbers are also printed
  to the console at the end of the match.

A 3-minute match at 30fps is roughly 0.7 MB.

---

## Notes & tips

- **Cost control:** replies are a single word, so `max_tokens` stays tiny and cost
  is dominated by the (small) state prompt. The console prints a running estimate
  during the match and a per-model total at the end.
- **Latency:** with real APIs, a 3-minute match makes ~`duration * fps /
  decision_every_n_frames * 2` calls (≈1,800 by default). Increase
  `decision_every_n_frames` to make fewer, cheaper, slower decisions.
- **Reproducibility:** pass `--seed` to fix the kickoff serve direction. Model
  responses themselves are not deterministic unless the provider is.

---

## Running a league (season)

`season.py` plays a round-robin between the models in `season.config.json`,
writes a replay log per match to `logs/`, and a combined `season.json` the
website reads.

```bash
python3 season.py            # verify models, play all fixtures, write season.json
python3 season.py --fresh    # ignore an existing season.json and replay from scratch
python3 season.py --no-verify# skip the per-model pre-flight probe
```

Standings use points (win 3, draw 1), tiebroken by goal difference then goals
for. Re-running resumes — fixtures already in `season.json` are skipped.

## The website

Static pages, no build step — `index.html` (standings) → `model.html` (a team's
schedule + stats + replays) → `stats.html` (sortable aggregates), all reading
`season.json`. They must be served over HTTP (browsers block `fetch()` from
`file://`):

```bash
python3 -m http.server 8000   # then open http://localhost:8000/index.html
```

## Hosting on GitHub Pages

Replays are small JSON logs rendered in the browser (no video files), so the
whole thing is static and fits GitHub Pages' free tier easily. The match logs
are committed (they're the site's content). To publish: push, then in the repo's
**Settings → Pages**, deploy from your branch at the **`/ (root)`** folder.
