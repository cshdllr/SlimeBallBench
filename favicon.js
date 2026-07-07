// Animated favicon: a ball flies in, the slime hops up and heads it off screen,
// then it loops. ~2s loop, drawn to a small canvas whose data URL is swapped into
// the <link rel="icon"> each frame. The jump apex and the ball's arrival are timed
// to coincide, so the header actually connects.
// (Chrome/Firefox/Edge animate live; Safari shows a single frame — still fine.)
(function () {
  // don't bother animating a favicon nobody sees inside the embedded replay
  if (window.top !== window.self) return;
  const SIZE = 32;
  const cv = document.createElement("canvas");
  cv.width = cv.height = SIZE;
  const g = cv.getContext("2d");

  // reuse an existing icon link or make one
  let link = document.querySelector('link[rel~="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/png";

  const SLIME = "#3fb950";     // matches the site's "win" green
  const BALL = "#f0c020";      // warm scoreboard amber
  const GROUND = 25;           // baseline y
  const CX = 12;               // slime centre x
  const BASE_R = 8.5;          // slime radius
  const BR = 3.6;              // ball radius
  const HIT = 0.42;            // loop phase at which the header connects
  const PERIOD = 2000;         // full loop in ms
  const FPS = 15;
  let last = 0;

  // Jump height: airborne 0.28→0.56 with the apex landing exactly on HIT (0.42),
  // so the slime's head is at its highest the instant the ball arrives.
  const jumpAt = p => {
    if (p < 0.28 || p > 0.56) return 0;
    const t = (p - 0.28) / 0.28;
    return Math.sin(t * Math.PI) * 8;
  };
  // squash: a quick crouch just before take-off, a slight stretch in the air.
  const squashAt = p => {
    if (p >= 0.20 && p < 0.28) return 1 - ((p - 0.20) / 0.08) * 0.22;
    if (p >= 0.28 && p <= 0.56) return 0.9 + jumpAt(p) / 8 * 0.12;
    return 1;
  };

  // Contact point: the ball rests ON the dome at the apex — its centre sits one
  // ball-radius above the head's top (GROUND − jump − head radius), so it kisses
  // the crown rather than sinking into the middle of the head.
  const HITX = CX + 1, HITY = GROUND - 8 - BASE_R - BR; // ≈ 5

  function ballPos(p) {
    // fly in from off the right, meeting the head at HIT…
    if (p < HIT) {
      const t = p / HIT;
      return { x: 33 - t * (33 - HITX), y: 9 - t * (9 - HITY), show: true };
    }
    // …then headed up and away off the top-left corner.
    if (p < 0.74) {
      const u = (p - HIT) / (0.74 - HIT);
      const x = HITX - u * (HITX + 8);   // → off the left edge
      const y = HITY - u * (HITY + 14);  // → up and off the top
      return { x, y, show: x > -BR - 2 && y > -BR - 2 };
    }
    return { x: 0, y: 0, show: false };  // off-screen; it comes in again next loop
  }

  function draw(p) {
    g.clearRect(0, 0, SIZE, SIZE);

    // ground line
    g.strokeStyle = "rgba(255,255,255,0.18)";
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(2, GROUND + 1.5);
    g.lineTo(SIZE - 2, GROUND + 1.5);
    g.stroke();

    // slime: a squashable half-circle sitting on the ground
    const lift = jumpAt(p);
    const sq = squashAt(p);
    const rx = BASE_R / Math.sqrt(sq);   // widen when squashed
    const ry = BASE_R * sq;
    const cy = GROUND - lift;

    g.fillStyle = SLIME;
    g.beginPath();
    g.ellipse(CX, cy, rx, ry, 0, Math.PI, 0, false); // upper half only
    g.lineTo(CX - rx, cy);
    g.fill();

    // eye
    g.fillStyle = "#0b0f14";
    g.beginPath();
    g.arc(CX + rx * 0.42, cy - ry * 0.45, 1.5, 0, Math.PI * 2);
    g.fill();

    // ball, always on top (it's either flying in front toward, or off, the head)
    const b = ballPos(p);
    if (b.show) {
      g.fillStyle = BALL;
      g.beginPath();
      g.arc(b.x, b.y, BR, 0, Math.PI * 2);
      g.fill();
    }

    link.href = cv.toDataURL("image/png");
  }

  function tick(now) {
    if (now - last >= 1000 / FPS) {
      last = now;
      draw((now % PERIOD) / PERIOD);
    }
    requestAnimationFrame(tick);
  }
  // pause the loop when the tab is hidden to avoid needless work
  let raf = requestAnimationFrame(tick);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf);
    else raf = requestAnimationFrame(tick);
  });
})();
