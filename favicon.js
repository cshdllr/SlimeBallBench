// Animated favicon: a little slime that crouches, springs up and boots a ball
// out of frame, then settles as the ball rolls back in. ~2s loop, drawn to a
// small canvas whose data URL is swapped into the <link rel="icon"> each frame.
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
  const PERIOD = 2000;         // full loop in ms
  const FPS = 15;
  let last = 0;

  // simple eased jump height over the "airborne" window
  const jumpAt = p => {
    // p in [0,1] across the whole loop; airborne between 0.12 and 0.52
    if (p < 0.12 || p > 0.52) return 0;
    const t = (p - 0.12) / 0.40;       // 0..1 during the hop
    return Math.sin(t * Math.PI) * 11; // up to 11px off the ground
  };
  // squash factor: crouch just before takeoff, stretch at apex
  const squashAt = p => {
    if (p < 0.12) return 1 - (p / 0.12) * 0.28;      // crouch down to 0.72
    if (p <= 0.52) return 0.85 + jumpAt(p) / 11 * 0.2; // stretch in the air
    return 1;
  };

  function ballPos(p) {
    // rest at the slime's feet, get kicked up-and-right at p=0.18, arc off-screen,
    // stay gone briefly, then roll back in from the left to the rest spot.
    if (p < 0.18) return { x: 11, y: GROUND - 3, r: 4.5, show: true };
    if (p < 0.62) {
      const t = (p - 0.18) / 0.44;
      const x = 11 + t * 46;                 // flies right, past the edge
      const y = GROUND - 3 - (34 * t - 34 * t * t) * 1.7; // parabola
      return { x, y, r: 4.5, show: x < SIZE + 6 };
    }
    if (p < 0.72) return { x: 60, y: 0, r: 4.5, show: false }; // off-screen
    const t = (p - 0.72) / 0.28;
    const x = -6 + t * 17;                   // rolls back to rest near the slime
    return { x, y: GROUND - 3, r: 4.5, show: true };
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

    // ball (drawn behind the slime on the way in, in front on the way out)
    const b = ballPos(p);
    const drawBall = () => {
      if (!b.show) return;
      g.fillStyle = BALL;
      g.beginPath();
      g.arc(b.x, b.y, b.r, 0, Math.PI * 2);
      g.fill();
    };
    if (p >= 0.62) drawBall(); // rolling in — behind slime feels natural

    // slime: a squashable half-circle sitting on the ground
    const lift = jumpAt(p);
    const sq = squashAt(p);
    const cx = 11;
    const baseR = 9;
    const rx = baseR / Math.sqrt(sq);   // widen when squashed
    const ry = baseR * sq;
    const cy = GROUND - lift;

    g.fillStyle = SLIME;
    g.beginPath();
    g.ellipse(cx, cy, rx, ry, 0, Math.PI, 0, false); // upper half only
    g.lineTo(cx - rx, cy);
    g.fill();

    // eye
    g.fillStyle = "#0b0f14";
    g.beginPath();
    g.arc(cx + rx * 0.42, cy - ry * 0.45, 1.5, 0, Math.PI * 2);
    g.fill();

    if (p < 0.62) drawBall(); // launched — in front of the slime

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
