/**
 * Drone Vision HUD - fighter-jet style OSD drawn over the WebRTC video.
 *
 * Layout comes from HUD_UI_SPEC.md. Everything tunable is in HUD_CONFIG below;
 * the drawing code only reads from it. Positions are fractions of the frame so
 * the same numbers work at 1280x720 and 1920x1080.
 *
 * House rules (override the reference image):
 *   - No filled backgrounds behind anything. Boxes are thin outlines.
 *   - Text is legible through a dark shadow halo, not a panel behind it.
 *   - Three text sizes only.
 *   - Equal-width digits, so numbers do not jitter as they change.
 *   - Number and unit share one baseline.
 *   - Everything stays at least `margin` inside the frame.
 *   - Any two labels that would overlap: the lower-priority one is dropped.
 *
 * This module draws. It does not fetch anything: the page hands it a telemetry
 * snapshot and a WebRTC stats object once per frame.
 */

/* ====================================================================== *
 * CONFIG - the whole look lives here
 * ====================================================================== */
export const HUD_CONFIG = {
  /**
   * Faint dark strips behind the top and bottom rows, as in the reference.
   * OFF by default: the brief forbids filled backgrounds. Turn on only if you
   * want the reference look back; nothing else changes.
   */
  backgrounds: false,

  // vertical centre of the ladder, both tapes and their value boxes
  centerY: 0.52,

  // Distance kept clear of every frame edge, as a fraction of each dimension.
  margin: 0.02,

  /**
   * Exactly three text sizes, as a fraction of frame height.
   *   s = labels, units, panel rows
   *   m = readouts and headings
   *   l = the primary numbers (speed, altitude, battery)
   */
  text: { s: 0.0135, m: 0.0200, l: 0.0310 },

  /**
   * Equal-width digits are the point here, so this must be a monospaced
   * family. DejaVu Sans Mono ships with Raspberry Pi OS and most Linux
   * desktops, so nothing is fetched over the network.
   */
  font: '"DejaVu Sans Mono", "Liberation Mono", Menlo, Consolas, monospace',

  // ---- heading tape ----
  heading: {
    x0: 0.38, x1: 0.61,
    labelY: 0.170,       // numeric labels
    tickY: 0.192,        // tick marks
    valueY: 0.228,       // current heading, below the tape
    labelEvery: 10,      // numeric labels every N degrees
    tickEvery: 5,
    unwrapped: true,     // true = 290 300 310 ... 370 380 (matches reference)
  },

  // ---- pitch ladder ----
  // Section 3 places the window at 44.5%..56%. The rung numbers sit at the
  // outer end of each half, and _drawLadder() shortens the rungs so the widest
  // label ("-90") still fits inside this width at any resolution.
  ladder: {
    x0: 0.445, x1: 0.56,
    y0: 0.39, y1: 0.61,
    pxPerDeg: 0.0073,    // x frame height, per degree
    stepDeg: 5,
    centreGap: 0.05,     // x frame width, total gap between the two halves
    halfLen: 0.029,      // x frame width, maximum length of each half

    /**
     * A faint light-grey wash behind the attitude indicator, so the ladder
     * stays readable against a bright sky. This is the one deliberate
     * exception to the "no backgrounds" rule, and it is confined to the
     * ladder window, under the rungs. Set to null to remove it.
     */
    wash: "rgba(205, 210, 216, 0.15)",
  },

  // ---- speed tape ----
  speed: {
    lineX: 0.367,
    top: 0.33, bottom: 0.69,
    pxPerUnit: 0.009,    // x frame height
    labelEvery: 20,
    tickEvery: 10,
    boxX0: 0.25, boxX1: 0.35,
    // a copter has no airspeed sensor: default to groundspeed
    source: "groundspeed",  // "groundspeed" | "airspeed"
    // Same faint wash as the pitch ladder, behind the tape and its numbers.
    wash: "rgba(205, 210, 216, 0.15)",
    /**
     * The box's OUTER edge (the one away from the tape) fades out instead of
     * ending in a hard vertical line. Length as a fraction of frame width:
     * the grey wash dissolves over this distance, and the outer two box edges
     * are drawn with a gradient that reaches zero at the same point.
     */
    fade: 0.045,
  },

  // ---- altitude tape ----
  alt: {
    lineX: 0.633,
    top: 0.33, bottom: 0.69,
    pxPerUnit: 0.009,
    labelEvery: 10,
    tickEvery: 5,
    boxX0: 0.65, boxX1: 0.75,
    source: "alt_rel",      // "alt_rel" | "alt_msl"
    wash: "rgba(205, 210, 216, 0.15)",
    fade: 0.045,            // see the speed tape
  },

  // ---- text under the speed tape ----
  // One line only: the bearing to home, labelled HOME.
  underSpeed: { x: 0.367, y0: 0.715 },

  // ---- bottom strip ----
  bottom: {
    /**
     * Radius of the bottom-row icons, as a fraction of frame height. Row 2 is
     * anchored from this radius (not from a fixed percentage) so an enlarged
     * icon still cannot cross the bottom margin.
     */
    iconRadius: 0.018,
  },

  // ---- panels ----
  panels: {
    radio: { x0: 0.57, x1: 0.75, y0: 0.55, y1: 0.90 },
    efficiency: { x0: 0.44, x1: 0.53, y0: 0.81, y1: 0.90 },
    decoder: { x0: 0.78, x1: 0.98, y0: 0.44, y1: 0.91 },
    columnGap: 0.012,    // x frame width between a label and its number
  },

  // ---- edge strips (only drawn when backgrounds is true) ----
  strips: {
    top: { y0: 0, y1: 0.09 },
    bottom: { y0: 0.93, y1: 1.0 },
  },

  // ---- style ----
  style: {
    fg: "rgba(255,255,255,0.90)",
    dim: "rgba(255,255,255,0.58)",
    faint: "rgba(255,255,255,0.34)",
    red: "#ff3b30",
    green: "#43e08a",
    amber: "#ffb020",
    shadow: "rgba(0,0,0,0.62)",
    lineW: 0.0015,            // x frame height
    stripBg: "rgba(0,0,0,0.25)",
    staleAlpha: 0.40,
  },

  // battery: APM 2.8 does not report cell count, so it is configured
  battery: { cells: 3, warnV: 3.5, dangerV: 3.3 },
};

/* ====================================================================== *
 * Small helpers
 * ====================================================================== */
const TAU = Math.PI * 2;

/** Linear interpolation. */
const lerp = (a, b, t) => a + (b - a) * t;

/** Interpolate an angle along the shortest path, handling 359 -> 0. */
function lerpAngle(cur, target, t) {
  const d = ((target - cur + 540) % 360) - 180;
  return (cur + d * t + 360) % 360;
}

/** True only for real numbers. null/undefined/NaN must render as "---". */
const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/** Format a number, or "---" when it is unknown. Fixed decimal places. */
function fmt(v, places) {
  return isNum(v) ? v.toFixed(places) : "---";
}

/** Integer format, or "---". */
function fmt0(v) {
  return isNum(v) ? String(Math.round(v)) : "---";
}

/* ====================================================================== *
 * Units. `places` is fixed per field so the width never changes.
 * ====================================================================== */
const UNITS = {
  metric: {
    speed: { f: 3.6, label: "km/h", places: 1 },
    alt: { f: 1, label: "m", places: 0 },
    vs: { f: 1, label: "m/s", places: 1 },
    dist: { f: 1, label: "m", places: 0 },
  },
  imperial: {
    speed: { f: 2.23694, label: "mph", places: 1 },
    alt: { f: 3.28084, label: "ft", places: 0 },
    vs: { f: 3.28084, label: "ft/s", places: 1 },
    dist: { f: 3.28084, label: "ft", places: 0 },
  },
};

/** Distance with automatic km/mile rollover above 10 km. */
function fmtDistance(metres, units) {
  if (!isNum(metres)) return "---";
  if (units === "metric") {
    return metres >= 10000
      ? `${(metres / 1000).toFixed(1)} km`
      : `${Math.round(metres)} m`;
  }
  const feet = metres * 3.28084;
  return feet >= 52800
    ? `${(metres / 1609.34).toFixed(1)} mi`
    : `${Math.round(feet)} ft`;
}

/* ====================================================================== *
 * HUD
 * ====================================================================== */
export class HUD {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: true });
    this.cfg = HUD_CONFIG;

    this.W = 0;
    this.H = 0;
    this.dpr = 1;

    // interpolated state
    this.s = { roll: 0, pitch: 0, heading: 0, speed: 0, alt: 0 };
    this._init = false;

    // current snapshot + WebRTC stats, set by update()
    this.data = null;
    this.stats = null;
    this.linkRttMs = null;
    this.latAvg = 0;
    this.live = false;

    // panel visibility, persisted
    this.showPanels = { radio: false, efficiency: false, decoder: false };
    this.hudVisible = true;
    this.units = "metric";
    this.altSource = this.cfg.alt.source;
    this._loadPrefs();

    // claims registry for the overlap check, reset every frame
    this._claims = [];

    // telemetry source state, filled in by the page
    this.sourceMode = null;
    this.sourceError = null;
    this.sourcePending = false;
    this.onToggleSim = null;
    this._chipHit = null;

    // decoder histogram: frames received per 30 ms bucket
    this._buckets = new Array(80).fill(0);
    this._bucketIdx = 0;
    this._lastFrames = null;

    this._bindKeys();
    this._bindClicks();
    this.W = window.innerWidth;
    this.H = window.innerHeight;
    this.resize();
  }

  /**
   * The canvas sits over the video with pointer-events: none, so clicks are
   * taken on the window and hit-tested against the chip's last drawn box.
   */
  _bindClicks() {
    window.addEventListener("click", (ev) => {
      const h = this._chipHit;
      if (!h) return;
      if (ev.clientX < h.x0 || ev.clientX > h.x1 ||
          ev.clientY < h.y0 || ev.clientY > h.y1) return;
      this.toggleSim();
    });
  }

  /* ------------------------------------------------------------------ *
   * Preferences (localStorage, wrapped because it can throw)
   * ------------------------------------------------------------------ */
  _loadPrefs() {
    try {
      const raw = localStorage.getItem("droneHud");
      if (!raw) return;
      const p = JSON.parse(raw);
      if (p && typeof p === "object") {
        if (p.showPanels) Object.assign(this.showPanels, p.showPanels);
        if (p.units === "imperial" || p.units === "metric") this.units = p.units;
        if (p.altSource) this.altSource = p.altSource;
        if (typeof p.hudVisible === "boolean") this.hudVisible = p.hudVisible;
      }
    } catch (e) { /* private mode, quota, bad JSON: defaults are fine */ }
  }

  _savePrefs() {
    try {
      localStorage.setItem("droneHud", JSON.stringify({
        showPanels: this.showPanels,
        units: this.units,
        altSource: this.altSource,
        hudVisible: this.hudVisible,
      }));
    } catch (e) { /* ignore */ }
  }

  /* ------------------------------------------------------------------ *
   * Keyboard
   * ------------------------------------------------------------------ */
  _bindKeys() {
    window.addEventListener("keydown", (ev) => {
      // never steal keys from the colour panel's inputs
      const t = ev.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" ||
                t.tagName === "TEXTAREA")) return;

      switch (ev.key.toLowerCase()) {
        case "h": this.hudVisible = !this.hudVisible; break;
        case "r": this.showPanels.radio = !this.showPanels.radio; break;
        case "e": this.showPanels.efficiency = !this.showPanels.efficiency; break;
        case "v": this.showPanels.decoder = !this.showPanels.decoder; break;
        case "u":
          this.units = this.units === "metric" ? "imperial" : "metric";
          break;
        case "a":
          this.altSource = this.altSource === "alt_rel" ? "alt_msl" : "alt_rel";
          break;
        case "f":
          if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen?.().catch(() => {});
          } else {
            document.exitFullscreen?.().catch(() => {});
          }
          break;
        case "s":
          // Toggle the simulator. The HUD does no network I/O itself: the
          // page owns the request and calls back with the outcome.
          this.toggleSim();
          break;
        default:
          return;
      }
      ev.preventDefault();
      this._savePrefs();
    });
  }

  /* ------------------------------------------------------------------ *
   * Simulator toggle (key S, or the SIM chip in the top centre)
   *
   * The HUD never talks to the network. The page installs `onToggleSim`
   * and calls `setSourceMode()` with the result, so the display always
   * reflects what the service actually did rather than what was requested.
   * ------------------------------------------------------------------ */
  toggleSim() {
    if (typeof this.onToggleSim === "function") {
      // The page owns the request, the error handling and the badge update.
      return this.onToggleSim();
    }
    return undefined;
  }

  /** Record what the service reports, so the chip and tooltip stay truthful. */
  setSourceMode(status) {
    if (!status || typeof status !== "object") return;
    this.sourceMode = status.mode || this.sourceMode;
    this.sourceError = status.error || null;
    this.sourcePending = false;
    if (status.error) {
      // Show the refusal briefly: this is the "real link is live" case.
      this._sourceErrorUntil = performance.now() + 5000;
    }
  }

  /** True while a toggle request is in flight, so the chip can dim. */
  setSourcePending(pending) {
    this.sourcePending = !!pending;
  }

  /* ------------------------------------------------------------------ *
   * Sizing
   * ------------------------------------------------------------------ */
  resize() {    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.W = window.innerWidth;
    this.H = window.innerHeight;
    this.canvas.width = Math.round(this.W * this.dpr);
    this.canvas.height = Math.round(this.H * this.dpr);
    this.canvas.style.width = this.W + "px";
    this.canvas.style.height = this.H + "px";
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this._cacheMetrics();
  }

  _cacheMetrics() {
    // One place that converts the config's frame fractions into pixels.
    const { W, H } = this;
    const c = this.cfg;
    const mx = W * c.margin;
    const my = H * c.margin;

    this.m = {
      lineW: Math.max(1, c.style.lineW * H),
      cx: W / 2,
      cy: H * c.centerY,
      // usable frame after the margin
      left: mx, right: W - mx,
      top: my, bottom: H - my,
      headingX0: W * c.heading.x0,
      headingX1: W * c.heading.x1,
      speedX: W * c.speed.lineX,
      altX: W * c.alt.lineX,
      speedTop: H * c.speed.top,
      speedBottom: H * c.speed.bottom,
      ladderX0: W * c.ladder.x0,
      ladderX1: W * c.ladder.x1,
      ladderY0: H * c.ladder.y0,
      ladderY1: H * c.ladder.y1,
      ladderPxDeg: H * c.ladder.pxPerDeg,
      gapPx: W * c.ladder.centreGap,
      halfPx: W * c.ladder.halfLen,
      speedPxUnit: H * c.speed.pxPerUnit,
      altPxUnit: H * c.alt.pxPerUnit,
      ss: this._sizePx("s"),
      sm: this._sizePx("m"),
      sl: this._sizePx("l"),
    };
  }

  /** Pixel size for one of the three text steps. */
  _sizePx(step) {
    const t = this.cfg.text;
    const frac = typeof step === "number" ? step : (t[step] ?? t.m);
    return frac * this.H;
  }

  /* ------------------------------------------------------------------ *
   * Input
   * ------------------------------------------------------------------ */
  update(data, stats, linkRttMs, latAvg, live) {
    this.data = data;
    this.stats = stats || null;
    this.linkRttMs = linkRttMs;
    this.latAvg = latAvg || 0;
    // The page owns the freshness test (WebSocket open + recent message), so
    // it is passed in rather than guessed here.
    this.live = typeof live === "boolean" ? live : !!(data && data.link_ok);
  }

  /** True when telemetry is fresh enough to be trusted. */
  isLive() {
    return !!this.live;
  }

  /* ------------------------------------------------------------------ *
   * Text, with overlap suppression
   * ------------------------------------------------------------------ */
  _font(sizePx, weight = 400) {
    return `${weight} ${Math.max(6, sizePx)}px ${this.cfg.font}`;
  }

  /**
   * Draw text with the dark halo that keeps it readable over bright sky.
   *
   * opts.size  : "s" | "m" | "l"
   * opts.align : "left" | "right" | "center"
   * opts.prio  : 1..3, see _claim
   * opts.bg    : optional thin outline box drawn around the label
   * opts.noClaim : draw without reserving space (used by _valueUnit, which
   *                reserves one box for the number and its unit together)
   */
  text(str, x, y, opts = {}) {
    const ctx = this.ctx;
    const sizePx = this._sizePx(opts.size || "m");
    const weight = opts.weight || 400;
    const align = opts.align || "left";
    const prefix = opts.prefix || "";

    ctx.font = this._font(sizePx, weight);
    const text = prefix + str;
    const w = ctx.measureText(text).width;

    const left = align === "right" ? x - w : align === "center" ? x - w / 2 : x;
    const right = left + w;
    const top = y - sizePx * 0.60;
    const bottom = y + sizePx * 0.60;

    if (!opts.noClaim) {
      const pad = sizePx * 0.10;
      if (!this._claimScreen(left - pad, top, right + pad, bottom,
                             opts.prio ?? 2)) {
        return;                     // hidden: a higher-priority label is here
      }
    }

    if (opts.bg) {
      // Thin outline only. Never a fill.
      ctx.beginPath();
      ctx.strokeStyle = opts.bgColor || this.cfg.style.dim;
      ctx.lineWidth = Math.max(1, this.m.lineW);
      const b = sizePx * 0.42;
      ctx.rect(left - b, y - sizePx * 0.86, w + b * 2, sizePx * 1.72);
      ctx.stroke();
    }

    ctx.textAlign = align;
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.miterLimit = 2;

    ctx.lineWidth = Math.max(2, this.H * 0.0022);
    ctx.strokeStyle = this.cfg.style.shadow;
    ctx.strokeText(text, x, y);

    ctx.fillStyle = opts.color || this.cfg.style.fg;
    ctx.fillText(text, x, y);
  }

  /**
   * Reserve a screen-space box for a piece of text. Returns false when
   * something already placed with equal or higher priority overlaps it.
   *
   * Priorities: 3 = primary readouts, 2 = labels, 1 = optional extras.
   *
   * The box passed in is in the canvas' current LOCAL space (the ladder draws
   * under a translate/rotate), so it is mapped through the active transform
   * first; otherwise a rotated label would claim a rectangle near the origin
   * and collide with everything (the reference's "NW hits 310" problem).
   *
   * `this._claimClip`, when set, is the screen-space clip currently in force.
   * A label outside it is never painted, so it must not reserve space either.
   */
  _claimScreen(x0, y0, x1, y1, prio) {
    const t = this.ctx.getTransform();
    const pts = [[x0, y0], [x1, y0], [x0, y1], [x1, y1]];
    let sx0 = Infinity, sy0 = Infinity, sx1 = -Infinity, sy1 = -Infinity;
    for (const [px, py] of pts) {
      const sx = px * t.a + py * t.c + t.e;
      const sy = px * t.b + py * t.d + t.f;
      if (sx < sx0) sx0 = sx;
      if (sx > sx1) sx1 = sx;
      if (sy < sy0) sy0 = sy;
      if (sy > sy1) sy1 = sy;
    }

    const clip = this._claimClip;
    if (clip) {
      sx0 = Math.max(sx0, clip.x0);
      sy0 = Math.max(sy0, clip.y0);
      sx1 = Math.min(sx1, clip.x1);
      sy1 = Math.min(sy1, clip.y1);
      if (sx0 >= sx1 || sy0 >= sy1) return false;   // fully clipped away
    }

    for (const c of this._claims) {
      if (c.x1 <= sx0 || c.x0 >= sx1 || c.y1 <= sy0 || c.y0 >= sy1) continue;
      if (c.prio >= prio) return false;
    }
    this._claims.push({ x0: sx0, y0: sy0, x1: sx1, y1: sy1, prio });
    return true;
  }

  /** Measured width of a string at one of the three text steps. */
  _textWidth(str, size, weight = 400) {
    const ctx = this.ctx;
    const prev = ctx.font;
    ctx.font = this._font(this._sizePx(size), weight);
    let w = 0;
    try { w = ctx.measureText(str).width; } catch (e) { w = str.length * 8; }
    ctx.font = prev;
    return w;
  }

  /**
   * Number and unit on one baseline, right-aligned so the pair ends at `x`.
   * Returns the total measured width, so callers can lay out left of it.
   *
   * The pair claims ONE box between them. Claiming separately made the unit
   * lose the collision check against its own number and vanish.
   */
  _valueUnit(value, unit, x, y, opts = {}) {
    const size = opts.size || "m";
    const unitSize = opts.unitSize || "s";
    const weight = opts.weight || 600;
    // Space between number and unit: scale with the SMALLER of the two, so a
    // big number next to a small unit does not butt right up against it.
    const gap = this._sizePx(unitSize) * 0.55;

    const unitW = this._textWidth(unit, unitSize, 400);
    const valueW = this._textWidth(value, size, weight);
    const total = valueW + gap + unitW;

    const align = opts.align || "right";
    let start;
    if (align === "right") start = x - total;
    else if (align === "center") start = x - total / 2;
    else start = x;

    // One reservation for the whole pair.
    const mainPx = this._sizePx(size);
    const top = y - mainPx * 0.62;
    const bottom = y + mainPx * 0.62;
    if (!this._claimScreen(start, top, start + total, bottom,
                           opts.prio ?? 2)) {
      return total;
    }

    this.text(value, start, y,
              { size, weight, align: "left", color: opts.color,
                prio: opts.prio, noClaim: true });
    this.text(unit, start + valueW + gap, y,
              { size: unitSize, align: "left",
                color: opts.unitColor || this.cfg.style.dim, noClaim: true });
    return total;
  }

  /**
   * Reserve a screen-space rectangle as an exclusion zone, so lower-priority
   * labels are not drawn inside it.
   *
   * Used for the speed and altitude value boxes: they are outlines now (no
   * fill), so tape numbers that fall behind them would show through and print
   * on top of the readout. Reserving the box makes the overlap check drop
   * them, which is exactly the "hide the lower-priority element" rule.
   */
  _reserveZone(x, y, w, h, prio = 2) {
    this._claims.push({ x0: x, y0: y, x1: x + w, y1: y + h, prio });
  }

  /**
   * The tape wash: a light-grey rectangle clipped to the tape strip that runs
   * from the tape out past the value box and dissolves into the margin.
   *
   * `inX` is the end nearest the tape; `boxX` is the box's OUTER edge (the one
   * facing the frame margin) and `fade` is how far the dissolve continues past
   * it. On the speed tape the outer end is the left one, on the altitude tape
   * the right one, so the sign of (boxX - inX) sets the direction.
   */
  _tapeWash(inX, boxX, fade, colour) {
    if (!colour) return;
    const ctx = this.ctx;
    const m = this.m;
    const dir = Math.sign(boxX - inX) || 1;
    const endX = boxX + dir * fade;
    const total = Math.abs(endX - inX);
    const solid = Math.abs(boxX - inX);

    const g = ctx.createLinearGradient(inX, 0, endX, 0);
    g.addColorStop(0, colour);
    g.addColorStop(solid / total, colour);
    g.addColorStop(1, "rgba(205, 210, 216, 0)");
    ctx.fillStyle = g;
    ctx.fillRect(Math.min(inX, endX), m.speedTop,
                 total, m.speedBottom - m.speedTop);
  }

  /**
   * Value box with no filled background. The edge nearest the tape is solid;
   * the two horizontal rules run outward and dissolve, so the box has NO hard
   * vertical boundary out in the margin — it simply opens out and fades.
   *
   * `side` is -1 when the tape is to the RIGHT of the box (speed tape) and
   * +1 when it is to the LEFT (altitude tape).
   */
  _fadeBox(x0, x1, y0, y1, side, fade, colour) {
    const ctx = this.ctx;
    const col = colour || this.cfg.style.dim;
    const lw = Math.max(1, this.m.lineW);
    const outer = side < 0 ? x0 : x1;     // the end that fades
    const inner = side < 0 ? x1 : x0;     // the end against the tape
    const len = Math.abs(inner - outer);
    const f = Math.min(fade, len * 0.9);

    ctx.save();
    ctx.lineWidth = lw;

    // solid edge against the tape, full height
    this._line(inner, y0, inner, y1, col, lw);

    // the two horizontal rules: solid near the tape, dissolving outward.
    // There is deliberately no outer vertical edge: the outline opens out.
    for (const y of [y0, y1]) {
      const g = ctx.createLinearGradient(inner, 0, outer, 0);
      g.addColorStop(0, col);
      g.addColorStop(Math.max(0, 1 - f / len), col);
      g.addColorStop(1, "rgba(255,255,255,0)");
      ctx.strokeStyle = g;
      ctx.beginPath();
      ctx.moveTo(inner, y);
      ctx.lineTo(outer, y);
      ctx.stroke();
    }

    ctx.restore();
  }

  _line(x1, y1, x2, y2, color, width, dash) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.strokeStyle = color || this.cfg.style.fg;
    ctx.lineWidth = width || this.m.lineW;
    ctx.setLineDash(dash || []);
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  /** Thin outline box. There is deliberately no filled variant. */
  _box(x, y, w, h, color) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.strokeStyle = color || this.cfg.style.dim;
    ctx.lineWidth = Math.max(1, this.m.lineW);
    ctx.rect(x, y, w, h);
    ctx.stroke();
  }

  /* ------------------------------------------------------------------ *
   * Frame
   * ------------------------------------------------------------------ */
  draw() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.W, this.H);
    if (!this.hudVisible) return;

    this._claims = [];

    const d = this.data || {};
    const live = this.isLive();
    const altKey = this.altSource;
    const alt = isNum(d[altKey]) ? d[altKey] : (isNum(d.alt_rel) ? d.alt_rel : d.alt);

    // ---- interpolate -------------------------------------------------
    // Telemetry arrives at ~20 Hz and the display runs at ~60 Hz, so each
    // frame moves a fraction of the way to the target (~100 ms settle).
    const k = this._init ? 0.22 : 1;
    this.s.roll = lerp(this.s.roll, isNum(d.roll) ? d.roll : 0, k);
    this.s.pitch = lerp(this.s.pitch, isNum(d.pitch) ? d.pitch : 0, k);
    this.s.heading = lerpAngle(this.s.heading, isNum(d.heading) ? d.heading : 0, k);
    this.s.speed = lerp(this.s.speed, isNum(d.groundspeed) ? d.groundspeed : 0, k);
    this.s.alt = lerp(this.s.alt, isNum(alt) ? alt : 0, k);
    this._init = true;

    if (this.cfg.backgrounds) this._drawStrips();
    this._drawStatusChip(d, live);

    // Flight data dims to 40% when the link is down. Link and Pi system data
    // are still true without a flight controller, so they stay full strength.
    ctx.save();
    if (!live) ctx.globalAlpha = this.cfg.style.staleAlpha;
    this._drawFlightCluster(d, live);
    ctx.restore();

    this._drawSystemCluster(d, live);

    ctx.save();
    if (!live) ctx.globalAlpha = this.cfg.style.staleAlpha;
    this._drawHeading(d, live);
    this._drawLadder(live);
    this._drawSpeedTape(d, live);
    this._drawAltTape(d, live);
    this._drawUnderSpeed(d, live);
    this._drawBottomStrip(d, live);
    if (this.showPanels.efficiency) this._drawEfficiencyPanel(d, live);
    ctx.restore();

    // Panels that report link/video health do not depend on the FC.
    if (this.showPanels.radio) this._drawRadioPanel(d, live);
    if (this.showPanels.decoder) this._drawDecoderPanel(d, live);
  }

  /* ------------------------------------------------------------------ *
   * Optional reference-style strips (off by default)
   * ------------------------------------------------------------------ */
  _drawStrips() {
    const { W, H } = this;
    const c = this.cfg.strips;

    const g = this.ctx.createLinearGradient(0, 0, 0, H * c.top.y1);
    g.addColorStop(0, this.cfg.style.stripBg);
    g.addColorStop(1, "rgba(0,0,0,0)");
    this.ctx.fillStyle = g;
    this.ctx.fillRect(0, 0, W, H * c.top.y1);

    const gb = this.ctx.createLinearGradient(0, H * c.bottom.y0, 0, H);
    gb.addColorStop(0, "rgba(0,0,0,0)");
    gb.addColorStop(0.35, this.cfg.style.stripBg);
    gb.addColorStop(1, this.cfg.style.stripBg);
    this.ctx.fillStyle = gb;
    this.ctx.fillRect(0, H * c.bottom.y0, W, H * (1 - c.bottom.y0));
  }

  /* ------------------------------------------------------------------ *
   * Link status chip (top centre)
   *
   * The spec asks for a red centred "NO TELEMETRY" banner. A large banner
   * over the video is what the user asked us to avoid, so this is one small
   * label. Missing values are already visible as "---" on every instrument.
   * ------------------------------------------------------------------ */
  _drawStatusChip(d, live) {
    const { W, H } = this;
    const c = this.cfg.style;

    const sim = !!(d.sim || d.source === "sim");
    let label, color;
    if (!live) { label = "NO FC"; color = c.dim; }
    else if (sim) { label = "SIM"; color = c.amber; }
    else { label = "LIVE"; color = c.green; }

    // Button affordance: the chip is clickable, so it gets a thin outline and
    // a hint of the action. No fill (house rule), just the outline.
    const y = H * 0.082;
    const sizePx = this._sizePx("m");
    const padX = sizePx * 0.55;
    const padY = sizePx * 0.34;

    this.ctx.font = this._font(sizePx, 700);
    const w = this.ctx.measureText(label).width;
    this._chipHit = {
      x0: W / 2 - w / 2 - padX, x1: W / 2 + w / 2 + padX,
      y0: y - padY, y1: y + sizePx * 0.62 + padY,
    };

    // An error (e.g. refusing to simulate over a live link) takes the chip
    // for a few seconds, so the refusal is visible where the toggle lives.
    const showErr = this.sourceError &&
                    performance.now() < (this._sourceErrorUntil || 0);
    const shown = showErr ? "SIM BLOCKED" : label;
    const shownColor = showErr ? c.red : color;

    if (this.sourcePending) {
      this.ctx.save();
      this.ctx.globalAlpha *= 0.55;
    }
    this._box(this._chipHit.x0, this._chipHit.y0,
              this._chipHit.x1 - this._chipHit.x0,
              this._chipHit.y1 - this._chipHit.y0,
              shownColor);
    this.text(shown, W / 2, y,
              { size: "m", weight: 700, align: "center",
                color: shownColor, prio: 3 });
    if (this.sourcePending) this.ctx.restore();

    // Hint under the chip: what pressing S will do right now.
    if (!showErr) {
      const hint = sim ? "S: back to real" : "S: simulate";
      this.text(hint, W / 2, this._chipHit.y1 + sizePx * 0.85,
                { size: "s", align: "center",
                  color: c.faint, prio: 1 });
    }
  }

  /* ------------------------------------------------------------------ *
   * 4.1 Top-left status cluster (flight-controller data)
   *
   * Kept inside the frame margin (`m.top`), not pinned to y = 3% of the
   * frame: at 1080p that put the top row 6 px from the edge.
   * ------------------------------------------------------------------ */
  _drawFlightCluster(d, live) {
    const { W, H } = this;
    const s = H / 1080;
    const m = this.m;
    // The GPS cluster stacks a small fix label ABOVE the big satellite count,
    // so the row has to start low enough for that label to clear the margin.
    const y = m.top + this._sizePx("l") * 0.62 + this._sizePx("s") * 0.62;
    let x = m.left;

    // ---------------- GPS cluster ----------------
    this._satIcon(x + 8 * s, y, 10 * s, this._fixColor(d, live));
    x += 24 * s;

    // With no flight controller the default sats value is 0, which is not a
    // measurement - it must read "---" like every other missing value.
    const fixLabel = !live ? "---"
      : ({ 0: "NO FIX", 1: "NO FIX", 2: "2D", 3: "3D", 4: "DGPS",
           5: "RTK FLT", 6: "RTK FIX" }[d.gps_fix] || "NO FIX");
    const fixColor = this._fixColor(d, live);
    this.text(fixLabel, x, y - 15 * s, { size: "s", color: fixColor, weight: 600 });

    const satsTxt = fmt0(live ? d.sats : null);
    this.text(satsTxt, x, y + 10 * s,
              { size: "l", weight: 700, color: fixColor, prio: 3 });
    x += this._textWidth(satsTxt, "l", 700) + 8 * s;

    if (live && isNum(d.hdop)) {
      const hd = `HDOP ${d.hdop.toFixed(1)}`;
      this.text(hd, x, y + 14 * s,
                { size: "s", color: this._hdopColor(d), prio: 1 });
      x += this._textWidth(hd, "s") + 12 * s;
    }
    x = Math.max(x, this.m.left + 150 * s);

    // ---------------- flight mode box (outline only) ----------------
    const mode = live && d.mode_code ? d.mode_code : "---";
    this.text(mode, x, y + 1 * s,
              { size: "m", weight: 700, align: "left", prio: 3,
                bg: true, bgColor: this.cfg.style.dim });
    x += Math.max(70 * s, this._textWidth(mode, "m", 700) + 22 * s) + 16 * s;

    // ---------------- flight timer ----------------
    this._stopwatch(x + 5 * s, y, 10 * s);
    x += 18 * s;

    const ft = live && isNum(d.flight_time_s) ? d.flight_time_s : null;
    const mm = ft === null ? "--" : String(Math.floor(ft / 60)).padStart(2, "0");
    const ss = ft === null ? "--" : String(Math.floor(ft % 60)).padStart(2, "0");
    const timer = `${mm}:${ss}`;
    this.text(timer, x, y, { size: "m", weight: 700, prio: 2 });
    x += this._textWidth(timer, "m", 700) + 12 * s;

    // arm state
    if (live) {
      const armed = !!d.armed;
      this.text(armed ? "ARMED" : "DISARMED", x, y,
                { size: "s", weight: 700, prio: 2,
                  color: armed ? this.cfg.style.red : this.cfg.style.dim });
    } else {
      this.text("---", x, y, { size: "s", prio: 1 });
    }
  }

  /* ------------------------------------------------------------------ *
   * 4.2 Top-right link and system strip (no FC needed)
   *
   * Laid out right-to-left from the measured text widths, never from fixed
   * offsets: units change width between metric and imperial.
   * ------------------------------------------------------------------ */
  _drawSystemCluster(d, live) {
    const { W, H } = this;
    const s = H / 1080;
    const x1 = this.m.right;
    const y = this.m.top + this._sizePx("l") * 0.62;

    const wifi = (d.wifi) || {};
    let cx = x1;

    // ---- video bitrate: the headline number, red like the reference ----
    const videoMbps = (this.stats && isNum(this.stats.bitrate) && this.stats.bitrate > 0)
      ? this.stats.bitrate
      : (d.video && isNum(d.video.actual_kbps) ? d.video.actual_kbps / 1000 : null);
    cx -= this._valueUnit(isNum(videoMbps) ? videoMbps.toFixed(1) : "---",
                          "Mbps", cx, y,
                          { size: "m", weight: 700, color: this.cfg.style.red,
                            prio: 3 });
    cx -= 20 * s;

    // ---- frame rate: not in the reference, but the first thing to check ----
    const fps = this.stats && isNum(this.stats.fps) ? this.stats.fps : null;
    cx -= this._valueUnit(fps === null ? "---" : fps.toFixed(0), "fps",
                          cx, y,
                          { size: "m", weight: 600,
                            color: fps !== null && fps < 15
                              ? this.cfg.style.amber : this.cfg.style.fg,
                            prio: 2 });
    cx -= 18 * s;

    // ---- signal bars ----
    this._signalBars(cx - 12 * s, y, 18 * s, this._barsFor(wifi.signal_dbm));
    cx -= 32 * s;

    // ---- frequency ----
    const freq = isNum(wifi.freq_mhz) ? `${wifi.freq_mhz} MHz` : "--- MHz";
    this.text(freq, cx, y, { size: "s", align: "right",
                             color: this.cfg.style.dim, prio: 1 });
    cx -= this._textWidth(freq, "s") + 14 * s;

    // ---- signal strength ----
    cx -= this._valueUnit(isNum(wifi.signal_dbm) ? String(wifi.signal_dbm) : "---",
                          "dBm", cx, y,
                          { size: "m", weight: 600, prio: 2 });
    cx -= 14 * s;

    // ---- second row: Pi system, also right-aligned ----
    const sy = y + 34 * s;
    const sys = (d.sys) || {};
    const temp = sys.cpu_temp_c;
    const tempColor = !isNum(temp) ? this.cfg.style.fg
      : temp >= 80 ? this.cfg.style.red
      : temp >= 70 ? this.cfg.style.amber
      : this.cfg.style.fg;

    const thr = sys.throttled && sys.throttled.live;
    const throttling = !!(thr && (thr.throttled || thr.freq_capped || thr.under_voltage));

    let sx = x1;
    sx -= this._valueUnit(isNum(temp) ? temp.toFixed(0) : "---", "C",
                          sx, sy,
                          { size: "s", weight: 600,
                            color: tempColor, unitColor: tempColor, prio: 2 });
    sx -= 14 * s;

    sx -= this._valueUnit(isNum(sys.cpu_mhz) ? String(Math.round(sys.cpu_mhz)) : "---",
                          "MHz", sx, sy,
                          { size: "s", color: this.cfg.style.dim, prio: 1 });
    sx -= 14 * s;

    sx -= this._valueUnit(isNum(sys.cpu_pct) ? String(Math.round(sys.cpu_pct)) : "---",
                          "%", sx, sy,
                          { size: "s", prio: 1 });
    sx -= 20 * s;

    this._cpuIcon(sx, sy, 7 * s);
    sx -= 20 * s;

    if (throttling) this._warnIcon(sx, sy, 8 * s);
  }

  _fixColor(d, live) {
    if (!live) return this.cfg.style.dim;
    const fix = d.gps_fix || 0;
    if (fix < 2) return this.cfg.style.red;
    if (fix < 3 || (isNum(d.hdop) && d.hdop > 2)) return this.cfg.style.amber;
    return this.cfg.style.green;
  }

  _hdopColor(d) {
    if (!isNum(d.hdop)) return this.cfg.style.dim;
    if (d.hdop > 5) return this.cfg.style.red;
    if (d.hdop > 2) return this.cfg.style.amber;
    return this.cfg.style.dim;
  }

  _barsFor(dbm) {
    if (!isNum(dbm)) return 0;
    if (dbm >= -55) return 4;
    if (dbm >= -67) return 3;
    if (dbm >= -78) return 2;
    return 1;
  }

  /* ------------------------------------------------------------------ *
   * 4.3 Heading tape
   * ------------------------------------------------------------------ */
  _drawHeading(d, live) {
    const { W, H } = this;
    const cfg = this.cfg.heading;
    const m = this.m;
    const s = H / 1080;
    const yLabel = H * cfg.labelY;
    const yTick = H * cfg.tickY;
    const yValue = H * cfg.valueY;

    // Cardinal letters get their own row, clear of the numbers. This is one
    // of the reference's problem spots (NW colliding with 310).
    const yCard = yLabel - this._sizePx("s") * 1.55;

    const hdg = live ? this.s.heading : 0;
    // Total visible width of the tape, in degrees.
    const span = 60;
    const pxPerDeg = (m.headingX1 - m.headingX0) / (span * 2);

    this.ctx.save();
    // Tell the overlap check where the clip is, so a label the clip removes
    // cannot reserve space (same reasoning as the ladder).
    const cardSize = this._sizePx("s");
    this._claimClip = {
      x0: m.headingX0 - 4,
      y0: yCard - cardSize,
      x1: m.headingX1 + 4,
      y1: yValue + this._sizePx("m") * 1.2,
    };
    this.ctx.beginPath();
    this.ctx.rect(this._claimClip.x0, this._claimClip.y0,
                  this._claimClip.x1 - this._claimClip.x0,
                  this._claimClip.y1 - this._claimClip.y0);
    this.ctx.clip();

    // baseline
    this._line(m.headingX0, yTick, m.headingX1, yTick, this.cfg.style.dim, m.lineW);

    const base = Math.floor(hdg / cfg.tickEvery) * cfg.tickEvery;

    for (let deg = base - span; deg <= base + span; deg += cfg.tickEvery) {
      const x = W / 2 + (deg - hdg) * pxPerDeg;
      if (x < m.headingX0 - 20 || x > m.headingX1 + 20) continue;

      const major = deg % cfg.labelEvery === 0;
      const tickH = (major ? 8 : 5) * s;
      this._line(x, yTick, x, yTick - tickH,
                 major ? this.cfg.style.fg : this.cfg.style.dim, m.lineW);

      if (!major) continue;

      // No telemetry: without this the tape anchors itself on north and looks
      // like a real heading. The ticks stay; the numbers do not.
      if (!live) continue;

      const wrapped = ((deg % 360) + 360) % 360;
      if (wrapped === 0) {
        // N is drawn on the cardinal row at this position, so the number row
        // stays empty here rather than printing 000 under it.
        continue;
      }

      // Labels: unwrapped (370, 380) or wrapped (010, 020). In unwrapped mode
      // degrees below 0 continue the scale as 350, 340 rather than -10.
      const shown = cfg.unwrapped ? (deg < 0 ? deg + 360 : deg) : wrapped;
      this.text(String(shown).padStart(3, "0"), x, yLabel,
                { size: "s", align: "center",
                  color: this.cfg.style.dim, prio: 1 });
    }

    // Cardinal letters, on their own row above the numbers.
    const cardinals = [["N", 0], ["NE", 45], ["E", 90], ["SE", 135],
                       ["S", 180], ["SW", 225], ["W", 270], ["NW", 315]];
    if (live) {
      for (const [letter, degBase] of cardinals) {
        // pick the equivalent of degBase nearest the current heading
        const delta = ((degBase - hdg + 540) % 360) - 180;
        if (Math.abs(delta) > span) continue;
        const x = W / 2 + delta * pxPerDeg;
        this.text(letter, x, yCard,
                  { size: "s", align: "center", weight: 700,
                    color: this.cfg.style.fg, prio: 2 });
      }
    }

    this.ctx.restore();
    this._claimClip = null;

    // Fixed centre caret, pointing up at the tape.
    const cx = W / 2;
    this.ctx.beginPath();
    this.ctx.moveTo(cx, yTick + 2 * s);
    this.ctx.lineTo(cx - 6 * s, yTick + 11 * s);
    this.ctx.lineTo(cx + 6 * s, yTick + 11 * s);
    this.ctx.closePath();
    this.ctx.fillStyle = this.cfg.style.fg;
    this.ctx.fill();

    // Current heading below the tape.
    // Round BEFORE wrapping: at 359.7 the display must read 000, not 360.
    const rounded = live ? ((Math.round(hdg) % 360) + 360) % 360 : null;
    this.text(rounded === null ? "---" : String(rounded).padStart(3, "0"),
              cx, yValue,
              { size: "m", align: "center", weight: 700, prio: 3 });

    // No home marker on this tape. The bearing to home is already a number
    // under the speed tape and a relative arrow in the compass group; a third
    // "H" on the cardinal row only crowded the scale.
  }

  /* ------------------------------------------------------------------ *
   * 4.4 Split pitch ladder
   * ------------------------------------------------------------------ */
  _drawLadder(live) {
    const { W, H } = this;
    const cfg = this.cfg.ladder;
    const m = this.m;
    const s = H / 1080;
    const ctx = this.ctx;

    const pitch = live ? this.s.pitch : 0;
    const roll = live ? this.s.roll : 0;

    // Rung numbers sit at the outer end of each half, outside the rung. The
    // widest of them ("-90") decides how much room the label needs, so the
    // rung length is computed to leave exactly that and never be clipped.
    const labelSize = this._sizePx("s");
    const labelW = this._textWidth("-90", "s", 400);
    const windowHalf = (m.ladderX1 - m.ladderX0) / 2;
    const gap = m.gapPx / 2;
    const labelPad = 4 * s;
    const halfLen = Math.max(
      10 * s,
      Math.min(m.halfPx, windowHalf - gap - labelW - labelPad - 6 * s)
    );

    ctx.save();
    // Labels outside this window are clipped away, so tell _claimScreen about
    // it: a clipped label must not block a visible one elsewhere.
    this._claimClip = { x0: m.ladderX0, y0: m.ladderY0,
                        x1: m.ladderX1, y1: m.ladderY1 };
    ctx.beginPath();
    ctx.rect(m.ladderX0, m.ladderY0,
             m.ladderX1 - m.ladderX0, m.ladderY1 - m.ladderY0);
    ctx.clip();

    // Faint wash behind the rungs. Drawn after the clip but before the rotate,
    // so it stays an axis-aligned rectangle filling the ladder window rather
    // than swinging around with the aircraft.
    if (cfg.wash) {
      ctx.fillStyle = cfg.wash;
      ctx.fillRect(m.ladderX0, m.ladderY0,
                   m.ladderX1 - m.ladderX0, m.ladderY1 - m.ladderY0);
    }

    ctx.translate(m.cx, m.cy);
    // Right bank rotates the ladder counter-clockwise.
    ctx.rotate(-roll * Math.PI / 180);

    for (let deg = -90; deg <= 90; deg += cfg.stepDeg) {
      // Nose-up pitch moves the ladder down.
      const y = (pitch - deg) * m.ladderPxDeg;
      if (Math.abs(y) > H * 0.42) continue;

      const isZero = deg === 0;
      const negative = deg < 0;
      const color = isZero ? this.cfg.style.fg : this.cfg.style.dim;

      // A gap in the middle: each half runs from `gap` out to `gap + halfLen`.
      for (const side of [-1, 1]) {
        const inner = side * gap;
        const outer = side * (gap + halfLen);

        if (isZero) {
          // The 0 rung is the only solid full-width reference we draw.
          this._line(inner, y, outer, y, this.cfg.style.fg, m.lineW * 1.6);
        } else if (negative) {
          // Negative rungs: short dashed line toward the centre.
          this._line(inner, y, outer, y, color, m.lineW, [5 * s, 5 * s]);
        } else {
          // Positive rungs: a short solid line.
          this._line(inner, y, outer, y, color, m.lineW);
        }

        // Rung number at the OUTER end, with its sign, as in the reference
        // (10, 5, 0, -5, -10). Without telemetry only the 0 line is drawn;
        // numbers would imply an attitude we do not have.
        if (live) {
          this.text(String(deg), outer + side * labelPad, y,
                    { size: "s", weight: isZero ? 700 : 400,
                      align: side < 0 ? "right" : "left", color, prio: 1 });
        }
      }
    }

    ctx.restore();
    this._claimClip = null;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(m.cx, m.cy + 5 * s);
    ctx.lineTo(m.cx - 7 * s, m.cy + 13 * s);
    ctx.lineTo(m.cx + 7 * s, m.cy + 13 * s);
    ctx.closePath();
    ctx.fillStyle = this.cfg.style.fg;
    ctx.fill();

    this._line(m.cx - 15 * s, m.cy - 4 * s, m.cx + 15 * s, m.cy - 4 * s,
               this.cfg.style.fg, m.lineW);
    ctx.restore();
  }

  /* ------------------------------------------------------------------ *
   * 4.5 Speed tape
   * ------------------------------------------------------------------ */
  _drawSpeedTape(d, live) {
    const { H } = this;
    const cfg = this.cfg.speed;
    const m = this.m;
    const s = H / 1080;
    const unit = UNITS[this.units].speed;

    const raw = cfg.source === "airspeed" ? d.airspeed : d.groundspeed;
    // Ticks and box share one interpolated value, or the box lags its ticks.
    const shown = live && isNum(raw) ? this.s.speed * unit.f : null;
    const value = shown === null ? 0 : shown;

    // Faint wash over the tape strip, under the tape line and its numbers.
    // It runs out past the value box and dissolves into the margin.
    if (cfg.wash) {
      this._tapeWash(m.speedX + 10 * s, this.W * cfg.boxX0,
                     this.W * cfg.fade, cfg.wash);
    }

    // The tape line and its end caps.
    this._line(m.speedX, m.speedTop, m.speedX, m.speedBottom,
               this.cfg.style.fg, m.lineW);
    const cap = 9 * s;
    this._line(m.speedX - cap, m.speedTop, m.speedX + cap, m.speedTop,
               this.cfg.style.fg, m.lineW);
    this._line(m.speedX - cap, m.speedBottom, m.speedX + cap, m.speedBottom,
               this.cfg.style.fg, m.lineW);

    const centerY = m.cy;
    const halfSpan = (m.speedBottom - m.speedTop) / 2 / m.speedPxUnit;

    // Reserve the value box BEFORE the tick labels, so numbers that would fall
    // inside the outline are dropped instead of printing over the readout.
    const boxX0 = this.W * cfg.boxX0;
    const boxX1 = this.W * cfg.boxX1;
    const boxH = this._sizePx("l") * 1.75;
    this._reserveZone(boxX0, centerY - boxH / 2, boxX1 - boxX0, boxH, 2);

    // Ticks point left, labels to the left of them.
    const base = Math.floor(value / cfg.tickEvery) * cfg.tickEvery;
    for (let v = base - halfSpan; v <= base + halfSpan + cfg.tickEvery;
         v += cfg.tickEvery) {
      if (v < 0) continue;
      const y = centerY - (v - value) * m.speedPxUnit;
      if (y < m.speedTop - 6 || y > m.speedBottom + 6) continue;
      const major = Math.round(v) % cfg.labelEvery === 0;
      const len = (major ? 11 : 6) * s;
      this._line(m.speedX, y, m.speedX - len, y,
                 major ? this.cfg.style.fg : this.cfg.style.dim, m.lineW);
      // No telemetry: ticks only, no numbers to imply a reading.
      if (major && live) {
        this.text(String(Math.round(v)), m.speedX - len - 6 * s, y,
                  { size: "s", align: "right",
                    color: this.cfg.style.dim, prio: 1 });
      }
    }

    // ---- boxed value: outline only, number and unit on one baseline ----
    this._fadeBox(boxX0, boxX1, centerY - boxH / 2, centerY + boxH / 2,
                  -1, this.W * cfg.fade);

    // pointer touching the line
    this.ctx.beginPath();
    this.ctx.moveTo(m.speedX, centerY);
    this.ctx.lineTo(m.speedX - 10 * s, centerY - 5 * s);
    this.ctx.lineTo(m.speedX - 10 * s, centerY + 5 * s);
    this.ctx.closePath();
    this.ctx.fillStyle = this.cfg.style.fg;
    this.ctx.fill();

    // label on the left, value + unit right-aligned inside the box
    this.text(cfg.source === "airspeed" ? "SPD(A)" : "SPD(G)",
              boxX0 + 7 * s, centerY,
              { size: "s", color: this.cfg.style.dim, prio: 2 });
    this._valueUnit(shown === null ? "---" : shown.toFixed(unit.places),
                    unit.label, boxX1 - 7 * s, centerY,
                    { size: "l", weight: 700, prio: 3 });
  }

  /* ------------------------------------------------------------------ *
   * 4.6 Altitude tape (mirror of speed)
   * ------------------------------------------------------------------ */
  _drawAltTape(d, live) {
    const { H } = this;
    const cfg = this.cfg.alt;
    const m = this.m;
    const s = H / 1080;
    const unit = UNITS[this.units].alt;

    const raw = this.altSource === "alt_msl" ? d.alt_msl : d.alt_rel;
    const shown = live && isNum(raw) ? this.s.alt * unit.f : null;
    const value = shown === null ? 0 : shown;

    // Faint wash over the tape strip, under the tape line and its numbers.
    // It runs out past the value box and dissolves into the margin.
    if (cfg.wash) {
      this._tapeWash(m.altX - 10 * s, this.W * cfg.boxX1,
                     this.W * cfg.fade, cfg.wash);
    }

    this._line(m.altX, m.speedTop, m.altX, m.speedBottom,
               this.cfg.style.fg, m.lineW);
    const cap = 9 * s;
    this._line(m.altX - cap, m.speedTop, m.altX + cap, m.speedTop,
               this.cfg.style.fg, m.lineW);
    this._line(m.altX - cap, m.speedBottom, m.altX + cap, m.speedBottom,
               this.cfg.style.fg, m.lineW);

    const centerY = m.cy;
    const halfSpan = (m.speedBottom - m.speedTop) / 2 / m.altPxUnit;

    // Reserve the value box before the tick labels (see the speed tape).
    const boxX0 = this.W * cfg.boxX0;
    const boxX1 = this.W * cfg.boxX1;
    const boxH = this._sizePx("l") * 1.75;
    this._reserveZone(boxX0, centerY - boxH / 2, boxX1 - boxX0, boxH, 2);

    const base = Math.floor(value / cfg.tickEvery) * cfg.tickEvery;
    for (let v = base - halfSpan; v <= base + halfSpan + cfg.tickEvery;
         v += cfg.tickEvery) {
      const y = centerY - (v - value) * m.altPxUnit;
      if (y < m.speedTop - 6 || y > m.speedBottom + 6) continue;
      const major = Math.round(v) % cfg.labelEvery === 0;
      const len = (major ? 11 : 6) * s;
      this._line(m.altX, y, m.altX + len, y,
                 major ? this.cfg.style.fg : this.cfg.style.dim, m.lineW);
      if (major && live) {
        this.text(String(Math.round(v)), m.altX + len + 6 * s, y,
                  { size: "s", color: this.cfg.style.dim, prio: 1 });
      }
    }

    // ---- boxed value: outline only, mirrored ----
    this._fadeBox(boxX0, boxX1, centerY - boxH / 2, centerY + boxH / 2,
                  1, this.W * cfg.fade);

    this.ctx.beginPath();
    this.ctx.moveTo(m.altX, centerY);
    this.ctx.lineTo(m.altX + 10 * s, centerY - 5 * s);
    this.ctx.lineTo(m.altX + 10 * s, centerY + 5 * s);
    this.ctx.closePath();
    this.ctx.fillStyle = this.cfg.style.fg;
    this.ctx.fill();

    this._valueUnit(shown === null ? "---" : shown.toFixed(unit.places),
                    unit.label, boxX0 + 7 * s, centerY,
                    { size: "l", weight: 700, align: "left", prio: 3 });

    this.text(this.altSource === "alt_msl" ? "MSL" : "ALT",
              boxX1 - 7 * s, centerY,
              { size: "s", align: "right",
                color: this.cfg.style.dim, prio: 2 });
  }

  /* ------------------------------------------------------------------ *
   * 4.7 Text under the speed tape
   *
   * One value only: the bearing to home. Speed is the tape's value box and
   * distance is the bottom strip, so repeating either here would just be a
   * second copy of a number already on screen.
   * ------------------------------------------------------------------ */
  _drawUnderSpeed(d, live) {
    const { W, H } = this;
    const cfg = this.cfg.underSpeed;
    const s = H / 1080;
    const x = W * cfg.x;
    const y = H * cfg.y0;

    // Labelled, because a bare number under the speed tape reads like another
    // speed or a heading. It is neither: it is the bearing to home.
    const bearing = live && isNum(d.home_bearing) ? Math.round(d.home_bearing) : null;
    const txt = bearing === null ? "---°" : `${String(bearing).padStart(3, "0")}°`;
    this.text(txt, x, y, { size: "s", align: "right", prio: 2 });
    this.text("HOME", x - this._textWidth(txt, "s") - 8 * s, y,
              { size: "s", align: "right",
                color: this.cfg.style.dim, prio: 1 });
  }

  /* ------------------------------------------------------------------ *
   * 4.8 Bottom strip
   *
   * Every field advances by its measured width, so a value that grows
   * ("9.9 km") pushes its neighbours instead of overprinting them.
   *
   * Row 2 has two measured groups: the distance/height pair flowing from the
   * left margin, and the battery/throttle/pitch cluster anchored to the right
   * margin. The compass group is then centred in whatever span is left
   * between them. It used to follow the left-hand fields, which crushed it
   * against the margin and left a wide empty gap in the middle of the frame.
   * ------------------------------------------------------------------ */
  _drawBottomStrip(d, live) {
    const { W, H } = this;
    const s = H / 1080;
    const unit = UNITS[this.units];
    const x0 = this.m.left;
    const x1 = this.m.right;

    // ---- row 1: small ----
    // Row 2 is anchored from the icon radius rather than from a fixed
    // percentage: the compass nose index reaches ~1.28 r above the baseline,
    // so an enlarged icon would otherwise cross the bottom margin. Row 1 then
    // sits above that, with a measured gap.
    const r2 = this.cfg.bottom.iconRadius * H;
    const iconTop = r2 * 1.28;
    const y2 = this.m.bottom -
               Math.max(iconTop, this._sizePx("m") * 0.62);
    const y1 = y2 - iconTop - this._sizePx("s") * 1.15;
    let x = x0;

    // Row 1 carries only what appears nowhere else. Groundspeed is the speed
    // tape's value box, so it is not repeated here.
    const vs = live && isNum(d.climb) ? d.climb * unit.vs.f : null;
    x += this._bottomField(x, y1,
                           vs === null ? "---"
                             : (vs >= 0 ? "+" : "") + vs.toFixed(unit.vs.places),
                           unit.vs.label, s, "s", 1);

    const tot = live && isNum(d.total_dist_m) ? d.total_dist_m : null;
    this.text(`Total ${tot === null ? "---" : fmtDistance(tot, this.units)}`,
              x, y1, { size: "s", color: this.cfg.style.dim, prio: 1 });

    // ---- row 2: primary numbers ----
    // y2 is set with row 1 above, so both rows clear the bottom margin.

    // --- left group: range and bearing to home, flowing from the margin ---
    x = x0;
    this.text(`D ${live ? fmtDistance(d.home_dist, this.units) : "---"}`,
              x, y2, { size: "m", weight: 600, prio: 2 });
    x += this._textWidth("D 00000 m", "m", 600);
    const leftEnd = x;

    // --- right cluster, right-to-left by measured width (drawn first so the
    // compass group can be centred in the span that is genuinely free) ---
    let rx = x1;
    const sp = 20 * s;

    const mah = live && isNum(d.mah_used) ? String(Math.round(d.mah_used)) : "---";
    rx -= this._valueUnit(mah, "mAh", rx, y2,
                          { size: "m", weight: 600, prio: 2 });
    rx -= sp;

    const cells = this.cfg.battery.cells;
    const cellV = isNum(d.batt_cell_v)
      ? d.batt_cell_v
      : (isNum(d.batt_v) && cells ? d.batt_v / cells : null);
    const cellColor = !isNum(cellV) ? this.cfg.style.fg
      : cellV < this.cfg.battery.dangerV ? this.cfg.style.red
      : cellV < this.cfg.battery.warnV ? this.cfg.style.amber
      : this.cfg.style.fg;

    const vTxt = live && isNum(d.batt_v) ? d.batt_v.toFixed(1) : "---";
    rx -= this._valueUnit(vTxt, "V", rx, y2,
                          { size: "m", weight: 700, color: cellColor,
                            unitColor: cellColor, prio: 3 });
    // Per-cell voltage, on the same baseline. It is a separate small field to
    // the LEFT of the pack voltage (a suffix collided with the next column).
    if (isNum(cellV)) {
      const pc = `${cellV.toFixed(2)}/c`;
      rx -= this._textWidth(pc, "s") + 8 * s;
      this.text(pc, rx, y2,
                { size: "s", align: "left", color: cellColor, prio: 1 });
    }
    rx -= sp;

    const amps = live && isNum(d.batt_a) ? d.batt_a.toFixed(1) : "---";
    rx -= this._valueUnit(amps, "A", rx, y2,
                          { size: "m", weight: 600, prio: 2 });
    rx -= sp;

    const thr = live && isNum(d.throttle) ? String(d.throttle) : "---";
    rx -= this._valueUnit(thr, "%", rx, y2,
                          { size: "m", weight: 600, prio: 2 });
    this._propIcon(rx - 12 * s, y2, 8 * s);
    rx -= 26 * s;

    const pitchTxt = live && isNum(d.pitch)
      ? `${d.pitch >= 0 ? "+" : ""}${Math.round(d.pitch)}` : "---";
    rx -= this._valueUnit(pitchTxt, "°", rx, y2,
                          { size: "m", weight: 600, prio: 2 });
    const rightStart = rx - 6 * s - this._textWidth("PIT", "s", 400);
    this.text("PIT", rx - 6 * s, y2,
              { size: "s", align: "right",
                color: this.cfg.style.dim, prio: 1 });

    // --- compass group: centred in the free span between the two clusters ---
    this._drawCompassGroup(d, live, leftEnd, rightStart, y2, r2);
  }

  /**
   * Compass rose and home pointer, centred between `leftEnd` (end of the
   * range field) and `rightStart` (start of the battery cluster).
   *
   * It carries only what appears nowhere else: the card itself (heading as a
   * direction) and the pointer to home. The heading NUMBER is on the heading
   * tape, so printing "340°" here would be the same reading twice.
   */
  _drawCompassGroup(d, live, leftEnd, rightStart, y, r) {
    const { W } = this;
    const iconW = r * 2;              // icon box, diameter
    const gapGroup = r * 2.0;         // between the two icons
    const groupW = iconW + gapGroup + iconW;

    const minGap = r * 2.2;
    let gx = W / 2 - groupW / 2;      // frame-centred by default
    const lo = leftEnd + minGap;
    const hi = rightStart - minGap - groupW;
    if (hi > lo) gx = Math.min(Math.max(gx, lo), hi);

    // rose: heading as a direction
    this._compassIcon(gx + r, y, r, live ? this.s.heading : null);

    // home pointer, rotated by the bearing RELATIVE to the nose
    this._homeIcon(gx + iconW + gapGroup + r, y, r,
                   live && d.home_set && isNum(d.home_bearing)
                     ? d.home_bearing - this.s.heading : null);
  }

  /**
   * One numeric field (value + unit on one baseline), returning its width.
   * Uses _valueUnit so the pair reserves a single box.
   */
  _bottomField(x, y, value, unit, s, size, prio) {
    this._valueUnit(value, unit, x, y,
                    { size, weight: 400, align: "left", prio });
    const gap = this._sizePx("s") * 0.55;
    return this._textWidth(value, size, 400) + gap +
           this._textWidth(unit, "s") + 20 * s;
  }

  /* ------------------------------------------------------------------ *
   * 4.9 Panels
   *
   * Label left, number right, one measured column gap. No background; an
   * optional thin outline frame only.
   * ------------------------------------------------------------------ */
  _panelFrame(box) {
    this._box(this.W * box.x0, this.H * box.y0,
              this.W * (box.x1 - box.x0), this.H * (box.y1 - box.y0),
              this.cfg.style.faint);
  }

  /** Left label, right value, on one row. */
  _panelRow(x0, x1, y, label, value, opts = {}) {
    this.text(label, x0, y,
              { size: "s", color: this.cfg.style.dim, prio: 1 });
    this.text(value, x1, y,
              { size: "s", align: "right",
                color: opts.color || this.cfg.style.fg, prio: 1 });
  }

  _drawRadioPanel(d, live) {
    const box = this.cfg.panels.radio;
    const { W, H } = this;
    const x0 = W * box.x0;
    const x1 = W * box.x1;
    let y = H * box.y0 + this._sizePx("s");
    const lh = this._sizePx("s") * 1.7;
    const wifi = d.wifi || {};

    this.text("Radio Link", x0, y,
              { size: "s", weight: 700, prio: 2 });
    this.text(isNum(wifi.quality_pct) ? `${Math.round(wifi.quality_pct)}%` : "---%",
              x1, y, { size: "s", align: "right", prio: 2 });
    y += lh;

    this._panelRow(x0, x1, y, "Link delay",
                   this.linkRttMs === null ? "--- ms"
                     : `${Math.round(this.linkRttMs)} ms`);
    y += lh * 0.9;

    // MAVLink telemetry throughput. This is NOT the video bitrate, which the
    // top strip and the decoder panel already show.
    this._panelRow(x0, x1, y, "Data",
                   `${isNum(d.data_kbps) ? d.data_kbps.toFixed(1) : "---"} kbps`);
    y += lh * 0.9;

    // wlan0 line: link rate only. Signal, quality and frequency are all on
    // the top strip, so repeating them here would be the same reading twice.
    const iface = wifi.iface || "wlan0";
    this.text(iface, x0, y, { size: "s", color: this.cfg.style.dim, prio: 1 });
    if (isNum(wifi.rate_mbps)) {
      this._valueUnit(wifi.rate_mbps.toFixed(1), "Mbps", x1, y,
                      { size: "s", prio: 1 });
    }

    // sparkline of signal strength (the history is not shown anywhere else)
    const sparkY0 = y + lh * 1.1;
    const sparkH = this._sizePx("s") * 2.6;
    this._sparkline(x0, sparkY0, x1 - x0, sparkH, this.cfg.style.green);
  }

  _drawEfficiencyPanel(d, live) {
    const box = this.cfg.panels.efficiency;
    const { W, H } = this;
    const x0 = W * box.x0;
    const x1 = W * box.x1;
    let y = H * box.y0 + this._sizePx("s");
    const lh = this._sizePx("s") * 1.7;

    // Hide entirely when there is no current sensor: the spec asks for this,
    // and mA/km without current is meaningless.
    if (!isNum(d.mah_used) || d.mah_used <= 0) return;

    this.text("Efficiency Stats", x0, y,
              { size: "s", weight: 700, prio: 2 });
    y += lh;

    this._panelRow(x0, x1, y, "mA/km",
                   isNum(d.mah_per_km) ? String(Math.round(d.mah_per_km)) : "---");
    y += lh * 0.9;

    this._panelRow(x0, x1, y, "mA/h",
                   isNum(d.mah_per_hour) ? String(Math.round(d.mah_per_hour)) : "---");
  }

  _drawDecoderPanel(d, live) {
    const box = this.cfg.panels.decoder;
    const { W, H } = this;
    const s = H / 1080;
    const st = this.stats;
    const c = this.cfg.style;

    this._panelFrame(box);

    const x0 = W * box.x0 + 12 * s;
    const x1 = W * box.x1 - 12 * s;
    let y = H * box.y0 + this._sizePx("s") * 1.4;
    const lh = this._sizePx("s") * 1.6;

    const w = st && st.width ? st.width : (d.video && d.video.width);
    const h = st && st.height ? st.height : (d.video && d.video.height);

    // Header carries the resolution. Bitrate and frame rate are NOT repeated
    // here: both are in the top strip, which is always on screen, so printing
    // them again made the same number appear twice.
    this.text("Video Decoder", x0, y,
              { size: "s", weight: 700, prio: 2 });
    this.text(w && h ? `${w}x${h}` : "n/a", x1, y,
              { size: "s", align: "right", color: c.dim, prio: 2 });
    y += lh;

    this._panelRow(x0, x1, y, "Params",
                   d.video && d.video.preset ? `x264 ${d.video.preset}` : "n/a");
    y += lh * 0.9;

    const buf = st && isNum(st.jitterMs) ? st.jitterMs : null;
    this._panelRow(x0, x1, y, "Buffer",
                   buf === null ? "n/a" : `${(buf / 1000).toFixed(2)} s`);
    y += lh * 0.9;

    // ---- histogram ----
    const histH = this._sizePx("s") * 3.2;
    this._histogram(x0, y, x1 - x0, histH);
    this.text("30 ms/bar", x1, y + histH + this._sizePx("s") * 0.9,
              { size: "s", align: "right", color: c.dim, prio: 1 });
    y += histH + lh * 1.4;

    const lost = st && isNum(st.lost) ? st.lost : null;
    const nack = st && isNum(st.nack) ? st.nack : null;
    const dropped = st && isNum(st.dropped) ? st.dropped : null;
    const pkts = st && isNum(st.packetsPerSec) ? st.packetsPerSec : null;
    const gapPkts = st && isNum(st.maxLostGapPkts) ? st.maxLostGapPkts : null;
    const gapMs = st && isNum(st.maxLostGapMs) ? st.maxLostGapMs : null;

    this._panelRow(x0, x1, y, "Max lost gap", gapPkts === null ? "n/a" : `${gapPkts} pkt`);
    y += lh * 0.86;
    this._panelRow(x0, x1, y, "Gap time", gapMs === null ? "n/a" : `${Math.round(gapMs)} ms`);
    y += lh * 0.86;
    this._panelRow(x0, x1, y, "Packets/sec", pkts === null ? "n/a" : String(Math.round(pkts)));
    y += lh * 0.86;
    // No "Frames/sec" row: the top strip already shows the decoded frame rate
    // from the same WebRTC stats object.
    this._panelRow(x0, x1, y, "NACK", nack === null ? "n/a" : String(nack));
    y += lh * 0.86;
    this._panelRow(x0, x1, y, "Dropped", dropped === null ? "n/a" : String(dropped));
    y += lh * 0.86;
    this._panelRow(x0, x1, y, "Lost", lost === null ? "n/a" : String(lost));
    // The wlan0 signal row is gone: it is already in the top strip.
  }

  _histogram(x, y, w, h) {
    const ctx = this.ctx;
    const s = this.H / 1080;
    const n = this._buckets.length;
    const bw = w / n;

    const maxV = 4;
    this._line(x, y + h, x + w, y + h, this.cfg.style.faint, 1, [3 * s, 3 * s]);
    this._line(x, y + h / 2, x + w, y + h / 2, "rgba(255,255,255,0.10)", 1,
               [2 * s, 4 * s]);

    ctx.fillStyle = this.cfg.style.green;
    for (let i = 0; i < n; i++) {
      const v = Math.min(maxV, this._buckets[(this._bucketIdx + i) % n]);
      if (v <= 0) continue;
      const bh = (v / maxV) * h;
      ctx.fillRect(x + i * bw + 0.5, y + h - bh, Math.max(1, bw - 1), bh);
    }
  }

  /** Track frames received in 30 ms buckets for the decoder histogram. */
  tickStats(dtMs) {
    const st = this.stats;
    if (!st || !isNum(st.frames)) {
      this._lastFrames = null;
      return;
    }
    if (this._lastFrames === null) {
      this._lastFrames = st.frames;
      return;
    }
    const df = Math.max(0, st.frames - this._lastFrames);
    this._lastFrames = st.frames;

    const buckets = Math.min(20, Math.max(1, Math.round(dtMs / 30)));
    for (let i = 0; i < buckets; i++) {
      this._bucketIdx = (this._bucketIdx + 1) % this._buckets.length;
      this._buckets[this._bucketIdx] = df / buckets;
    }
  }

  /* ------------------------------------------------------------------ *
   * Icons (strokes only - no filled shapes except the tiny marks)
   * ------------------------------------------------------------------ */
  _satIcon(cx, cy, r, color) {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1, r * 0.16);

    ctx.beginPath();
    ctx.rect(cx - r * 0.32, cy - r * 0.32, r * 0.64, r * 0.64);
    ctx.stroke();

    for (let i = 1; i <= 2; i++) {
      ctx.beginPath();
      ctx.arc(cx + r * 0.5, cy - r * 0.5, r * (0.5 + i * 0.45),
              -Math.PI / 2, 0);
      ctx.globalAlpha = 0.75 / i;
      ctx.stroke();
    }
    ctx.restore();
  }

  _signalBars(x, cy, h, bars) {
    const ctx = this.ctx;
    const n = 4;
    const bw = h / 6;
    const gap = bw * 0.55;
    const totalW = n * bw + (n - 1) * gap;
    const x0 = x - totalW / 2;
    for (let i = 0; i < n; i++) {
      const bh = h * (0.3 + 0.233 * i);
      const on = i < bars;
      ctx.fillStyle = on
        ? (bars <= 1 ? this.cfg.style.red : this.cfg.style.fg)
        : "rgba(255,255,255,0.20)";
      ctx.fillRect(x0 + i * (bw + gap), cy + h / 2 - bh, bw, bh);
    }
  }

  _cpuIcon(cx, cy, r) {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = this.cfg.style.dim;
    ctx.lineWidth = Math.max(1, r * 0.16);
    ctx.strokeRect(cx - r * 0.6, cy - r * 0.6, r * 1.2, r * 1.2);
    ctx.strokeRect(cx - r * 0.28, cy - r * 0.28, r * 0.56, r * 0.56);
    ctx.restore();
  }

  _warnIcon(cx, cy, r) {
    const ctx = this.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(cx, cy - r);
    ctx.lineTo(cx + r, cy + r * 0.8);
    ctx.lineTo(cx - r, cy + r * 0.8);
    ctx.closePath();
    ctx.strokeStyle = this.cfg.style.red;
    ctx.lineWidth = Math.max(1, r * 0.18);
    ctx.stroke();
    this.text("!", cx, cy + r * 0.16,
              { size: "s", align: "center", weight: 700,
                color: this.cfg.style.red, prio: 1 });
    ctx.restore();
  }

  _stopwatch(cx, cy, r) {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = this.cfg.style.dim;
    ctx.lineWidth = Math.max(1, r * 0.18);
    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.62, 0, TAU);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx, cy - r * 0.62);
    ctx.lineTo(cx, cy + r * 0.1);
    ctx.moveTo(cx, cy - r);
    ctx.lineTo(cx, cy - r * 0.62);
    ctx.stroke();
    ctx.restore();
  }

  /**
   * Compass rose. It is a north-referenced card: the needle swings opposite to
   * the heading, so north moves left when the aircraft turns right, and the
   * fixed index at the top is the aircraft's nose.
   *
   * Geometry is deliberately layered so nothing crosses anything else:
   *
   *   0.00 - 0.70 r   needle (north arm + south tail), always inside the card
   *   0.80 r          the card ring
   *   0.94 - 1.18 r   fixed nose index, sitting OUTSIDE the ring with a gap
   *
   * The old version put the index triangle's base at 0.7 r, i.e. inside the
   * ring, so the ring line cut straight through the marker and the top of the
   * dial looked skewed.
   */
  _compassIcon(cx, cy, r, headingDeg) {
    const ctx = this.ctx;
    const hdg = isNum(headingDeg) ? headingDeg : null;
    const lw = Math.max(1, r * 0.13);

    ctx.save();
    ctx.translate(cx, cy);
    ctx.strokeStyle = this.cfg.style.fg;

    // card ring
    ctx.lineWidth = lw;
    ctx.beginPath();
    ctx.arc(0, 0, r * 0.78, 0, TAU);
    ctx.stroke();

    // fixed nose index: a caret above the ring, pointing in at the card.
    // Its tip stops short of the ring and its base clears the row margin.
    ctx.lineWidth = lw;
    ctx.lineJoin = "miter";
    ctx.beginPath();
    ctx.moveTo(0, -r * 0.96);
    ctx.lineTo(-r * 0.27, -r * 1.24);
    ctx.lineTo(r * 0.27, -r * 1.24);
    ctx.closePath();
    ctx.stroke();

    // The needle only exists when there is a heading: pointing north on a
    // dead link would claim an attitude the aircraft has not reported.
    if (hdg !== null) {
      ctx.save();
      // North of the card appears at -heading relative to the nose.
      ctx.rotate(-hdg * Math.PI / 180);

      // North arm: a narrow triangle, wide enough to survive at the small
      // size the bottom row uses, tip held inside the ring.
      ctx.lineWidth = lw;
      ctx.beginPath();
      ctx.moveTo(0, -r * 0.64);
      ctx.lineTo(r * 0.22, -r * 0.02);
      ctx.lineTo(0, -r * 0.16);
      ctx.lineTo(-r * 0.22, -r * 0.02);
      ctx.closePath();
      ctx.stroke();

      // South tail: plain and shorter, so the two ends never read alike.
      ctx.lineWidth = Math.max(1, r * 0.11);
      ctx.beginPath();
      ctx.moveTo(0, r * 0.06);
      ctx.lineTo(0, r * 0.44);
      ctx.stroke();

      ctx.restore();
    }

    ctx.restore();
  }

  /** Home icon, rotated so it points toward home. */
  _homeIcon(cx, cy, r, bearingDeg) {
    const ctx = this.ctx;
    ctx.save();
    ctx.translate(cx, cy);
    if (isNum(bearingDeg)) ctx.rotate(bearingDeg * Math.PI / 180);
    ctx.strokeStyle = this.cfg.style.green;
    ctx.lineWidth = Math.max(1, r * 0.16);
    ctx.beginPath();
    ctx.moveTo(0, -r * 0.75);
    ctx.lineTo(r * 0.6, -r * 0.25);
    ctx.lineTo(r * 0.28, -r * 0.25);
    ctx.lineTo(r * 0.28, r * 0.6);
    ctx.lineTo(-r * 0.28, r * 0.6);
    ctx.lineTo(-r * 0.28, -r * 0.25);
    ctx.lineTo(-r * 0.6, -r * 0.25);
    ctx.closePath();
    ctx.stroke();
    ctx.restore();
  }

  _propIcon(cx, cy, r) {
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = this.cfg.style.dim;
    ctx.lineWidth = Math.max(1, r * 0.16);
    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.7, 0, TAU);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx - r * 0.5, cy - r * 0.5);
    ctx.lineTo(cx + r * 0.5, cy + r * 0.5);
    ctx.stroke();
    ctx.restore();
  }

  _sparkline(x, y, w, h, color) {
    const ctx = this.ctx;
    const hist = this._wifiHistory || [];
    this._line(x, y + h, x + w, y + h, this.cfg.style.faint, 1);
    if (hist.length < 2) return;

    // Map dBm -95..-30 onto the box.
    const lo = -95, hi = -30;
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = this.m.lineW * 1.3;
    for (let i = 0; i < hist.length; i++) {
      const px = x + (i / (hist.length - 1)) * w;
      const v = Math.max(lo, Math.min(hi, hist[i]));
      const py = y + h - ((v - lo) / (hi - lo)) * h;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
  }

  /** Keep the last 30 s of signal samples (one per second). */
  pushWifiSample(dbm) {
    if (!isNum(dbm)) return;
    if (!this._wifiHistory) this._wifiHistory = [];
    this._wifiHistory.push(dbm);
    if (this._wifiHistory.length > 30) this._wifiHistory.shift();
  }
}
