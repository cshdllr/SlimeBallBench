#!/usr/bin/env python3
"""
AI Slime Soccer Simulator
=========================

Runs a headless physics simulation of two "slime" players competing at soccer.
Every N frames the simulation pauses and queries each player's model (via an
OpenAI-compatible API) for the next action. The full match is logged frame by
frame to JSON, which `replay.html` plays back as an animation.

Usage:
    python simulate.py [--config config.json] [--out logs/<auto>.json]

The simulation is provider-neutral: each player is configured with a model
name, API key and base URL, so any OpenAI-compatible endpoint works. For Claude,
point base_url at Anthropic's OpenAI-compatible endpoint
(https://api.anthropic.com/v1/). A built-in "heuristic" policy lets you run a
match with no API keys at all (set "policy": "heuristic" on a player).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Physics constants (simulation units; roughly 1 unit == 1 pixel for rendering)
# ---------------------------------------------------------------------------
FIELD_WIDTH = 1000.0       # x: 0 (left wall) .. FIELD_WIDTH (right wall)
FIELD_HEIGHT = 500.0       # y: 0 (ground) .. FIELD_HEIGHT (ceiling), up is +y
GROUND_Y = 0.0
SLIME_RADIUS = 50.0
BALL_RADIUS = 15.0
GOAL_HEIGHT = 120.0        # ball scores when it crosses a side wall below this y
CENTER_X = FIELD_WIDTH / 2.0

# How far each slime may advance. They share a contested middle band but cannot
# walk into the opponent's deep third — and they collide (block each other) on top.
P1_X_MIN, P1_X_MAX = SLIME_RADIUS, FIELD_WIDTH * 0.65
P2_X_MIN, P2_X_MAX = FIELD_WIDTH * 0.35, FIELD_WIDTH - SLIME_RADIUS

GRAVITY = -2000.0          # units / s^2
MOVE_SPEED = 420.0         # horizontal slime speed, units / s
JUMP_VELOCITY = 850.0      # initial vertical slime speed on jump
BALL_BOUNCE = 0.8          # restitution against ground / walls / ceiling
GROUND_FRICTION = 0.985    # horizontal ball damping while rolling on ground
AIR_FRICTION = 0.999       # mild horizontal air damping for the ball
HIT_BOOST = 620.0          # minimum ball speed imparted by a slime contact

VALID_ACTIONS = [
    "move_left",
    "move_right",
    "jump",
    "move_left_jump",
    "move_right_jump",
    "idle",
]

# Default pricing ($ per 1M tokens) used only if a player config omits it.
DEFAULT_PRICING = {
    "input_cost_per_1m": 5.0,
    "output_cost_per_1m": 25.0,
}


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
@dataclass
class Slime:
    x: float
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    x_min: float = SLIME_RADIUS
    x_max: float = FIELD_WIDTH - SLIME_RADIUS

    @property
    def grounded(self) -> bool:
        return self.y <= 1e-6

    def apply_action(self, action: str) -> bool:
        """Apply an action; return True if a jump was actually initiated."""
        direction = 0
        jump = False
        if action == "move_left":
            direction = -1
        elif action == "move_right":
            direction = 1
        elif action == "jump":
            jump = True
        elif action == "move_left_jump":
            direction, jump = -1, True
        elif action == "move_right_jump":
            direction, jump = 1, True
        # idle / unknown -> no motion

        self.vx = direction * MOVE_SPEED
        if jump and self.grounded:
            self.vy = JUMP_VELOCITY
            return True
        return False

    def step(self, dt: float) -> None:
        # vertical: gravity + integration, land on ground
        self.vy += GRAVITY * dt
        self.y += self.vy * dt
        if self.y <= GROUND_Y:
            self.y = GROUND_Y
            self.vy = 0.0

        # horizontal: integrate and clamp to this slime's allowed half
        self.x += self.vx * dt
        if self.x < self.x_min:
            self.x, self.vx = self.x_min, 0.0
        elif self.x > self.x_max:
            self.x, self.vx = self.x_max, 0.0


@dataclass
class Ball:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
@dataclass
class GoalEvent:
    frame: int
    sim_time: float
    scorer: str  # "p1" or "p2"
    score: tuple
    own_goal: bool = False
    toucher: Optional[int] = None  # last player to touch before the goal


class Simulation:
    def __init__(self, fps: int):
        self.fps = fps
        self.dt = 1.0 / fps
        # Player 1 occupies the left half and defends the left goal (x = 0).
        # Player 2 occupies the right half and defends the right goal.
        self.p1 = Slime(x=FIELD_WIDTH * 0.25, x_min=P1_X_MIN, x_max=P1_X_MAX)
        self.p2 = Slime(x=FIELD_WIDTH * 0.75, x_min=P2_X_MIN, x_max=P2_X_MAX)
        self.ball = Ball(x=CENTER_X, y=FIELD_HEIGHT * 0.6)
        self.score = [0, 0]
        self.goals: list[GoalEvent] = []
        self.frame = 0

        # -- per-player gameplay metrics (index 0 = p1, 1 = p2) --
        self.touches = [0, 0]          # distinct ball contacts (rising edge)
        self.jumps = [0, 0]            # jumps actually launched
        self.distance = [0.0, 0.0]     # horizontal distance the slime travelled
        self.possession = [0, 0]       # frames since each player last touched the ball
        self.saves = [0, 0]            # touches that cleared a real goal threat
        self.own_goals = [0, 0]        # goals a player put into their own net
        self._contact = [False, False]  # currently overlapping the ball?
        self.last_toucher: Optional[int] = None

    # -- reset after a goal -------------------------------------------------
    def reset_positions(self, serve_to_left: bool) -> None:
        self.p1 = Slime(x=FIELD_WIDTH * 0.25, x_min=P1_X_MIN, x_max=P1_X_MAX)
        self.p2 = Slime(x=FIELD_WIDTH * 0.75, x_min=P2_X_MIN, x_max=P2_X_MAX)
        # Give the ball a small nudge toward whoever was just scored on.
        nudge = -60.0 if serve_to_left else 60.0
        self.ball = Ball(x=CENTER_X, y=FIELD_HEIGHT * 0.6, vx=nudge)
        # the ball is dead-center and untouched again
        self._contact = [False, False]
        self.last_toucher = None

    # -- apply both players' actions, counting jumps ------------------------
    def apply_actions(self, a1: str, a2: str) -> None:
        if self.p1.apply_action(a1):
            self.jumps[0] += 1
        if self.p2.apply_action(a2):
            self.jumps[1] += 1

    # -- one physics tick ---------------------------------------------------
    def step(self) -> Optional[GoalEvent]:
        x0_1, x0_2 = self.p1.x, self.p2.x
        self.p1.step(self.dt)
        self.p2.step(self.dt)
        self._separate_slimes()
        self.distance[0] += abs(self.p1.x - x0_1)
        self.distance[1] += abs(self.p2.x - x0_2)

        b = self.ball
        b.vy += GRAVITY * self.dt
        b.x += b.vx * self.dt
        b.y += b.vy * self.dt

        # ground
        if b.y - BALL_RADIUS <= GROUND_Y:
            b.y = GROUND_Y + BALL_RADIUS
            b.vy = -b.vy * BALL_BOUNCE
            b.vx *= GROUND_FRICTION
        else:
            b.vx *= AIR_FRICTION

        # ceiling
        if b.y + BALL_RADIUS >= FIELD_HEIGHT:
            b.y = FIELD_HEIGHT - BALL_RADIUS
            b.vy = -abs(b.vy) * BALL_BOUNCE

        # side walls / goals
        goal = self._check_walls_and_goals()
        if goal is not None:
            return goal

        # slime collisions
        self._collide_with_slime(0, self.p1)
        self._collide_with_slime(1, self.p2)

        # possession: credit each frame to whoever last touched the ball
        if self.last_toucher is not None:
            self.possession[self.last_toucher] += 1

        return None

    def _check_walls_and_goals(self) -> Optional[GoalEvent]:
        b = self.ball
        # Left wall / goal (p1's goal). A goal here scores for p2.
        if b.x - BALL_RADIUS <= 0:
            if b.y <= GOAL_HEIGHT:
                self.score[1] += 1
                own = self.last_toucher == 0  # p1 put it into their own net
                if own:
                    self.own_goals[0] += 1
                return self._make_goal("p2", own)
            b.x = BALL_RADIUS
            b.vx = abs(b.vx) * BALL_BOUNCE
        # Right wall / goal (p2's goal). A goal here scores for p1.
        elif b.x + BALL_RADIUS >= FIELD_WIDTH:
            if b.y <= GOAL_HEIGHT:
                self.score[0] += 1
                own = self.last_toucher == 1  # p2 put it into their own net
                if own:
                    self.own_goals[1] += 1
                return self._make_goal("p1", own)
            b.x = FIELD_WIDTH - BALL_RADIUS
            b.vx = -abs(b.vx) * BALL_BOUNCE
        return None

    def _make_goal(self, scorer: str, own: bool) -> GoalEvent:
        ev = GoalEvent(self.frame, self.frame * self.dt, scorer, tuple(self.score),
                       own_goal=own, toucher=self.last_toucher)
        self.goals.append(ev)
        return ev

    def _separate_slimes(self) -> None:
        """Keep the two slimes from overlapping — they block each other like a wall."""
        a, b = (self.p1, self.p2) if self.p1.x <= self.p2.x else (self.p2, self.p1)
        min_sep = 2 * SLIME_RADIUS
        gap = b.x - a.x
        if gap >= min_sep:
            return
        overlap = min_sep - gap
        a.x -= overlap / 2.0
        b.x += overlap / 2.0
        # respect field-half limits; if a bound is hit, push the other slime instead
        a.x = max(a.x, a.x_min)
        b.x = min(b.x, b.x_max)
        if b.x - a.x < min_sep:
            deficit = min_sep - (b.x - a.x)
            room = a.x - a.x_min
            push = min(deficit, room)
            a.x -= push
            b.x = min(b.x + (deficit - push), b.x_max)
        # cancel the velocity components driving them together
        if a.vx > 0:
            a.vx = 0.0
        if b.vx < 0:
            b.vx = 0.0

    def _collide_with_slime(self, idx: int, slime: Slime) -> None:
        b = self.ball
        dx = b.x - slime.x
        dy = b.y - slime.y
        dist = math.hypot(dx, dy)
        min_dist = SLIME_RADIUS + BALL_RADIUS
        # Only the rounded dome (upper half) collides; ignore contact from below.
        if dist >= min_dist or dist <= 1e-6 or dy < -BALL_RADIUS:
            self._contact[idx] = False
            return

        # count a touch only on the rising edge (not every frame of contact)
        if not self._contact[idx]:
            self.touches[idx] += 1
            self.last_toucher = idx
            # A save: at contact the ball was a real threat to this player's own
            # goal — low, near that goal, and moving toward it (incoming velocity).
            if idx == 0:
                threat = b.vx < -50 and b.x < 280 and b.y < GOAL_HEIGHT + 70
            else:
                threat = b.vx > 50 and b.x > FIELD_WIDTH - 280 and b.y < GOAL_HEIGHT + 70
            if threat:
                self.saves[idx] += 1
        self._contact[idx] = True

        nx, ny = dx / dist, dy / dist
        # push the ball out to the dome surface
        b.x = slime.x + nx * min_dist
        b.y = slime.y + ny * min_dist

        # impart velocity along the contact normal, carrying the slime's motion
        speed = max(math.hypot(b.vx, b.vy), HIT_BOOST)
        b.vx = nx * speed + slime.vx * 0.6
        b.vy = max(ny * speed + slime.vy * 0.5, 80.0)  # always pop upward a bit


# ---------------------------------------------------------------------------
# Game state serialization (what each model sees)
# ---------------------------------------------------------------------------
def build_state(sim: Simulation, player: str, time_remaining: float) -> dict:
    """Build the JSON state from the perspective of `player` ('p1' or 'p2')."""
    me = sim.p1 if player == "p1" else sim.p2
    opp = sim.p2 if player == "p1" else sim.p1
    if player == "p1":
        my_goal_x, target_goal_x, side = 0.0, FIELD_WIDTH, "left"
        my_score, opp_score = sim.score[0], sim.score[1]
    else:
        my_goal_x, target_goal_x, side = FIELD_WIDTH, 0.0, "right"
        my_score, opp_score = sim.score[1], sim.score[0]

    def r(v):
        return round(v, 1)

    return {
        "you_play_side": side,
        "your_goal_x": my_goal_x,
        "opponent_goal_x": target_goal_x,
        "ball": {"x": r(sim.ball.x), "y": r(sim.ball.y), "vx": r(sim.ball.vx), "vy": r(sim.ball.vy)},
        "you": {"x": r(me.x), "y": r(me.y), "vx": r(me.vx), "vy": r(me.vy)},
        "opponent": {"x": r(opp.x), "y": r(opp.y), "vx": r(opp.vx), "vy": r(opp.vy)},
        "score": {"you": my_score, "opponent": opp_score},
        "time_remaining_seconds": round(time_remaining, 1),
    }


SYSTEM_PROMPT = f"""You are an AI controlling a "slime" in a 2D slime-soccer match.

THE FIELD (coordinate system):
- x runs from 0 (left wall) to {FIELD_WIDTH:.0f} (right wall).
- y runs from 0 (the ground) upward; larger y is higher. Gravity pulls everything down.
- The ceiling is at y = {FIELD_HEIGHT:.0f}.
- There is a goal at each side wall. A goal is the opening from the ground up to
  y = {GOAL_HEIGHT:.0f}. If the ball crosses a side wall below that height, a goal is scored.

YOUR SLIME:
- It is a semicircle (flat side on the ground) of radius {SLIME_RADIUS:.0f}. The ball is radius {BALL_RADIUS:.0f}.
- You can roam most of the field but not deep into the opponent's end. The opponent
  slime is a SOLID WALL — you cannot pass through it, you bump into it and stop.
- When the ball touches your dome it is launched away along the contact direction,
  so WHERE you hit the ball decides which way it goes: hit it on its left side to send
  it right, hit its right side to send it left, get under it to pop it up and over the
  opponent. Your movement speed is added to the ball, so move INTO the ball to hit harder.

THE STATE (sent each turn as JSON):
- "you_play_side": which side you defend.
- "your_goal_x": the x of the goal you must DEFEND. "opponent_goal_x": the goal you ATTACK.
- "ball", "you", "opponent": each has x, y position and vx, vy velocity.
- "score": your goals vs opponent goals. "time_remaining_seconds".

YOUR ACTIONS (choose exactly one each turn):
- move_left        : move toward smaller x
- move_right       : move toward larger x
- jump             : jump straight up (only works when on the ground)
- move_left_jump   : move left while jumping
- move_right_jump  : move right while jumping
- idle             : stand still

HOW TO WIN: score MORE goals than the opponent before time runs out. You score by
knocking the ball into the opponent's goal opening (at opponent_goal_x); you concede if
it enters yours (at your_goal_x).

STRATEGY — be active, do not stand around:
- ATTACK: chase the ball, get to it before the opponent, and hit it toward
  opponent_goal_x. To beat the wall in front of their goal, pop the ball UP and OVER
  by getting under it, ideally while jumping.
- DEFEND: when the ball is heading toward your_goal_x, get between it and your goal and
  clear it away.
- Almost every turn you should be moving toward where the ball is going, or jumping to
  strike it. Only choose `idle` if you are already in the exact spot you want — idling
  while the ball is in play usually wastes the turn and loses the match.

Respond with ONLY the single action word, nothing else."""


def parse_action(text: str) -> str:
    """Extract a valid action from a model response, robustly.

    Tolerant of natural phrasing: "move left", "I'll jump left", "go right and
    jump", "stay put", etc. — not just the exact underscore tokens.
    """
    if not text:
        return "idle"
    t = text.lower()

    jump = "jump" in t
    li, ri = t.find("left"), t.find("right")
    has_left, has_right = li != -1, ri != -1
    # If both directions appear, trust whichever is mentioned first.
    if has_left and has_right:
        if li < ri:
            has_right = False
        else:
            has_left = False

    if has_left:
        return "move_left_jump" if jump else "move_left"
    if has_right:
        return "move_right_jump" if jump else "move_right"
    if jump:
        return "jump"
    return "idle"


# ---------------------------------------------------------------------------
# Policies: model-backed and built-in heuristic
# ---------------------------------------------------------------------------
class Player:
    def __init__(self, key: str, cfg: dict):
        self.key = key                       # "p1" / "p2"
        self.name = cfg.get("name", key)
        self.color = cfg.get("color", "#888888")
        self.model = cfg.get("model", "")
        self.policy = cfg.get("policy", "model")
        self.input_cost = cfg.get("input_cost_per_1m", DEFAULT_PRICING["input_cost_per_1m"])
        self.output_cost = cfg.get("output_cost_per_1m", DEFAULT_PRICING["output_cost_per_1m"])

        # stats
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.action_counts = {a: 0 for a in VALID_ACTIONS}
        self.errors = 0
        self.decision_count = 0       # model calls attempted
        self.total_latency_ms = 0.0   # cumulative API round-trip time

        self._client = None
        if self.policy == "model":
            self._init_client(cfg)

    def _init_client(self, cfg: dict) -> None:
        try:
            from openai import OpenAI
        except ImportError:
            print("ERROR: the 'openai' package is required for model players.\n"
                  "       Install it with:  pip install openai\n"
                  "       (or set \"policy\": \"heuristic\" on the player to run without an API).",
                  file=sys.stderr)
            sys.exit(1)

        # Key resolution order:
        #   1. literal api_key in the config (discouraged — it's committed to disk)
        #   2. a per-player env var named by "api_key_env" (best for mixed providers)
        #   3. ANTHROPIC_API_KEY / OPENAI_API_KEY fallbacks (single-provider convenience)
        env_name = cfg.get("api_key_env")
        api_key = (cfg.get("api_key")
                   or (os.environ.get(env_name) if env_name else None)
                   or os.environ.get("ANTHROPIC_API_KEY")
                   or os.environ.get("OPENAI_API_KEY", ""))
        base_url = cfg.get("base_url") or None
        timeout = cfg.get("request_timeout_seconds", 20)
        if not api_key:
            print(f"WARNING: player '{self.name}' has no api_key; falling back to heuristic policy.",
                  file=sys.stderr)
            self.policy = "heuristic"
            return
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._max_tokens = cfg.get("max_tokens", 16)

    @property
    def cost(self) -> float:
        return (self.prompt_tokens * self.input_cost
                + self.completion_tokens * self.output_cost) / 1_000_000.0

    def decide(self, state: dict) -> str:
        if self.policy in ("heuristic", "random"):
            action = self._heuristic(state) if self.policy == "heuristic" else random.choice(VALID_ACTIONS)
        else:
            action = self._ask_model(state)
        self.action_counts[action] += 1
        return action

    # -- model-backed decision ---------------------------------------------
    def _ask_model(self, state: dict) -> str:
        t0 = time.perf_counter()
        self.decision_count += 1
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(state)},
                ],
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            content = resp.choices[0].message.content or ""
            return parse_action(content)
        except Exception as exc:  # timeout, API error, malformed response -> idle
            self.errors += 1
            print(f"  [api] {self.name} error ({type(exc).__name__}: {exc}); defaulting to idle.",
                  file=sys.stderr)
            return "idle"
        finally:
            self.total_latency_ms += (time.perf_counter() - t0) * 1000.0

    @property
    def avg_latency_ms(self) -> Optional[float]:
        if self.decision_count == 0:
            return None
        return self.total_latency_ms / self.decision_count

    # -- built-in heuristic bot --------------------------------------------
    def _heuristic(self, state: dict) -> str:
        ball = state["ball"]
        me = state["you"]
        my_goal_x = state["your_goal_x"]
        attack_right = state["opponent_goal_x"] > FIELD_WIDTH / 2

        # Stay goal-side of the ball so a contact sends it toward the opponent:
        # if we attack right, sit slightly left of the ball, and vice-versa.
        contact_offset = -(SLIME_RADIUS * 0.6) if attack_right else (SLIME_RADIUS * 0.6)

        # When the ball is loose on our own half we hang back toward our goal to
        # defend rather than charging out; otherwise we go meet it for a clear.
        ball_on_my_half = abs(ball["x"] - my_goal_x) < FIELD_WIDTH / 2
        if ball_on_my_half:
            # mostly chase the ball, with a slight pull back toward our goal
            guard_x = my_goal_x + (160 if my_goal_x < FIELD_WIDTH / 2 else -160)
            desired_x = 0.8 * (ball["x"] + contact_offset) + 0.2 * guard_x
        else:
            # ball is on the opponent's half — hold near the center line, ready
            desired_x = my_goal_x + (380 if my_goal_x < FIELD_WIDTH / 2 else -380)

        dx = desired_x - me["x"]
        close = abs(ball["x"] - me["x"]) < SLIME_RADIUS + BALL_RADIUS + 25
        ball_above = SLIME_RADIUS < ball["y"] < 270
        should_jump = close and ball_above and me["y"] <= 1.0

        if abs(dx) < 12:
            return "jump" if should_jump else "idle"
        if dx > 0:
            return "move_right_jump" if should_jump else "move_right"
        return "move_left_jump" if should_jump else "move_left"


# ---------------------------------------------------------------------------
# Match runner
# ---------------------------------------------------------------------------
def run_match(config: dict, out_path: str, quiet: bool = False) -> dict:
    """Play one match, write its log to out_path, and return the log dict.

    When quiet=True, per-frame commentary and the final report are suppressed
    (used by the season runner, which prints its own concise output).
    """
    fps = int(config.get("fps", 30))
    duration = float(config.get("match_duration_seconds", 180))
    decide_every = int(config.get("decision_every_n_frames", 6))
    total_frames = int(duration * fps)

    p1 = Player("p1", config["player1"])
    p2 = Player("p2", config["player2"])
    sim = Simulation(fps)

    if not quiet:
        print("=" * 60)
        print(f"  AI SLIME SOCCER  —  {p1.name}  vs  {p2.name}")
        print(f"  {duration:.0f}s @ {fps}fps  |  decide every {decide_every} frames")
        print("=" * 60)

    frames = []
    action1, action2 = "idle", "idle"
    next_commentary = time.time()
    serve_to_left = random.random() < 0.5

    for frame in range(total_frames):
        sim.frame = frame
        time_remaining = duration - frame / fps

        # Decision tick: query each model SEQUENTIALLY to keep state consistent.
        if frame % decide_every == 0:
            state1 = build_state(sim, "p1", time_remaining)
            action1 = p1.decide(state1)
            state2 = build_state(sim, "p2", time_remaining)
            action2 = p2.decide(state2)

        sim.apply_actions(action1, action2)
        goal = sim.step()

        frames.append({
            "t": frame,
            "ball": [round(sim.ball.x, 1), round(sim.ball.y, 1)],
            "p1": [round(sim.p1.x, 1), round(sim.p1.y, 1)],
            "p2": [round(sim.p2.x, 1), round(sim.p2.y, 1)],
            "score": [sim.score[0], sim.score[1]],
            "a1": action1,
            "a2": action2,
        })

        if goal is not None:
            if not quiet:
                scorer = p1.name if goal.scorer == "p1" else p2.name
                mm, ss = divmod(int(goal.sim_time), 60)
                print(f"  ⚽ GOAL! {scorer} scores at {mm:02d}:{ss:02d}  "
                      f"→  {p1.name} {sim.score[0]} - {sim.score[1]} {p2.name}")
            serve_to_left = goal.scorer == "p2"
            sim.reset_positions(serve_to_left)
            action1, action2 = "idle", "idle"

        # Periodic running commentary (score + estimated cost so far).
        if not quiet:
            now = time.time()
            if now >= next_commentary:
                mm, ss = divmod(int(frame / fps), 60)
                print(f"  [{mm:02d}:{ss:02d}]  {p1.name} {sim.score[0]} - {sim.score[1]} {p2.name}"
                      f"   est. cost: ${p1.cost + p2.cost:.4f}")
                next_commentary = now + 5.0

    summary = build_summary(config, sim, p1, p2, fps, duration)
    log = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fps": fps,
        "field": {
            "width": FIELD_WIDTH, "height": FIELD_HEIGHT, "ground": GROUND_Y,
            "slime_radius": SLIME_RADIUS, "ball_radius": BALL_RADIUS,
            "goal_height": GOAL_HEIGHT, "center_x": CENTER_X,
        },
        "players": {
            "p1": {"name": p1.name, "color": p1.color, "model": p1.model, "policy": p1.policy},
            "p2": {"name": p2.name, "color": p2.color, "model": p2.model, "policy": p2.policy},
        },
        "summary": summary,
        "frames": frames,
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(log, f)

    if not quiet:
        print_final_report(summary, p1, p2, out_path)
    return log


def build_summary(config, sim, p1, p2, fps, duration) -> dict:
    return {
        "duration_seconds": duration,
        "fps": fps,
        "final_score": {p1.name: sim.score[0], p2.name: sim.score[1]},
        "winner": (
            p1.name if sim.score[0] > sim.score[1]
            else p2.name if sim.score[1] > sim.score[0]
            else "draw"
        ),
        "goals": [
            {
                "frame": g.frame,
                "time_seconds": round(g.sim_time, 1),
                "scorer": p1.name if g.scorer == "p1" else p2.name,
                "score_after": list(g.score),
                "own_goal": g.own_goal,
            }
            for g in sim.goals
        ],
        "match_metrics": _match_metrics(sim, p1, p2),
        "players": {
            p1.name: _player_summary(p1, 0, sim),
            p2.name: _player_summary(p2, 1, sim),
        },
    }


def _match_metrics(sim: Simulation, p1: Player, p2: Player) -> dict:
    goals = sim.goals
    # lead changes: how often the team that's ahead flips
    last_sign = 0
    lead_changes = 0
    largest_lead = 0
    for g in goals:
        diff = g.score[0] - g.score[1]
        largest_lead = max(largest_lead, abs(diff))
        sign = (diff > 0) - (diff < 0)
        if sign != 0:
            if last_sign != 0 and sign != last_sign:
                lead_changes += 1
            last_sign = sign
    return {
        "total_goals": len(goals),
        "total_ball_touches": sim.touches[0] + sim.touches[1],
        "total_own_goals": sim.own_goals[0] + sim.own_goals[1],
        "total_saves": sim.saves[0] + sim.saves[1],
        "time_to_first_goal_seconds": round(goals[0].sim_time, 1) if goals else None,
        "first_scorer": (p1.name if goals[0].scorer == "p1" else p2.name) if goals else None,
        "lead_changes": lead_changes,
        "largest_lead": largest_lead,
    }


def _player_summary(p: Player, idx: int, sim: Simulation) -> dict:
    touches = sim.touches[idx]
    goals_for = sim.score[idx]
    goals_against = sim.score[1 - idx]
    possession_total = sim.possession[0] + sim.possession[1]
    possession_pct = (
        round(100.0 * sim.possession[idx] / possession_total, 1)
        if possession_total else 0.0
    )
    scorer_key = "p1" if idx == 0 else "p2"
    first_goal_for = next(
        (round(g.sim_time, 1) for g in sim.goals if g.scorer == scorer_key), None
    )
    avg_lat = p.avg_latency_ms
    return {
        "model": p.model,
        "policy": p.policy,
        # gameplay
        "goals_for": goals_for,
        "goals_against": goals_against,
        "ball_touches": touches,
        "goals_per_touch": round(goals_for / touches, 3) if touches else 0.0,
        "saves": sim.saves[idx],
        "own_goals": sim.own_goals[idx],
        "possession_pct": possession_pct,
        "jumps": sim.jumps[idx],
        "distance_travelled": round(sim.distance[idx], 1),
        "time_to_first_goal_seconds": first_goal_for,
        "action_distribution": p.action_counts,
        # model / cost
        "decisions": p.decision_count,
        "avg_decision_latency_ms": round(avg_lat, 1) if avg_lat is not None else None,
        "api_errors": p.errors,
        "tokens": {"prompt": p.prompt_tokens, "completion": p.completion_tokens},
        "estimated_cost_usd": round(p.cost, 6),
    }


def print_final_report(summary, p1, p2, out_path) -> None:
    print("\n" + "=" * 60)
    print("  FINAL RESULT")
    print("=" * 60)
    fs = summary["final_score"]
    print(f"  {p1.name}  {fs[p1.name]} - {fs[p2.name]}  {p2.name}")
    print(f"  Winner: {summary['winner']}")
    mm = summary["match_metrics"]
    print(f"\n  Goals: {len(summary['goals'])}")
    for g in summary["goals"]:
        m, s = divmod(int(g["time_seconds"]), 60)
        og = "  (OWN GOAL)" if g.get("own_goal") else ""
        print(f"    {m:02d}:{s:02d}  {g['scorer']}  ({g['score_after'][0]}-{g['score_after'][1]}){og}")
    ttfg = mm["time_to_first_goal_seconds"]
    print(f"\n  Match metrics:")
    print(f"    Time to first goal: {ttfg if ttfg is not None else '—'}s"
          f"  (by {mm['first_scorer'] or '—'})")
    print(f"    Total ball touches: {mm['total_ball_touches']}   |  saves: {mm['total_saves']}"
          f"   |  own goals: {mm['total_own_goals']}")
    print(f"    Lead changes: {mm['lead_changes']}   |  largest lead: {mm['largest_lead']}")

    print("\n  Per-model gameplay:")
    sp = summary["players"]
    for p in (p1, p2):
        m = sp[p.name]
        lat = m["avg_decision_latency_ms"]
        lat_str = f"{lat:.0f}ms/decision" if lat is not None else "n/a"
        print(f"    {p.name}:")
        print(f"      goals {m['goals_for']}-{m['goals_against']}  |  touches {m['ball_touches']}"
              f"  |  goals/touch {m['goals_per_touch']}  |  possession {m['possession_pct']}%")
        print(f"      saves {m['saves']}  |  own goals {m['own_goals']}"
              f"  |  jumps {m['jumps']}  |  distance {m['distance_travelled']:.0f}"
              f"  |  1st goal {m['time_to_first_goal_seconds'] if m['time_to_first_goal_seconds'] is not None else '—'}s"
              f"  |  latency {lat_str}")

    print("\n  Token usage & estimated cost:")
    for p in (p1, p2):
        print(f"    {p.name}: {p.prompt_tokens} in + {p.completion_tokens} out tokens"
              f"  →  ${p.cost:.4f}   (api errors: {p.errors})")
    total = p1.cost + p2.cost
    print(f"    TOTAL estimated cost: ${total:.4f}")

    print("\n  Action distribution:")
    for p in (p1, p2):
        dist = ", ".join(f"{a}:{c}" for a, c in p.action_counts.items() if c)
        print(f"    {p.name}: {dist or '(none)'}")

    print(f"\n  Log written to: {out_path}")
    print(f"  Open replay.html and load that file to watch the match.")
    print("=" * 60)


def slugify(name: str) -> str:
    """Turn a display name into a filename-safe slug, e.g. 'Claude Haiku 4.5' -> 'claude_haiku_4_5'."""
    out = []
    for ch in name.lower():
        out.append(ch if ch.isalnum() else "_")
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "player"


def default_log_path(config: dict) -> str:
    p1 = slugify(config["player1"].get("name", "p1"))
    p2 = slugify(config["player2"].get("name", "p2"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("logs", f"{p1}_v_{p2}_{ts}.json")


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Sets vars that aren't already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def main() -> None:
    load_dotenv()  # pick up ANTHROPIC_API_KEY from a local .env if present
    ap = argparse.ArgumentParser(description="AI Slime Soccer simulator")
    ap.add_argument("--config", default="config.json", help="path to config JSON")
    ap.add_argument("--out", default=None, help="output log path (default: logs/match_<timestamp>.json)")
    ap.add_argument("--seed", type=int, default=None, help="random seed for reproducibility")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    with open(args.config) as f:
        config = json.load(f)

    out_path = args.out or default_log_path(config)

    run_match(config, out_path)


if __name__ == "__main__":
    main()
