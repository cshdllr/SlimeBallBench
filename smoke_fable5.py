"""One-call smoke test for Claude Fable 5 before running its league matches.

Fable 5 has thinking ALWAYS on (unlike Sonnet 5, which the league runs with
thinking disabled). So it needs a large token budget and low reasoning effort,
and its real per-move token usage must be checked before spending on 10 matches.

Run it once after adding Anthropic credits:

    ANTHROPIC_API_KEY=sk-ant-... python3 smoke_fable5.py

It makes a single decision call with the real system prompt + a sample game
state and prints the reply, the parsed action, token usage, latency, and the
projected cost of one match and all 10 Fable 5 matches. Green-light the full
run only if it prints a real action (not "idle") and the completion tokens are
small (tens, not thousands).
"""
import json
import os
import sys
import time

from openai import OpenAI

from simulate import SYSTEM_PROMPT, parse_action

MODEL = "claude-fable-5"
BASE_URL = "https://api.anthropic.com/v1/"
IN_PER_1M, OUT_PER_1M = 10.0, 50.0
MAX_COMPLETION_TOKENS = 4096
REASONING_EFFORT = "low"
DECISIONS_PER_MATCH = 180  # 3/sec * 60s
FABLE_MATCHES = 10

# a mid-game state in the same shape the simulator sends every turn
STATE = {
    "you_play_side": "left", "your_goal_x": 0.0, "opponent_goal_x": 1000.0,
    "ball": {"x": 612.4, "y": 88.1, "vx": -3.2, "vy": 5.0},
    "you": {"x": 240.0, "y": 0.0, "vx": 1.1, "vy": 0.0},
    "opponent": {"x": 760.5, "y": 0.0, "vx": -2.0, "vy": 0.0},
    "score": {"you": 2, "opponent": 3}, "time_remaining_seconds": 41.0,
}


def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY is not set — export it and rerun.", file=sys.stderr)
        return 2

    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=120)
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(STATE)},
        ],
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        reasoning_effort=REASONING_EFFORT,
    )
    dt = (time.perf_counter() - t0) * 1000.0

    content = resp.choices[0].message.content or ""
    action = parse_action(content)
    u = resp.usage
    pt = getattr(u, "prompt_tokens", 0) or 0
    ct = getattr(u, "completion_tokens", 0) or 0
    finish = resp.choices[0].finish_reason

    move_cost = (pt * IN_PER_1M + ct * OUT_PER_1M) / 1_000_000.0
    match_cost = move_cost * DECISIONS_PER_MATCH

    print(f"model            : {MODEL}  (effort={REASONING_EFFORT}, max_completion_tokens={MAX_COMPLETION_TOKENS})")
    print(f"raw reply        : {content!r}")
    print(f"parsed action    : {action}")
    print(f"finish_reason    : {finish}")
    print(f"latency          : {dt:.0f} ms")
    print(f"tokens           : prompt={pt}  completion={ct}")
    print(f"cost / move      : ${move_cost:.5f}")
    print(f"cost / match     : ~${match_cost:.2f}   ({DECISIONS_PER_MATCH} moves, Fable-5 side only)")
    print(f"cost / {FABLE_MATCHES} matches : ~${match_cost * FABLE_MATCHES:.2f}   (Fable-5 side; add ~$3 for opponents)")
    print()

    ok = True
    if finish == "length":
        print("⚠️  finish_reason=length — it hit the token cap mid-thought. Raise max_completion_tokens.")
        ok = False
    if action == "idle" and not content.strip():
        print("⚠️  empty reply parsed to idle — Fable 5 would never move. Fix budget/effort before running.")
        ok = False
    if ct > 400:
        print(f"⚠️  completion tokens high ({ct}) — thinking isn't being minimized; the full run will cost more than estimated.")
    if ok and action != "idle":
        print("✅ looks good — returns a real action. Safe to launch the league run.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
