"""Ask each model to design its own soccer-team crest as an SVG.

Reuses the endpoints / keys in season.config.json. Each model gets one prompt
and returns raw SVG, which we extract + sanitize (no scripts, handlers, external
refs, raster) before saving to crests/<id>.svg. Purely a generation tool — it
touches nothing the site loads until you wire the crests in.
"""
import json, os, re, sys
from openai import OpenAI
import simulate

PROMPT = """You are the {name} team in SlimeBallBench — a league where AI models \
compete head-to-head at soccer.

Design your team's crest and return it as a single self-contained SVG.

Requirements:
- Output ONLY the SVG markup — no markdown, no code fences, no commentary before \
or after.
- Root element: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"> … \
</svg>, exactly square.
- Your team color is {color}. Make it the dominant color — the field, the primary \
shape, or both. You may add up to two accent colors.
- Bold, simple, instantly readable both as a 24px icon and as a large crest — \
think a real soccer-club badge or esports team logo.
- Vector shapes only: path, circle, rect, polygon, line, ellipse. NO text, \
letters, numbers, or words anywhere. No <image>, no external references, no \
embedded raster data, no scripts.
- Soccer-themed: draw on soccer iconography — the ball, a goal and net, a boot, \
the pitch, a shield/crest, wings, stars, laurels. Do NOT depict a slime or blob.
- It should feel like YOU — your identity, or how you'd want your team seen.

Return the SVG now."""


def sanitize_svg(raw: str):
    """Extract the <svg>…</svg> and strip anything executable or external."""
    s = raw.strip()
    s = re.sub(r"^```[a-zA-Z]*", "", s).strip()
    s = re.sub(r"```\s*$", "", s).strip()
    m = re.search(r"<svg\b.*?</svg\s*>", s, re.S | re.I)
    if not m:
        return None
    svg = m.group(0)
    svg = re.sub(r"<script\b.*?</script\s*>", "", svg, flags=re.S | re.I)
    svg = re.sub(r"<foreignObject\b.*?</foreignObject\s*>", "", svg, flags=re.S | re.I)
    svg = re.sub(r"<image\b[^>]*/?>", "", svg, flags=re.I)
    svg = re.sub(r"<!\[CDATA\[.*?\]\]>", "", svg, flags=re.S)
    svg = re.sub(r"\son\w+\s*=\s*\"[^\"]*\"", "", svg, flags=re.I)
    svg = re.sub(r"\son\w+\s*=\s*'[^']*'", "", svg, flags=re.I)
    svg = re.sub(r"(xlink:href|href)\s*=\s*\"(?:javascript:|https?:|//)[^\"]*\"", "", svg, flags=re.I)
    svg = re.sub(r"(xlink:href|href)\s*=\s*'(?:javascript:|https?:|//)[^']*'", "", svg, flags=re.I)
    head = svg[:svg.find(">") + 1]
    if "xmlns" not in head:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def main():
    simulate.load_dotenv()
    cfg = json.load(open("season.config.json"))
    # ids + colors come from season.json (authoritative for the site)
    season = json.load(open("season.json"))
    id_by_name = {m["name"]: m["id"] for m in season["models"]}

    os.makedirs("crests", exist_ok=True)
    results, failures = [], []

    for m in cfg["models"]:
        name, color = m["name"], m.get("color", "#888")
        mid = id_by_name.get(name, slug(name))
        env_name = m.get("api_key_env")
        key = (m.get("api_key")
               or (os.environ.get(env_name) if env_name else None)
               or os.environ.get("ANTHROPIC_API_KEY")
               or os.environ.get("OPENAI_API_KEY"))
        if not key:
            print(f"  {name:24} NO KEY ({env_name})", file=sys.stderr)
            failures.append(name); continue

        # generous token budget so nobody truncates mid-path (game config caps at 32)
        tk = ({"max_completion_tokens": 6000}
              if m.get("max_completion_tokens") is not None else {"max_tokens": 6000})
        if m.get("reasoning_effort"):
            tk["reasoning_effort"] = m["reasoning_effort"]
        if m.get("extra_body"):
            tk["extra_body"] = m["extra_body"]

        try:
            client = OpenAI(api_key=key, base_url=m.get("base_url") or None, timeout=180)
            resp = client.chat.completions.create(
                model=m["model"],
                messages=[{"role": "user",
                           "content": PROMPT.format(name=name, color=color)}],
                **tk,
            )
            raw = resp.choices[0].message.content or ""
            svg = sanitize_svg(raw)
            if not svg:
                print(f"  {name:24} NO SVG in response ({len(raw)} chars)", file=sys.stderr)
                failures.append(name); continue
            path = f"crests/{mid}.svg"
            open(path, "w").write(svg)
            results.append({"id": mid, "name": name, "color": color, "file": path,
                            "bytes": len(svg)})
            print(f"  {name:24} ok  ({len(svg)} bytes)")
        except Exception as exc:
            print(f"  {name:24} FAILED ({type(exc).__name__}: {str(exc)[:140]})",
                  file=sys.stderr)
            failures.append(name)

    json.dump(results, open("crests/manifest.json", "w"), indent=2)
    print(f"\nDone: {len(results)} crests, {len(failures)} failed"
          + (f" — {failures}" if failures else ""))


if __name__ == "__main__":
    main()
