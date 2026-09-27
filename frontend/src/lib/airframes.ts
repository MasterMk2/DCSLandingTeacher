/**
 * Aircraft models for the 3D flight view: an outline per DCS airframe --
 * the planform, how many fins and which way they lean, where the wing sits,
 * rotors and propellers -- so a Hornet reads as a Hornet and a Huey as a
 * helicopter, and the attitude is read off the right shape.
 *
 * Built here from a few public figures (length, span, rotor diameter) rather
 * than shipped as mesh files: no asset licensing in a public repository and
 * nothing to download. They are outlines for reading attitude, not scale
 * models -- the details are drawn by eye to those figures.
 *
 * Each outline is drawn in metres in the aircraft's own drawing frame
 * (station x aft from the nose tip, y up from the fuselage datum, z to the
 * right) and handed out normalised: unit length, nose +x, top +y, right +z,
 * centred on its own length. `lengthM` says how long that unit really is,
 * so the view can draw the model to scale against a deck.
 *
 * Kept free of three.js, like flight3d.ts, so vitest's node environment can
 * check it.
 */

import type { Vec3 } from "./flight3d";

type Rgb = readonly [number, number, number];

// Vertex colours, multiplied by the material's: the ghosts are grey, the
// cursor orange. Glass stays dark so which way is up is never in doubt.
const BODY: Rgb = [1, 1, 1];
const GLASS: Rgb = [0.22, 0.26, 0.3];
const BLADE: Rgb = [0.5, 0.52, 0.55];

const DEG = Math.PI / 180;
const RING = 8;

/** A fuselage cross-section: station (m aft of the nose), half-width, and
 *  the heights of its top and bottom. */
type Section = readonly [x: number, halfWidth: number, top: number, bottom: number];

/** One end of a lifting surface: its leading edge and its chord (m). */
interface Edge {
  x: number;
  y: number;
  z: number;
  chord: number;
}

type Axis = "x" | "y" | "z";

/** Radial and chordwise unit vectors of a blade at angle `phi` round `axis`
 *  (0 = aft for a main rotor, up for a propeller). */
function bladeFrame(axis: Axis, phi: number): [Vec3, Vec3] {
  const c = Math.cos(phi);
  const s = Math.sin(phi);
  if (axis === "y") return [[c, 0, s], [-s, 0, c]];
  if (axis === "x") return [[0, c, s], [0, -s, c]];
  return [[c, s, 0], [-s, c, 0]];
}

function discFrame(axis: Axis): [Vec3, Vec3, Vec3] {
  if (axis === "y") return [[0, 1, 0], [1, 0, 0], [0, 0, 1]];
  if (axis === "x") return [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  return [[0, 0, 1], [1, 0, 0], [0, 1, 0]];
}

/** Triangles in the drawing frame. Faces are two-sided in the view, so
 *  winding does not matter. */
class Drawing {
  readonly positions: number[] = [];
  readonly colors: number[] = [];

  tri(a: Vec3, b: Vec3, c: Vec3, color: Rgb) {
    const ux = b[0] - a[0];
    const uy = b[1] - a[1];
    const uz = b[2] - a[2];
    const vx = c[0] - a[0];
    const vy = c[1] - a[1];
    const vz = c[2] - a[2];
    const nx = uy * vz - uz * vy;
    const ny = uz * vx - ux * vz;
    const nz = ux * vy - uy * vx;
    // Pointed noses and closed tips collapse whole rings onto one point.
    if (nx * nx + ny * ny + nz * nz < 1e-12) return;
    // Stations run aft; the model's x runs forward.
    for (const p of [a, b, c]) this.positions.push(-p[0], p[1], p[2]);
    for (let i = 0; i < 3; i++) this.colors.push(color[0], color[1], color[2]);
  }

  quad(a: Vec3, b: Vec3, c: Vec3, d: Vec3, color: Rgb) {
    this.tri(a, b, c, color);
    this.tri(a, c, d, color);
  }

  /** A body lofted through elliptical sections; closed at both ends.
   *  Off the centreline (`z`), it is drawn on both sides. */
  body(sections: readonly Section[], opts: { z?: number; color?: Rgb } = {}) {
    const color = opts.color ?? BODY;
    const z0 = opts.z ?? 0;
    for (const side of z0 !== 0 ? [1, -1] : [1]) {
      const rings = sections.map(([x, w, top, bottom]) => {
        const mid = (top + bottom) / 2;
        const h = (top - bottom) / 2;
        const ring: Vec3[] = [];
        for (let k = 0; k < RING; k++) {
          const t = (2 * Math.PI * k) / RING;
          ring.push([x, mid + h * Math.cos(t), z0 * side + w * Math.sin(t)]);
        }
        return ring;
      });
      for (let i = 0; i + 1 < rings.length; i++) {
        for (let k = 0; k < RING; k++) {
          const j = (k + 1) % RING;
          this.quad(rings[i][k], rings[i + 1][k], rings[i + 1][j], rings[i][j], color);
        }
      }
      for (const i of [0, sections.length - 1]) {
        const [x, , top, bottom] = sections[i];
        const centre: Vec3 = [x, (top + bottom) / 2, z0 * side];
        for (let k = 0; k < RING; k++) this.tri(centre, rings[i][k], rings[i][(k + 1) % RING], color);
      }
    }
  }

  canopy(sections: readonly Section[]) {
    this.body(sections, { color: GLASS });
  }

  /**
   * A wing, tailplane or fin between two edges, with a thin diamond
   * section (thickest at 35% chord) so it still shows edge-on. Drawn on
   * both sides unless it lies on the centreline.
   */
  surface(root: Edge, tip: Edge, opts: { thickness?: number; color?: Rgb } = {}) {
    const color = opts.color ?? BODY;
    const thickness = opts.thickness ?? 0.06;
    for (const side of root.z !== 0 || tip.z !== 0 ? [1, -1] : [1]) {
      const r = { ...root, z: root.z * side };
      const t = { ...tip, z: tip.z * side };
      // Across the plate: square to the span and to the chord (the x axis).
      const sy = t.y - r.y;
      const sz = t.z - r.z;
      const norm = Math.hypot(sy, sz) || 1;
      const ny = sz / norm;
      const nz = -sy / norm;
      const section = (e: Edge): Vec3[] => {
        const h = (e.chord * thickness) / 2;
        const ridge = e.x + e.chord * 0.35;
        return [
          [e.x, e.y, e.z],
          [ridge, e.y + ny * h, e.z + nz * h],
          [e.x + e.chord, e.y, e.z],
          [ridge, e.y - ny * h, e.z - nz * h],
        ];
      };
      const a = section(r);
      const b = section(t);
      for (let k = 0; k < 4; k++) {
        const j = (k + 1) % 4;
        this.quad(a[k], b[k], b[j], a[j], color);
      }
      this.quad(a[0], a[1], a[2], a[3], color);
      this.quad(b[0], b[1], b[2], b[3], color);
    }
  }

  /** A surface through several edges: cranked or curved planforms. */
  panels(edges: Edge[], opts: { thickness?: number } = {}) {
    for (let i = 0; i + 1 < edges.length; i++) this.surface(edges[i], edges[i + 1], opts);
  }

  /** A fin rising `height` from `root`, leaning `cantDeg` outboard. */
  fin(root: Edge, tip: { x: number; chord: number }, height: number, cantDeg = 0) {
    this.surface(root, {
      x: tip.x,
      chord: tip.chord,
      y: root.y + height * Math.cos(cantDeg * DEG),
      z: root.z + height * Math.sin(cantDeg * DEG),
    });
  }

  /** A flat cylinder: hubs, radomes, a fenestron. */
  disc(centre: Vec3, axis: Axis, radius: number, half: number, color: Rgb = BODY) {
    const [a, u, v] = discFrame(axis);
    const segments = 16;
    const at = (k: number, h: number): Vec3 => {
      const t = (2 * Math.PI * k) / segments;
      const c = Math.cos(t) * radius;
      const s = Math.sin(t) * radius;
      return [
        centre[0] + a[0] * h + u[0] * c + v[0] * s,
        centre[1] + a[1] * h + u[1] * c + v[1] * s,
        centre[2] + a[2] * h + u[2] * c + v[2] * s,
      ];
    };
    const top: Vec3 = [centre[0] + a[0] * half, centre[1] + a[1] * half, centre[2] + a[2] * half];
    const bottom: Vec3 = [centre[0] - a[0] * half, centre[1] - a[1] * half, centre[2] - a[2] * half];
    for (let k = 0; k < segments; k++) {
      this.tri(top, at(k, half), at(k + 1, half), color);
      this.tri(bottom, at(k + 1, -half), at(k, -half), color);
      this.quad(at(k, half), at(k, -half), at(k + 1, -half), at(k + 1, half), color);
    }
  }

  /**
   * A rotor or propeller: `blades` flat blades round `axis`. Odd counts put
   * one blade aft (over the tail boom), even ones sit at 45 deg, so a blade
   * pair never lies across the fuselage and reads as a wing.
   */
  rotor(
    hub: Vec3,
    axis: Axis,
    radius: number,
    blades: number,
    opts: { chord?: number; phaseDeg?: number; mirror?: boolean } = {},
  ) {
    const chord = opts.chord ?? radius * 0.06;
    const phase = opts.phaseDeg ?? (blades % 2 === 1 ? 0 : 45);
    for (const side of (opts.mirror ?? hub[2] !== 0) ? [1, -1] : [1]) {
      const c: Vec3 = [hub[0], hub[1], hub[2] * side];
      for (let i = 0; i < blades; i++) {
        const [d, n] = bladeFrame(axis, (phase + (360 * i) / blades) * DEG);
        const p = (r: number, w: number): Vec3 => [
          c[0] + d[0] * r + n[0] * w,
          c[1] + d[1] * r + n[1] * w,
          c[2] + d[2] * r + n[2] * w,
        ];
        const r0 = radius * 0.08;
        this.quad(p(r0, -chord / 2), p(radius, -chord * 0.4), p(radius, chord * 0.4), p(r0, chord / 2), BLADE);
      }
      this.disc(c, axis, Math.max(radius * 0.08, chord * 0.6), Math.max(radius * 0.02, 0.05), BLADE);
    }
  }
}

// ---------------------------------------------------------------------------
// Fixed-wing outlines. Each is drawn to the aircraft named in `base` and
// stretched (length and span separately) to the others that borrow it.
// ---------------------------------------------------------------------------

interface Options {
  canards?: boolean;
  /** Flanker's tail cone between the nozzles. */
  stinger?: boolean;
  finCantDeg?: number;
  twoSeat?: boolean;
  intake?: "ventral" | "side";
  tipTanks?: boolean;
  sweepDeg?: number;
  stabOnFin?: boolean;
  canardX?: number;
  scoop?: boolean;
  elliptic?: boolean;
  twin?: boolean;
  biplane?: boolean;
  blades?: number;
  radome?: boolean;
  highWing?: boolean;
  tTail?: boolean;
}

interface Family {
  /** The aircraft the drawing is made to, and its real length and span (m). */
  base: { label: string; lengthM: number; spanM: number };
  draw(d: Drawing, o: Options): void;
}

const FAMILIES = {
  generic: {
    base: { label: "", lengthM: 18, spanM: 11 },
    draw(d) {
      d.body([
        [0, 0, 0, 0],
        [2, 0.5, 0.5, -0.5],
        [6, 0.9, 0.8, -0.7],
        [14, 0.9, 0.7, -0.6],
        [18, 0.7, 0.5, -0.5],
      ]);
      d.canopy([
        [3.5, 0, 0.6, 0.6],
        [4.5, 0.45, 1.3, 0.6],
        [6.5, 0.5, 1.4, 0.7],
        [8.5, 0, 0.8, 0.8],
      ]);
      d.surface({ x: 7.5, y: 0, z: 0.8, chord: 5.5 }, { x: 11, y: 0, z: 5.5, chord: 1.8 });
      d.surface({ x: 14.8, y: 0, z: 0.6, chord: 2.8 }, { x: 16.6, y: 0, z: 3.0, chord: 1.2 });
      d.fin({ x: 13.5, y: 0.6, z: 0, chord: 4.0 }, { x: 16.4, chord: 1.4 }, 3.2);
    },
  },

  hornet: {
    base: { label: "F/A-18C", lengthM: 17.07, spanM: 12.31 },
    draw(d) {
      d.body([
        [0, 0, -0.05, -0.05],
        [1.2, 0.3, 0.25, -0.3],
        [3.0, 0.5, 0.55, -0.5],
        [5.0, 0.62, 0.7, -0.6],
        [7.0, 1.0, 0.6, -0.75],
        [10.5, 1.1, 0.55, -0.7],
        [14.0, 1.0, 0.45, -0.55],
        [16.6, 0.85, 0.35, -0.4],
        [17.07, 0.8, 0.3, -0.35],
      ]);
      d.canopy([
        [2.6, 0, 0.5, 0.5],
        [3.4, 0.34, 0.95, 0.5],
        [4.8, 0.4, 1.05, 0.55],
        [6.2, 0.2, 0.8, 0.6],
        [6.8, 0, 0.66, 0.66],
      ]);
      // Leading-edge extensions, running forward to the windscreen.
      d.surface({ x: 2.8, y: 0.25, z: 0.5, chord: 5.0 }, { x: 7.4, y: 0.15, z: 1.9, chord: 1.0 }, { thickness: 0.03 });
      d.surface({ x: 7.35, y: 0.05, z: 0.9, chord: 5.1 }, { x: 10.0, y: 0.2, z: 6.155, chord: 1.7 });
      d.surface({ x: 13.9, y: 0, z: 0.8, chord: 3.0 }, { x: 16.0, y: -0.1, z: 3.29, chord: 1.05 });
      // Twin fins canted 20 deg outboard, ahead of the tailplane.
      d.fin({ x: 11.0, y: 0.45, z: 0.9, chord: 3.5 }, { x: 13.6, chord: 1.35 }, 3.0, 20);
    },
  },

  tomcat: {
    base: { label: "F-14", lengthM: 19.1, spanM: 19.55 },
    // Wings drawn forward (20 deg), as they are in the landing pattern.
    draw(d) {
      d.body([
        [0, 0, -0.05, -0.05],
        [1.5, 0.32, 0.3, -0.32],
        [3.5, 0.55, 0.55, -0.5],
        [5.5, 0.7, 0.7, -0.55],
        [8.0, 0.8, 0.6, -0.5],
        [11, 0.7, 0.45, -0.35],
        [14, 0.5, 0.3, -0.2],
        [16, 0.3, 0.15, -0.1],
      ]);
      d.canopy([
        [3.0, 0, 0.5, 0.5],
        [3.8, 0.38, 0.95, 0.5],
        [6.2, 0.42, 1.05, 0.55],
        [7.4, 0.2, 0.85, 0.6],
        [8.0, 0, 0.62, 0.62],
      ]);
      // Nacelles set wide apart, with the flat "pancake" between them.
      d.body(
        [
          [7.0, 0.55, 0.45, -0.75],
          [9, 0.62, 0.45, -0.7],
          [15, 0.6, 0.4, -0.55],
          [18.4, 0.5, 0.3, -0.4],
          [19.1, 0.45, 0.25, -0.35],
        ],
        { z: 1.55 },
      );
      d.surface({ x: 5.0, y: 0.2, z: 0.6, chord: 11.5 }, { x: 9.2, y: 0.2, z: 2.9, chord: 6.8 }, { thickness: 0.03 });
      d.surface({ x: 9.3, y: 0.25, z: 2.9, chord: 3.6 }, { x: 12.0, y: 0.3, z: 9.775, chord: 1.4 });
      d.surface({ x: 15.3, y: 0.1, z: 1.9, chord: 3.3 }, { x: 17.9, y: 0, z: 4.99, chord: 1.15 });
      d.surface({ x: 15.5, y: 0, z: 0, chord: 3.4 }, { x: 15.8, y: 0, z: 1.0, chord: 3.0 }, { thickness: 0.03 });
      d.fin({ x: 13.4, y: 0.45, z: 1.6, chord: 3.6 }, { x: 16.2, chord: 1.3 }, 2.8, 5);
    },
  },

  viper: {
    base: { label: "F-16C", lengthM: 15.06, spanM: 9.96 },
    draw(d, o) {
      d.body([
        [0, 0, 0, 0],
        [1.2, 0.28, 0.25, -0.25],
        [2.8, 0.45, 0.45, -0.45],
        [4.5, 0.6, 0.6, -0.55],
        [7.0, 0.72, 0.55, -0.6],
        [10, 0.72, 0.55, -0.55],
        [13, 0.6, 0.45, -0.45],
        [14.5, 0.45, 0.35, -0.35],
        [15.06, 0.42, 0.33, -0.33],
      ]);
      if (o.intake === "side") {
        d.body(
          [
            [5.0, 0.3, 0.2, -0.45],
            [5.5, 0.35, 0.25, -0.5],
            [8.5, 0.3, 0.2, -0.4],
            [10, 0.05, 0.05, -0.1],
          ],
          { z: 0.75 },
        );
      } else {
        d.body([
          [4.2, 0.5, -0.45, -1.15],
          [5.0, 0.55, -0.4, -1.2],
          [7.5, 0.55, -0.45, -1.0],
          [9.5, 0.2, -0.5, -0.6],
        ]);
      }
      d.canopy([
        [2.2, 0, 0.4, 0.4],
        [3.0, 0.35, 0.9, 0.4],
        [4.3, 0.4, 1.0, 0.5],
        [5.8, 0.25, 0.8, 0.55],
        [7.0, 0, 0.62, 0.62],
      ]);
      d.surface({ x: 3.8, y: 0.05, z: 0.55, chord: 5.0 }, { x: 7.4, y: 0.05, z: 1.45, chord: 1.5 }, { thickness: 0.03 });
      d.surface({ x: 6.8, y: 0.05, z: 0.7, chord: 5.3 }, { x: 10.4, y: 0.05, z: 4.98, chord: 1.05 });
      // All-moving tailplane with 10 deg of anhedral.
      d.surface({ x: 12.2, y: 0, z: 0.75, chord: 2.6 }, { x: 14.0, y: -0.36, z: 2.79, chord: 0.9 });
      d.fin({ x: 10.4, y: 0.5, z: 0, chord: 3.6 }, { x: 13.0, chord: 1.1 }, 2.9);
    },
  },

  eagle: {
    base: { label: "F-15C", lengthM: 19.43, spanM: 13.05 },
    draw(d, o) {
      d.body([
        [0, 0, -0.05, -0.05],
        [1.5, 0.32, 0.28, -0.35],
        [3.5, 0.55, 0.55, -0.55],
        [5.5, 0.65, 0.7, -0.6],
        [6.6, 1.45, 0.6, -0.9],
        [9, 1.6, 0.6, -0.85],
        [13, 1.55, 0.6, -0.75],
        [16.5, 1.25, 0.5, -0.55],
        [18.6, 0.95, 0.4, -0.4],
        [19.1, 0.9, 0.38, -0.38],
      ]);
      d.canopy(
        o.twoSeat
          ? [
              [3.2, 0, 0.5, 0.5],
              [4.0, 0.4, 1.0, 0.5],
              [7.0, 0.45, 1.12, 0.6],
              [8.6, 0.25, 0.85, 0.6],
              [9.4, 0, 0.62, 0.62],
            ]
          : [
              [3.2, 0, 0.5, 0.5],
              [4.0, 0.4, 1.0, 0.5],
              [5.6, 0.45, 1.1, 0.6],
              [7.4, 0.25, 0.85, 0.6],
              [8.4, 0, 0.62, 0.62],
            ],
      );
      d.surface({ x: 9.2, y: 0.25, z: 1.5, chord: 6.6 }, { x: 14.0, y: 0.25, z: 6.525, chord: 1.9 });
      d.surface({ x: 16.2, y: 0, z: 1.55, chord: 3.1 }, { x: 18.3, y: 0, z: 4.3, chord: 1.1 });
      // Twin fins, upright.
      d.fin({ x: 14.6, y: 0.55, z: 1.45, chord: 3.9 }, { x: 17.2, chord: 1.4 }, 3.15);
    },
  },

  flanker: {
    base: { label: "Su-27", lengthM: 21.94, spanM: 14.7 },
    draw(d, o) {
      const stinger = o.stinger ?? true;
      d.body([
        [0, 0, -0.05, -0.05],
        [1.8, 0.38, 0.32, -0.38],
        [4.0, 0.6, 0.62, -0.55],
        [6.0, 0.72, 0.8, -0.5],
        [9, 0.8, 0.65, -0.35],
        [13, 0.7, 0.5, -0.25],
        [17, 0.5, 0.35, -0.15],
        ...(stinger
          ? ([
              [20.0, 0.3, 0.2, -0.05],
              [21.94, 0.12, 0.12, 0],
            ] as Section[])
          : ([[18.5, 0.3, 0.25, -0.1]] as Section[])),
      ]);
      d.canopy([
        [4.0, 0, 0.62, 0.62],
        [4.9, 0.4, 1.05, 0.6],
        [6.3, 0.45, 1.2, 0.7],
        [7.8, 0.25, 1.0, 0.7],
        [9.2, 0, 0.7, 0.7],
      ]);
      // Engines in separate nacelles under the wide centre body.
      d.body(
        [
          [7.5, 0.6, -0.1, -1.1],
          [8.5, 0.65, -0.05, -1.1],
          [13, 0.62, 0, -0.85],
          [17.5, 0.55, 0, -0.55],
          [19.6, 0.42, 0.05, -0.42],
        ],
        { z: 1.25 },
      );
      // Leading-edge root extension and the lifting centre body behind it.
      d.surface({ x: 4.8, y: 0.15, z: 0.6, chord: 14.6 }, { x: 10.2, y: 0.1, z: 2.6, chord: 8.8 }, { thickness: 0.02 });
      d.surface({ x: 10.0, y: 0.1, z: 2.5, chord: 5.0 }, { x: 14.3, y: 0.1, z: 7.35, chord: 1.4 });
      d.body(
        [
          [15.0, 0.2, 0.2, -0.2],
          [20.6, 0.2, 0.2, -0.2],
          [21.0, 0, 0, 0],
        ],
        { z: 2.15 },
      );
      d.surface({ x: 18.0, y: 0, z: 2.0, chord: 3.0 }, { x: 20.3, y: 0, z: 4.95, chord: 1.2 });
      d.fin({ x: 15.6, y: 0.2, z: 2.15, chord: 3.9 }, { x: 18.6, chord: 1.2 }, 3.7, o.finCantDeg ?? 0);
      if (o.canards) {
        d.surface({ x: 6.8, y: 0.25, z: 0.8, chord: 2.2 }, { x: 8.1, y: 0.25, z: 3.1, chord: 0.8 });
      }
    },
  },

  mirage2000: {
    base: { label: "Mirage 2000C", lengthM: 14.36, spanM: 9.13 },
    draw(d) {
      d.body([
        [0, 0, 0, 0],
        [1.5, 0.28, 0.25, -0.28],
        [3.5, 0.48, 0.5, -0.45],
        [5.0, 0.58, 0.62, -0.5],
        [6.0, 0.95, 0.55, -0.55],
        [9, 1.0, 0.5, -0.55],
        [12.5, 0.75, 0.4, -0.45],
        [14.0, 0.5, 0.3, -0.3],
        [14.36, 0.45, 0.28, -0.28],
      ]);
      d.canopy([
        [3.0, 0, 0.45, 0.45],
        [3.8, 0.36, 0.88, 0.45],
        [5.0, 0.4, 0.95, 0.5],
        [6.4, 0.2, 0.75, 0.55],
        [7.2, 0, 0.58, 0.58],
      ]);
      // Tailless delta.
      d.surface({ x: 5.6, y: -0.1, z: 0.8, chord: 8.4 }, { x: 11.7, y: -0.1, z: 4.565, chord: 1.4 });
      d.fin({ x: 9.8, y: 0.45, z: 0, chord: 4.2 }, { x: 13.0, chord: 0.9 }, 3.0);
    },
  },

  canardDelta: {
    base: { label: "AJS 37", lengthM: 16.4, spanM: 10.6 },
    draw(d, o) {
      d.body([
        [0, 0, 0, 0],
        [2.0, 0.35, 0.3, -0.35],
        [4.0, 0.55, 0.55, -0.55],
        [5.5, 1.0, 0.55, -0.6],
        [10, 0.95, 0.55, -0.6],
        [14, 0.75, 0.45, -0.45],
        [16.0, 0.6, 0.38, -0.38],
        [16.4, 0.55, 0.35, -0.35],
      ]);
      d.canopy([
        [3.2, 0, 0.5, 0.5],
        [4.0, 0.38, 0.9, 0.5],
        [5.4, 0.4, 0.98, 0.55],
        [6.8, 0.2, 0.75, 0.55],
        [7.5, 0, 0.58, 0.58],
      ]);
      const cx = o.canardX ?? 5.4;
      d.surface({ x: cx, y: 0.35, z: 0.9, chord: 3.0 }, { x: cx + 1.4, y: 0.4, z: 2.72, chord: 1.6 });
      d.surface({ x: 8.6, y: -0.15, z: 0.9, chord: 7.0 }, { x: 14.0, y: -0.15, z: 5.3, chord: 1.4 });
      d.fin({ x: 10.8, y: 0.5, z: 0, chord: 4.6 }, { x: 14.2, chord: 1.2 }, 3.1);
    },
  },

  harrier: {
    base: { label: "AV-8B", lengthM: 14.12, spanM: 9.25 },
    draw(d) {
      d.body([
        [0, 0, -0.05, -0.05],
        [1.5, 0.35, 0.3, -0.35],
        [3.2, 0.55, 0.55, -0.55],
        [4.8, 1.15, 0.6, -0.7],
        [8, 1.1, 0.6, -0.7],
        [10.5, 0.7, 0.5, -0.45],
        [12.5, 0.4, 0.35, -0.25],
        [14.12, 0.12, 0.15, -0.05],
      ]);
      d.canopy([
        [2.4, 0, 0.5, 0.5],
        [3.2, 0.42, 1.05, 0.5],
        [4.6, 0.45, 1.15, 0.55],
        [6.0, 0.25, 0.9, 0.6],
        [7.2, 0, 0.62, 0.62],
      ]);
      // Shoulder wing drooping 12 deg to the tips.
      d.surface({ x: 6.0, y: 0.6, z: 0.6, chord: 4.4 }, { x: 8.6, y: -0.25, z: 4.625, chord: 1.3 });
      d.surface({ x: 11.4, y: 0.2, z: 0.35, chord: 2.0 }, { x: 12.9, y: -0.3, z: 2.12, chord: 0.8 });
      d.fin({ x: 10.6, y: 0.4, z: 0, chord: 2.6 }, { x: 12.6, chord: 0.9 }, 1.9);
    },
  },

  warthog: {
    base: { label: "A-10", lengthM: 16.26, spanM: 17.53 },
    draw(d) {
      d.body([
        [0, 0, -0.1, -0.1],
        [1.2, 0.4, 0.3, -0.45],
        [3.0, 0.62, 0.6, -0.7],
        [5.5, 0.75, 0.7, -0.8],
        [9.0, 0.7, 0.6, -0.7],
        [12.5, 0.45, 0.45, -0.4],
        [15.0, 0.3, 0.35, -0.2],
        [15.6, 0.25, 0.3, -0.15],
      ]);
      d.canopy([
        [2.5, 0, 0.55, 0.55],
        [3.2, 0.45, 1.1, 0.55],
        [4.6, 0.5, 1.2, 0.6],
        [5.6, 0.2, 0.9, 0.65],
        [6.0, 0, 0.7, 0.7],
      ]);
      d.surface({ x: 6.3, y: -0.55, z: 0.6, chord: 3.1 }, { x: 7.3, y: -0.2, z: 8.765, chord: 1.6 });
      // Engine pods on pylons high on the rear fuselage.
      d.body(
        [
          [9.0, 0.55, 1.75, 0.65],
          [9.4, 0.62, 1.82, 0.58],
          [11.6, 0.55, 1.75, 0.65],
          [12.4, 0.42, 1.62, 0.78],
        ],
        { z: 1.35 },
      );
      d.surface({ x: 9.8, y: 0.5, z: 0.45, chord: 1.8 }, { x: 9.9, y: 1.0, z: 1.0, chord: 1.6 }, { thickness: 0.05 });
      // Straight tailplane with a fin at each tip.
      d.surface({ x: 13.8, y: 0.3, z: 0.2, chord: 2.2 }, { x: 13.8, y: 0.3, z: 2.87, chord: 2.0 });
      d.surface({ x: 13.6, y: -0.6, z: 2.87, chord: 2.5 }, { x: 13.9, y: 2.3, z: 2.87, chord: 2.1 });
    },
  },

  frogfoot: {
    base: { label: "Su-25", lengthM: 15.53, spanM: 14.36 },
    draw(d) {
      d.body([
        [0, 0, -0.05, -0.05],
        [1.5, 0.3, 0.25, -0.35],
        [3.5, 0.55, 0.6, -0.6],
        [5.5, 0.65, 0.7, -0.65],
        [9, 0.6, 0.6, -0.55],
        [12.5, 0.4, 0.45, -0.3],
        [15.0, 0.2, 0.3, -0.1],
        [15.53, 0.1, 0.2, 0],
      ]);
      d.canopy([
        [2.8, 0, 0.55, 0.55],
        [3.4, 0.4, 1.0, 0.55],
        [4.6, 0.42, 1.05, 0.6],
        [5.4, 0, 0.7, 0.7],
      ]);
      d.body(
        [
          [5.2, 0.45, 0.2, -0.7],
          [5.6, 0.5, 0.25, -0.75],
          [10.5, 0.48, 0.2, -0.7],
          [12.2, 0.4, 0.15, -0.55],
        ],
        { z: 0.95 },
      );
      d.surface({ x: 6.8, y: 0.4, z: 1.2, chord: 3.8 }, { x: 8.9, y: 0.1, z: 7.18, chord: 1.3 });
      d.surface({ x: 12.8, y: 0.8, z: 0.2, chord: 2.2 }, { x: 14.3, y: 1.0, z: 2.33, chord: 1.0 });
      d.fin({ x: 11.6, y: 0.4, z: 0, chord: 3.2 }, { x: 14.2, chord: 1.2 }, 2.8);
    },
  },

  phantom: {
    base: { label: "F-4E", lengthM: 19.2, spanM: 11.7 },
    draw(d) {
      d.body([
        [0, 0, -0.05, -0.05],
        [2.0, 0.35, 0.3, -0.35],
        [4.5, 0.6, 0.62, -0.55],
        [6.5, 0.7, 0.72, -0.6],
        [7.5, 1.2, 0.62, -0.7],
        [11, 1.25, 0.6, -0.65],
        [14.5, 0.9, 0.5, -0.5],
        [17.0, 0.5, 0.4, -0.3],
        [19.2, 0.2, 0.3, 0],
      ]);
      d.canopy([
        [4.3, 0, 0.6, 0.6],
        [5.0, 0.42, 1.05, 0.6],
        [8.2, 0.42, 1.08, 0.6],
        [9.3, 0.2, 0.85, 0.6],
        [9.8, 0, 0.66, 0.66],
      ]);
      // Flat inner wing, outer panels turned up 12 deg.
      d.panels([
        { x: 8.6, y: -0.35, z: 1.1, chord: 5.6 },
        { x: 11.6, y: -0.35, z: 4.2, chord: 2.8 },
        { x: 13.2, y: 0, z: 5.85, chord: 1.4 },
      ]);
      // Stabilator drooping 23 deg.
      d.surface({ x: 16.4, y: 0.35, z: 0.4, chord: 2.3 }, { x: 17.9, y: -0.58, z: 2.65, chord: 0.8 });
      d.fin({ x: 14.4, y: 0.5, z: 0, chord: 4.2 }, { x: 17.6, chord: 1.0 }, 2.9);
    },
  },

  tiger: {
    base: { label: "F-5E", lengthM: 14.45, spanM: 8.13 },
    draw(d) {
      d.body([
        [0, 0, 0, 0],
        [2.0, 0.25, 0.22, -0.25],
        [4.0, 0.45, 0.45, -0.45],
        [5.5, 0.52, 0.55, -0.48],
        [6.3, 0.8, 0.5, -0.55],
        [10, 0.8, 0.45, -0.5],
        [13.0, 0.6, 0.35, -0.35],
        [14.2, 0.45, 0.3, -0.3],
        [14.45, 0.42, 0.28, -0.28],
      ]);
      d.canopy([
        [3.8, 0, 0.42, 0.42],
        [4.5, 0.34, 0.8, 0.42],
        [5.8, 0.36, 0.85, 0.45],
        [7.0, 0, 0.52, 0.52],
      ]);
      d.surface({ x: 5.0, y: 0, z: 0.45, chord: 3.0 }, { x: 7.3, y: 0, z: 0.95, chord: 0.8 }, { thickness: 0.03 });
      d.surface({ x: 7.0, y: -0.2, z: 0.7, chord: 3.6 }, { x: 9.05, y: -0.2, z: 4.065, chord: 1.2 });
      d.surface({ x: 12.2, y: 0, z: 0.5, chord: 1.8 }, { x: 13.5, y: 0, z: 2.2, chord: 0.7 });
      d.fin({ x: 10.8, y: 0.4, z: 0, chord: 3.0 }, { x: 13.2, chord: 0.9 }, 2.1);
    },
  },

  fishbed: {
    base: { label: "MiG-21bis", lengthM: 14.7, spanM: 7.15 },
    draw(d) {
      // Nose intake with its shock cone.
      d.body([
        [0, 0, 0, 0],
        [0.5, 0.35, 0.35, -0.35],
        [1.0, 0.55, 0.55, -0.55],
        [4.0, 0.62, 0.68, -0.6],
        [7, 0.65, 0.62, -0.6],
        [11, 0.6, 0.55, -0.55],
        [13.5, 0.5, 0.45, -0.45],
        [14.7, 0.45, 0.42, -0.42],
      ]);
      d.body([
        [4.8, 0.28, 0.95, 0.5],
        [9, 0.25, 0.8, 0.5],
        [11.5, 0, 0.6, 0.6],
      ]);
      d.canopy([
        [3.0, 0, 0.62, 0.62],
        [3.6, 0.34, 0.98, 0.62],
        [4.9, 0.34, 1.0, 0.62],
        [5.5, 0, 0.92, 0.92],
      ]);
      d.surface({ x: 6.0, y: -0.2, z: 0.55, chord: 5.4 }, { x: 10.5, y: -0.2, z: 3.575, chord: 0.6 });
      d.surface({ x: 12.3, y: 0, z: 0.5, chord: 1.9 }, { x: 13.8, y: 0, z: 1.9, chord: 0.6 });
      d.fin({ x: 10.5, y: 0.5, z: 0, chord: 3.5 }, { x: 13.3, chord: 1.1 }, 2.4);
    },
  },

  sabre: {
    base: { label: "F-86F", lengthM: 11.4, spanM: 11.3 },
    draw(d, o) {
      d.body([
        [0, 0.38, 0.35, -0.4],
        [1.5, 0.6, 0.6, -0.6],
        [3.5, 0.72, 0.72, -0.7],
        [6, 0.68, 0.65, -0.65],
        [9, 0.48, 0.5, -0.45],
        [11.0, 0.32, 0.35, -0.3],
        [11.4, 0.3, 0.32, -0.28],
      ]);
      // The nose intake, dark.
      d.disc([0.02, -0.02, 0], "x", 0.34, 0.02, GLASS);
      d.canopy([
        [2.2, 0, 0.6, 0.6],
        [2.8, 0.36, 1.05, 0.6],
        [4.0, 0.4, 1.1, 0.62],
        [5.0, 0, 0.7, 0.7],
      ]);
      // More sweep turns the wing about mid-span, so a MiG-19's tips stay
      // ahead of the tail instead of trailing past it.
      const sweep = Math.tan((o.sweepDeg ?? 35) * DEG);
      const rootX = 3.9 - (sweep - Math.tan(35 * DEG)) * 2.5;
      d.surface({ x: rootX, y: -0.3, z: 0.65, chord: 2.9 }, { x: rootX + 5.0 * sweep, y: -0.05, z: 5.65, chord: 1.3 });
      if (o.stabOnFin) {
        d.surface({ x: 9.9, y: 1.6, z: 0.1, chord: 1.2 }, { x: 10.9, y: 1.6, z: 1.9, chord: 0.6 });
      } else {
        d.surface({ x: 9.3, y: 0.35, z: 0.3, chord: 1.4 }, { x: 10.6, y: 0.4, z: 2.2, chord: 0.7 });
      }
      d.fin({ x: 8.3, y: 0.5, z: 0, chord: 2.6 }, { x: 10.5, chord: 1.0 }, 2.05);
    },
  },

  mirageF1: {
    base: { label: "Mirage F1", lengthM: 15.3, spanM: 8.4 },
    draw(d) {
      d.body([
        [0, 0, 0, 0],
        [2.0, 0.3, 0.3, -0.3],
        [4.0, 0.52, 0.55, -0.5],
        [5.5, 0.58, 0.62, -0.52],
        [6.3, 0.95, 0.55, -0.55],
        [10, 0.95, 0.5, -0.55],
        [13, 0.7, 0.42, -0.42],
        [15.0, 0.5, 0.35, -0.35],
        [15.3, 0.48, 0.33, -0.33],
      ]);
      d.canopy([
        [3.4, 0, 0.5, 0.5],
        [4.2, 0.36, 0.9, 0.5],
        [5.6, 0.38, 0.95, 0.55],
        [7.0, 0, 0.62, 0.62],
      ]);
      d.surface({ x: 7.0, y: 0.45, z: 0.8, chord: 3.8 }, { x: 10.7, y: 0.25, z: 4.2, chord: 1.1 });
      d.surface({ x: 12.6, y: -0.1, z: 0.6, chord: 2.0 }, { x: 14.3, y: -0.1, z: 2.3, chord: 0.8 });
      d.fin({ x: 11.2, y: 0.45, z: 0, chord: 3.8 }, { x: 14.2, chord: 1.0 }, 2.6);
    },
  },

  trainer: {
    base: { label: "L-39", lengthM: 12.13, spanM: 9.46 },
    draw(d, o) {
      const tipTanks = o.tipTanks ?? true;
      d.body([
        [0, 0, -0.1, -0.1],
        [1.5, 0.35, 0.3, -0.4],
        [3.5, 0.55, 0.62, -0.6],
        [5.5, 0.7, 0.7, -0.6],
        [7.5, 0.62, 0.6, -0.55],
        [10.2, 0.35, 0.42, -0.3],
        [12.13, 0.2, 0.3, -0.1],
      ]);
      // Tandem seats under one long canopy.
      d.canopy([
        [2.2, 0, 0.55, 0.55],
        [2.8, 0.4, 1.0, 0.55],
        [5.0, 0.42, 1.12, 0.6],
        [5.9, 0, 0.8, 0.8],
      ]);
      const tipZ = tipTanks ? 4.45 : 4.73;
      const sweep = Math.tan((o.sweepDeg ?? 8) * DEG);
      d.surface({ x: 4.6, y: -0.45, z: 0.6, chord: 2.8 }, { x: 4.6 + (tipZ - 0.6) * sweep, y: -0.2, z: tipZ, chord: 1.4 });
      if (tipTanks) {
        d.body(
          [
            [4.6, 0, -0.25, -0.25],
            [5.2, 0.2, -0.05, -0.45],
            [6.8, 0.2, -0.05, -0.45],
            [7.5, 0, -0.25, -0.25],
          ],
          { z: 4.53 },
        );
      }
      d.surface({ x: 10.0, y: 0.45, z: 0.3, chord: 1.4 }, { x: 10.6, y: 0.5, z: 2.2, chord: 0.8 });
      d.fin({ x: 9.2, y: 0.4, z: 0, chord: 2.3 }, { x: 11.0, chord: 1.0 }, 2.0);
    },
  },

  skyhawk: {
    base: { label: "A-4E", lengthM: 12.22, spanM: 8.38 },
    draw(d) {
      d.body([
        [0, 0, -0.1, -0.1],
        [1.5, 0.3, 0.28, -0.35],
        [3.5, 0.5, 0.55, -0.55],
        [4.5, 0.85, 0.6, -0.6],
        [8, 0.8, 0.55, -0.55],
        [10.5, 0.5, 0.42, -0.35],
        [12.22, 0.25, 0.3, -0.15],
      ]);
      d.canopy([
        [2.6, 0, 0.5, 0.5],
        [3.2, 0.38, 0.95, 0.5],
        [4.4, 0.4, 1.0, 0.55],
        [5.2, 0, 0.65, 0.65],
      ]);
      d.surface({ x: 4.3, y: -0.4, z: 0.6, chord: 4.9 }, { x: 6.8, y: -0.25, z: 4.19, chord: 1.4 });
      // Tailplane set halfway up the fin.
      d.surface({ x: 10.4, y: 1.35, z: 0.1, chord: 1.4 }, { x: 11.4, y: 1.35, z: 1.7, chord: 0.7 });
      d.fin({ x: 8.7, y: 0.4, z: 0, chord: 3.0 }, { x: 11.1, chord: 1.0 }, 2.5);
    },
  },

  swingWing: {
    base: { label: "Tornado", lengthM: 16.72, spanM: 13.91 },
    // Wings drawn forward (25 deg), as they are for landing.
    draw(d) {
      d.body([
        [0, 0, -0.05, -0.05],
        [2.0, 0.4, 0.35, -0.4],
        [4.0, 0.6, 0.62, -0.6],
        [6.0, 0.7, 0.8, -0.6],
        [7.0, 1.1, 0.7, -0.7],
        [11, 1.15, 0.65, -0.65],
        [14.5, 0.95, 0.5, -0.5],
        [16.3, 0.75, 0.4, -0.4],
        [16.72, 0.7, 0.38, -0.38],
      ]);
      d.canopy([
        [3.4, 0, 0.6, 0.6],
        [4.2, 0.42, 1.05, 0.6],
        [6.6, 0.44, 1.12, 0.65],
        [7.8, 0, 0.78, 0.78],
      ]);
      // Fixed glove, then the swinging outer wing from its pivot.
      d.surface({ x: 6.0, y: 0.4, z: 0.9, chord: 6.0 }, { x: 8.4, y: 0.4, z: 2.0, chord: 3.8 }, { thickness: 0.04 });
      d.surface({ x: 8.6, y: 0.4, z: 2.0, chord: 3.2 }, { x: 10.8, y: 0.4, z: 6.955, chord: 1.4 });
      d.surface({ x: 13.4, y: -0.1, z: 1.0, chord: 3.0 }, { x: 15.4, y: -0.3, z: 3.8, chord: 1.2 });
      d.fin({ x: 11.2, y: 0.5, z: 0, chord: 4.8 }, { x: 14.8, chord: 1.4 }, 3.7);
    },
  },

  warbird: {
    base: { label: "P-51D", lengthM: 9.83, spanM: 11.28 },
    draw(d, o) {
      if (o.twin) {
        // Engines on the wings, a glazed nose between them.
        d.body([
          [0, 0, 0, 0],
          [0.4, 0.35, 0.3, -0.3],
          [1.5, 0.55, 0.6, -0.55],
          [4, 0.55, 0.6, -0.6],
          [6.5, 0.4, 0.45, -0.45],
          [9.83, 0.1, 0.25, 0.05],
        ]);
        d.canopy([
          [0.8, 0, 0.45, 0.45],
          [1.2, 0.4, 0.85, 0.45],
          [2.2, 0.45, 0.9, 0.5],
          [2.8, 0, 0.6, 0.6],
        ]);
        d.body(
          [
            [0.9, 0.3, 0.1, -0.5],
            [1.3, 0.4, 0.2, -0.65],
            [3.6, 0.35, 0.1, -0.55],
            [4.8, 0.1, -0.2, -0.3],
          ],
          { z: 2.0 },
        );
        d.rotor([0.8, -0.2, 2.0], "x", 1.25, o.blades ?? 3, { chord: 0.2 });
      } else {
        d.body([
          [0, 0, 0, 0],
          [0.5, 0.3, 0.3, -0.3],
          [0.65, 0.45, 0.45, -0.5],
          [2.0, 0.5, 0.55, -0.65],
          [3.8, 0.5, 0.6, -0.7],
          [6.0, 0.4, 0.55, -0.55],
          [8.5, 0.22, 0.35, -0.25],
          [9.83, 0.08, 0.2, 0],
        ]);
        if (o.scoop) {
          d.body([
            [4.0, 0.3, -0.55, -0.95],
            [4.5, 0.32, -0.55, -1.05],
            [6.2, 0.3, -0.5, -0.9],
            [7.0, 0, -0.5, -0.5],
          ]);
        }
        d.canopy([
          [2.8, 0, 0.55, 0.55],
          [3.3, 0.35, 0.95, 0.55],
          [4.3, 0.36, 1.0, 0.58],
          [5.2, 0, 0.6, 0.6],
        ]);
        d.rotor([0.3, 0, 0], "x", 1.7, o.blades ?? 4, { chord: 0.22 });
      }
      if (o.elliptic) {
        // Straight quarter-chord line, chord falling away as an ellipse.
        d.panels(
          [0, 0.4, 0.65, 0.82, 0.93, 1].map((t) => {
            const chord = Math.max(0.35, 2.6 * Math.sqrt(1 - t * t));
            return { x: 3.18 - 0.3 * chord, y: -0.55 + 0.45 * t, z: 0.45 + 5.19 * t, chord };
          }),
        );
      } else {
        d.surface({ x: 2.4, y: -0.55, z: 0.45, chord: 2.6 }, { x: 3.4, y: -0.1, z: 5.64, chord: 1.0 });
      }
      if (o.biplane) {
        d.surface({ x: 1.9, y: 1.15, z: 0, chord: 2.3 }, { x: 2.5, y: 1.3, z: 5.64, chord: 1.2 });
        d.surface({ x: 2.9, y: -0.4, z: 3.8, chord: 0.2 }, { x: 2.5, y: 1.2, z: 3.8, chord: 0.2 }, { thickness: 0.5 });
      }
      d.surface({ x: 8.2, y: 0.25, z: 0.2, chord: 1.4 }, { x: 8.8, y: 0.25, z: 2.05, chord: 0.7 });
      d.fin({ x: 7.8, y: 0.3, z: 0, chord: 1.9 }, { x: 9.0, chord: 0.8 }, 1.5);
    },
  },

  hawkeye: {
    base: { label: "E-2C", lengthM: 17.54, spanM: 24.56 },
    draw(d, o) {
      d.body([
        [0, 0, -0.2, -0.2],
        [1.0, 0.6, 0.5, -0.7],
        [3.0, 0.95, 0.9, -0.95],
        [6, 1.05, 1.0, -1.0],
        [10, 1.0, 0.95, -0.9],
        [13.5, 0.6, 0.6, -0.4],
        [16.5, 0.25, 0.4, 0.05],
      ]);
      d.canopy([
        [0.9, 0, 0.45, 0.45],
        [1.6, 0.75, 1.0, 0.45],
        [2.8, 0.85, 1.1, 0.6],
        [3.3, 0, 0.95, 0.95],
      ]);
      d.surface({ x: 5.2, y: 1.0, z: 0.8, chord: 3.4 }, { x: 5.9, y: 1.15, z: 12.28, chord: 1.5 });
      d.body(
        [
          [3.3, 0.35, 1.1, 0.4],
          [4.0, 0.6, 1.35, 0.05],
          [7.8, 0.55, 1.3, 0.1],
          [9.8, 0.15, 1.05, 0.75],
        ],
        { z: 3.8 },
      );
      d.rotor([3.1, 0.75, 3.8], "x", 2.05, o.blades ?? 4, { chord: 0.3 });
      if (o.radome ?? true) {
        d.disc([8.6, 3.3, 0], "y", 3.66, 0.38);
        d.surface({ x: 7.6, y: 1.0, z: 0.3, chord: 1.2 }, { x: 7.9, y: 3.0, z: 0.3, chord: 1.0 });
      }
      // Tailplane with dihedral carrying four fins.
      d.surface({ x: 14.8, y: 0.5, z: 0.3, chord: 2.2 }, { x: 15.4, y: 1.3, z: 4.0, chord: 1.6 });
      d.surface({ x: 14.7, y: 0.1, z: 4.0, chord: 2.3 }, { x: 15.3, y: 3.3, z: 4.0, chord: 1.6 });
      d.surface({ x: 14.7, y: 0.2, z: 1.9, chord: 2.3 }, { x: 15.2, y: 3.0, z: 1.9, chord: 1.6 });
    },
  },

  viking: {
    base: { label: "S-3B", lengthM: 16.26, spanM: 20.93 },
    draw(d) {
      d.body([
        [0, 0, -0.2, -0.2],
        [1.0, 0.7, 0.5, -0.75],
        [3, 1.0, 0.9, -1.1],
        [7, 1.05, 0.95, -1.1],
        [11, 0.9, 0.85, -0.8],
        [14, 0.45, 0.6, -0.2],
        [16.26, 0.15, 0.4, 0.2],
      ]);
      d.canopy([
        [1.0, 0, 0.5, 0.5],
        [1.6, 0.8, 1.0, 0.5],
        [3.0, 0.9, 1.1, 0.6],
        [3.4, 0, 0.95, 0.95],
      ]);
      d.surface({ x: 5.4, y: 1.05, z: 0.9, chord: 3.8 }, { x: 7.9, y: 1.3, z: 10.465, chord: 1.4 });
      d.body(
        [
          [5.0, 0.5, 0.25, -0.75],
          [5.4, 0.6, 0.35, -0.85],
          [8.4, 0.52, 0.27, -0.77],
          [9.4, 0.35, 0.1, -0.6],
        ],
        { z: 2.9 },
      );
      d.surface({ x: 6.0, y: 0.3, z: 2.9, chord: 2.2 }, { x: 6.3, y: 1.1, z: 2.9, chord: 2.4 }, { thickness: 0.05 });
      d.surface({ x: 13.4, y: 0.6, z: 0.4, chord: 2.6 }, { x: 15.0, y: 0.75, z: 4.0, chord: 1.2 });
      d.fin({ x: 11.2, y: 0.8, z: 0, chord: 4.2 }, { x: 14.4, chord: 1.8 }, 4.4);
    },
  },

  hercules: {
    base: { label: "C-130", lengthM: 29.79, spanM: 40.41 },
    draw(d, o) {
      d.body([
        [0, 0, -0.3, -0.3],
        [1.2, 1.0, 0.9, -1.4],
        [3.5, 1.95, 1.9, -2.0],
        [8, 2.1, 2.3, -2.1],
        [18, 2.1, 2.3, -2.1],
        [23, 1.5, 2.2, -0.8],
        [27, 0.7, 2.0, 0.8],
        [29.0, 0.3, 1.9, 1.4],
      ]);
      d.canopy([
        [1.0, 0, 0.9, 0.9],
        [1.8, 1.3, 1.8, 0.9],
        [3.2, 1.7, 2.35, 1.3],
        [3.8, 0, 2.2, 2.2],
      ]);
      d.surface({ x: 10.4, y: 2.45, z: 1.5, chord: 4.9 }, { x: 12.0, y: 2.8, z: 20.205, chord: 2.5 });
      for (const z of [5.0, 10.0]) {
        d.body(
          [
            [8.3, 0.5, 2.4, 1.6],
            [9.0, 0.65, 2.6, 1.1],
            [13.5, 0.55, 2.55, 1.2],
            [15.0, 0.2, 2.45, 2.0],
          ],
          { z },
        );
        d.rotor([8.0, 1.95, z], "x", 2.05, o.blades ?? 4, { chord: 0.35 });
      }
      d.surface({ x: 25.5, y: 2.0, z: 0.4, chord: 3.8 }, { x: 27.3, y: 2.1, z: 7.85, chord: 1.9 });
      d.fin({ x: 22.6, y: 2.2, z: 0, chord: 6.8 }, { x: 27.0, chord: 2.6 }, 7.0);
    },
  },

  jetTransport: {
    base: { label: "KC-135", lengthM: 41.53, spanM: 39.88 },
    draw(d, o) {
      d.body([
        [0, 0, -0.2, -0.2],
        [1.5, 1.2, 0.9, -1.2],
        [4.5, 1.85, 1.85, -1.85],
        [10, 1.9, 1.9, -1.9],
        [30, 1.9, 1.9, -1.9],
        [36, 1.2, 1.6, -0.6],
        [41.0, 0.3, 1.2, 0.6],
        [41.53, 0.2, 1.15, 0.75],
      ]);
      d.canopy([
        [1.6, 0, 1.0, 1.0],
        [2.2, 1.1, 1.55, 1.0],
        [3.6, 1.35, 1.9, 1.3],
        [4.0, 0, 1.75, 1.75],
      ]);
      const rootY = o.highWing ? 1.7 : -1.2;
      const tipY = o.highWing ? 0.9 : 0.3;
      d.surface({ x: 13.5, y: rootY, z: 1.8, chord: 8.0 }, { x: 26.0, y: tipY, z: 19.94, chord: 2.5 });
      // Four engines on pylons, hung ahead of the swept leading edge.
      for (const f of [0.3, 0.63]) {
        const z = 1.8 + (19.94 - 1.8) * f;
        const wingY = rootY + (tipY - rootY) * f;
        const x = 13.5 + (26.0 - 13.5) * f - 3.2;
        const y = wingY - 1.3;
        d.body(
          [
            [x, 0.55, y + 0.55, y - 0.55],
            [x + 0.6, 0.72, y + 0.72, y - 0.72],
            [x + 3.6, 0.62, y + 0.62, y - 0.62],
            [x + 4.4, 0.35, y + 0.35, y - 0.35],
          ],
          { z },
        );
        d.surface({ x: x + 1.2, y: y + 0.5, z, chord: 3.0 }, { x: x + 2.4, y: wingY, z, chord: 3.0 }, { thickness: 0.05 });
      }
      if (o.radome) {
        d.disc([27, 5.0, 0], "y", 4.57, 0.9);
        d.surface({ x: 25.5, y: 1.8, z: 0.5, chord: 2.5 }, { x: 26.3, y: 4.2, z: 0.5, chord: 2.0 });
      }
      if (o.tTail) {
        d.fin({ x: 31.5, y: 1.8, z: 0, chord: 8.0 }, { x: 37.5, chord: 4.5 }, 8.0);
        d.surface({ x: 36.5, y: 9.6, z: 0.3, chord: 4.5 }, { x: 39.5, y: 9.6, z: 8.0, chord: 2.0 });
      } else {
        d.fin({ x: 32.0, y: 1.8, z: 0, chord: 7.5 }, { x: 38.8, chord: 2.8 }, 8.7);
        d.surface({ x: 35.2, y: 0.8, z: 0.8, chord: 5.0 }, { x: 39.2, y: 1.2, z: 6.8, chord: 1.8 });
      }
    },
  },
} satisfies Record<string, Family>;

type FamilyKey = keyof typeof FAMILIES;

// ---------------------------------------------------------------------------
// Helicopters: drawn from their own figures rather than stretched, since the
// rotor must keep its real diameter whatever the fuselage does.
// ---------------------------------------------------------------------------

interface Rotorcraft {
  /** Fuselage length, nose to tail, rotors excluded (m). */
  length: number;
  /** Cabin width and height (m). */
  width: number;
  height: number;
  rotorRadius: number;
  blades: number;
  /** A conventional tail rotor (m); absent for coaxial and tandem rotors. */
  tailRotorRadius?: number;
  tailBlades?: number;
  layout?: "single" | "coaxial" | "tandem";
  fenestron?: boolean;
  /** Span of the stub wings (m). */
  stubSpan?: number;
  /** Stepped tandem cockpit of a gunship. */
  tandemCockpit?: boolean;
  skids?: boolean;
  mastSight?: boolean;
}

function drawRotorcraft(d: Drawing, h: Rotorcraft) {
  const { length: L, width: W, height: H } = h;
  const layout = h.layout ?? "single";
  const top = H * 0.55;
  const bottom = -H * 0.45;
  const s = L / 12; // tail parts scale with the aircraft
  if (layout === "tandem") {
    d.body([
      [0, W * 0.2, top * 0.2, bottom * 0.5],
      [0.05 * L, W * 0.45, top * 0.8, bottom * 0.95],
      [0.12 * L, W * 0.5, top, bottom],
      [0.85 * L, W * 0.5, top, bottom],
      [0.97 * L, W * 0.4, top, bottom * 0.3],
      [L, W * 0.3, top * 0.9, top * 0.1],
    ]);
    // The aft rotor's tall pylon.
    d.body([
      [0.76 * L, W * 0.2, top, top - 0.2],
      [0.8 * L, W * 0.2, top + 1.7, top - 0.2],
      [0.97 * L, W * 0.14, top + 1.7, top - 0.2],
      [L, W * 0.1, top + 1.4, top],
    ]);
  } else {
    d.body([
      [0, W * 0.18, top * 0.1, bottom * 0.5],
      [0.07 * L, W * 0.42, top * 0.65, bottom * 0.9],
      [0.18 * L, W * 0.5, top, bottom],
      [0.45 * L, W * 0.5, top, bottom * 0.9],
      [0.58 * L, W * 0.28, top * 0.8, bottom * 0.2],
      [0.64 * L, W * 0.08 + 0.1, top * 0.55, top * 0.1],
      [L, W * 0.05 + 0.06, top * 0.6, top * 0.3],
    ]);
  }
  d.canopy(
    h.tandemCockpit
      ? [
          [0.02 * L, 0, top * 0.3, top * 0.3],
          [0.08 * L, W * 0.32, top * 1.0, top * 0.1],
          [0.3 * L, W * 0.32, top * 1.3, top * 0.3],
          [0.36 * L, 0, top * 1.05, top * 1.05],
        ]
      : [
          [0.005 * L, 0, top * 0.1, top * 0.1],
          [0.05 * L, W * 0.42, top * 0.6, bottom * 0.3],
          [0.15 * L, W * 0.5, top * 1.02, 0],
          [0.19 * L, 0, top * 1.02, top * 1.02],
        ],
  );

  const R = h.rotorRadius;
  const n = h.blades;
  const mastTop = top + Math.max(0.5, H * 0.3);
  const mast = (x: number, height: number) =>
    d.body([
      [x - 0.3, 0.22, top + height, top - 0.1],
      [x + 0.3, 0.22, top + height, top - 0.1],
    ]);
  if (layout === "tandem") {
    d.rotor([0.1 * L, top + 0.6, 0], "y", R, n);
    d.rotor([0.9 * L, top + 2.0, 0], "y", R, n, { phaseDeg: 60 });
  } else {
    const x = (layout === "coaxial" ? 0.38 : 0.33) * L;
    mast(x, mastTop - top);
    d.rotor([x, mastTop, 0], "y", R, n);
    if (layout === "coaxial") {
      mast(x, mastTop - top + 0.12 * R);
      d.rotor([x, mastTop + 0.12 * R, 0], "y", R, n, { phaseDeg: 180 / n });
    }
    if (h.mastSight) d.body([[x - 0.35, 0.35, mastTop + 0.9, mastTop + 0.2], [x + 0.35, 0.35, mastTop + 0.9, mastTop + 0.2]]);
  }

  if (h.stubSpan) {
    d.surface({ x: 0.4 * L, y: 0, z: W * 0.4, chord: 0.09 * L }, { x: 0.42 * L, y: -0.1 * h.stubSpan, z: h.stubSpan / 2, chord: 0.07 * L });
  }
  if (h.skids) {
    const zs = W * 0.55;
    const ys = bottom - 0.45;
    d.body(
      [
        [0.06 * L, 0.05, ys + 0.2, ys + 0.1],
        [0.12 * L, 0.06, ys + 0.04, ys - 0.04],
        [0.55 * L, 0.06, ys + 0.04, ys - 0.04],
      ],
      { z: zs },
    );
    for (const x of [0.2 * L, 0.45 * L]) {
      d.surface({ x, y: bottom, z: W * 0.3, chord: 0.12 }, { x, y: ys, z: zs, chord: 0.12 }, { thickness: 0.5 });
    }
  }

  if (layout !== "tandem") {
    const finY = top * 0.45;
    d.fin({ x: L - 1.4 * s, y: finY, z: 0, chord: 1.2 * s }, { x: L - 0.8 * s, chord: 0.8 * s }, h.fenestron ? 1.3 * s : 1.6 * s);
    d.surface({ x: 0.8 * L, y: top * 0.4, z: 0.1, chord: 0.06 * L }, { x: 0.81 * L, y: top * 0.4, z: 0.12 * L, chord: 0.045 * L });
    if (h.fenestron) {
      d.disc([L - 1.0 * s, finY + 0.6 * s, 0], "z", 0.45 * s, 0.12 * s, BLADE);
    } else if (h.tailRotorRadius) {
      // On the left of the fin.
      d.rotor([L - 0.6 * s, finY + 1.3 * s, -(W * 0.06 + 0.35)], "z", h.tailRotorRadius, h.tailBlades ?? 2, {
        mirror: false,
      });
    }
  }
}

// ---------------------------------------------------------------------------
// The catalogue: DCS type names to outlines.
// ---------------------------------------------------------------------------

interface AirframeType {
  /** Prefixes of the normalised DCS type name (see `normaliseAirframe`);
   *  the longest match wins, so "FA18E" beats "FA18". */
  match: readonly string[];
  label: string;
  family?: FamilyKey;
  /** Real length and span (m), when not the family's own aircraft. */
  dims?: [number, number];
  opts?: Options;
  /** The options draw this aircraft's own layout (a high wing and T-tail,
   *  an elliptical wing), so the caption should not call it borrowed. */
  ownShape?: boolean;
  rotorcraft?: Rotorcraft;
}

const TYPES: readonly AirframeType[] = [
  // Carrier aircraft
  { match: ["FA18", "F18"], label: "F/A-18C", family: "hornet" },
  { match: ["FA18E", "FA18F", "F18E", "F18F"], label: "F/A-18E/F", family: "hornet", dims: [18.31, 13.62] },
  { match: ["EA18G"], label: "EA-18G", family: "hornet", dims: [18.31, 13.62] },
  { match: ["F14"], label: "F-14", family: "tomcat" },
  { match: ["SU33"], label: "Su-33", family: "flanker", dims: [21.19, 14.7], opts: { canards: true } },
  { match: ["MIG29"], label: "MiG-29", family: "flanker", dims: [17.37, 11.36], opts: { stinger: false, finCantDeg: 6 } },
  { match: ["E2"], label: "E-2", family: "hawkeye" },
  { match: ["C2A"], label: "C-2A", family: "hawkeye", dims: [17.3, 24.6], opts: { radome: false } },
  { match: ["S3B"], label: "S-3B", family: "viking" },
  { match: ["T45"], label: "T-45", family: "trainer", dims: [11.99, 9.39], opts: { tipTanks: false, sweepDeg: 26 } },
  { match: ["A4E", "A4F", "A4K", "A4M"], label: "A-4E", family: "skyhawk" },
  { match: ["RAFALE"], label: "Rafale", family: "canardDelta", dims: [15.27, 10.9], opts: { canardX: 6.0 } },
  // Fighters and attack aircraft
  { match: ["F16"], label: "F-16C", family: "viper" },
  { match: ["JF17"], label: "JF-17", family: "viper", dims: [14.93, 9.45], opts: { intake: "side" } },
  { match: ["F15"], label: "F-15C", family: "eagle" },
  { match: ["F15E"], label: "F-15E", family: "eagle", opts: { twoSeat: true } },
  { match: ["SU27"], label: "Su-27", family: "flanker" },
  { match: ["J11"], label: "J-11A", family: "flanker", dims: [21.9, 14.7] },
  { match: ["SU30"], label: "Su-30", family: "flanker", dims: [21.935, 14.7] },
  { match: ["SU34"], label: "Su-34", family: "flanker", dims: [23.34, 14.7], opts: { canards: true } },
  { match: ["M2000", "MIRAGE2000"], label: "Mirage 2000", family: "mirage2000" },
  { match: ["MIRAGEF1"], label: "Mirage F1", family: "mirageF1" },
  { match: ["AJS37", "AJ37", "JA37", "SK37"], label: "AJS 37", family: "canardDelta" },
  { match: ["EF2000", "EUROFIGHTER", "TYPHOON"], label: "Eurofighter", family: "canardDelta", dims: [15.96, 10.95], opts: { canardX: 3.2 } },
  { match: ["JAS39", "GRIPEN"], label: "JAS 39", family: "canardDelta", dims: [14.1, 8.4], opts: { canardX: 4.6 } },
  { match: ["AV8B"], label: "AV-8B", family: "harrier" },
  { match: ["A10"], label: "A-10", family: "warthog" },
  { match: ["SU25"], label: "Su-25", family: "frogfoot" },
  { match: ["F4E"], label: "F-4E", family: "phantom" },
  { match: ["F5"], label: "F-5E", family: "tiger" },
  { match: ["MIG21"], label: "MiG-21", family: "fishbed" },
  { match: ["F86"], label: "F-86F", family: "sabre" },
  { match: ["MIG15"], label: "MiG-15", family: "sabre", dims: [10.08, 10.08], opts: { stabOnFin: true, sweepDeg: 37 } },
  // 48 deg in the F-86 drawing: squeezed to the MiG-19's shorter span it
  // becomes its ~55 deg.
  { match: ["MIG19"], label: "MiG-19", family: "sabre", dims: [12.54, 9.0], opts: { sweepDeg: 48 } },
  { match: ["TORNADO"], label: "Tornado", family: "swingWing" },
  { match: ["SU24"], label: "Su-24", family: "swingWing", dims: [22.53, 17.64] },
  { match: ["MIG23"], label: "MiG-23", family: "swingWing", dims: [16.7, 13.97] },
  { match: ["MIG27"], label: "MiG-27", family: "swingWing", dims: [17.08, 13.97] },
  { match: ["SU17"], label: "Su-17", family: "swingWing", dims: [19.02, 13.68] },
  { match: ["TU22"], label: "Tu-22M3", family: "swingWing", dims: [42.46, 34.28] },
  // Trainers
  { match: ["L39"], label: "L-39", family: "trainer" },
  { match: ["C101"], label: "C-101", family: "trainer", dims: [12.5, 10.6], opts: { tipTanks: false, sweepDeg: 3 } },
  { match: ["MB339"], label: "MB-339", family: "trainer", dims: [10.97, 11.22], opts: { sweepDeg: 10 } },
  { match: ["HAWK"], label: "Hawk", family: "trainer", dims: [11.98, 9.39], opts: { tipTanks: false, sweepDeg: 26 } },
  // Propeller aircraft
  { match: ["P51", "TF51"], label: "P-51D", family: "warbird", opts: { scoop: true } },
  { match: ["P47"], label: "P-47D", family: "warbird", dims: [11.0, 12.42] },
  { match: ["SPITFIRE"], label: "Spitfire", family: "warbird", dims: [9.47, 11.23], opts: { elliptic: true }, ownShape: true },
  { match: ["SPITFIRELFMKIXCW"], label: "Spitfire LF Mk IX CW", family: "warbird", dims: [9.47, 9.93], opts: { elliptic: true }, ownShape: true },
  { match: ["BF109"], label: "Bf 109", family: "warbird", dims: [8.95, 9.925] },
  { match: ["FW190A"], label: "Fw 190A", family: "warbird", dims: [9.0, 10.51] },
  { match: ["FW190D"], label: "Fw 190D", family: "warbird", dims: [10.2, 10.5] },
  { match: ["F4U"], label: "F4U", family: "warbird", dims: [10.26, 12.5] },
  { match: ["I16"], label: "I-16", family: "warbird", dims: [6.13, 9.0], opts: { blades: 2 } },
  { match: ["YAK52"], label: "Yak-52", family: "warbird", dims: [7.745, 9.3], opts: { blades: 2 } },
  { match: ["CHRISTENEAGLE"], label: "Christen Eagle II", family: "warbird", dims: [5.64, 6.07], opts: { biplane: true, blades: 2 }, ownShape: true },
  { match: ["MOSQUITO"], label: "Mosquito", family: "warbird", dims: [13.57, 16.52], opts: { twin: true }, ownShape: true },
  // Transports and tankers
  { match: ["C130", "KC130", "HERCULES"], label: "C-130", family: "hercules" },
  { match: ["C130J"], label: "C-130J", family: "hercules", opts: { blades: 6 } },
  { match: ["C130J30"], label: "C-130J-30", family: "hercules", dims: [34.69, 40.41], opts: { blades: 6 } },
  { match: ["KC135"], label: "KC-135", family: "jetTransport" },
  { match: ["E3"], label: "E-3", family: "jetTransport", dims: [46.61, 44.42], opts: { radome: true } },
  { match: ["IL76", "IL78"], label: "Il-76", family: "jetTransport", dims: [46.59, 50.5], opts: { highWing: true, tTail: true }, ownShape: true },
  { match: ["A50"], label: "A-50", family: "jetTransport", dims: [46.59, 50.5], opts: { highWing: true, tTail: true, radome: true }, ownShape: true },
  { match: ["C17"], label: "C-17", family: "jetTransport", dims: [53.04, 51.75], opts: { highWing: true, tTail: true }, ownShape: true },
  // Helicopters
  {
    match: ["UH1"],
    label: "UH-1H",
    rotorcraft: { length: 12.77, width: 2.4, height: 2.3, rotorRadius: 7.315, blades: 2, tailRotorRadius: 1.295, tailBlades: 2, skids: true },
  },
  {
    match: ["UH60", "SH60", "MH60", "HH60"],
    label: "UH-60",
    rotorcraft: { length: 15.26, width: 2.36, height: 2.3, rotorRadius: 8.18, blades: 4, tailRotorRadius: 1.68, tailBlades: 4 },
  },
  {
    match: ["MI8", "MI17"],
    label: "Mi-8",
    rotorcraft: { length: 18.17, width: 2.5, height: 2.7, rotorRadius: 10.645, blades: 5, tailRotorRadius: 1.955, tailBlades: 3 },
  },
  {
    match: ["MI24", "MI35"],
    label: "Mi-24",
    rotorcraft: {
      length: 17.5,
      width: 1.7,
      height: 2.2,
      rotorRadius: 8.65,
      blades: 5,
      tailRotorRadius: 1.95,
      tailBlades: 3,
      stubSpan: 6.66,
      tandemCockpit: true,
    },
  },
  {
    match: ["AH64"],
    label: "AH-64D",
    rotorcraft: {
      length: 15.06,
      width: 1.5,
      height: 1.9,
      rotorRadius: 7.315,
      blades: 4,
      tailRotorRadius: 1.4,
      tailBlades: 4,
      stubSpan: 5.23,
      tandemCockpit: true,
    },
  },
  {
    match: ["AH1"],
    label: "AH-1",
    rotorcraft: {
      length: 13.87,
      width: 1.0,
      height: 1.7,
      rotorRadius: 7.315,
      blades: 2,
      tailRotorRadius: 1.295,
      tailBlades: 2,
      stubSpan: 3.28,
      tandemCockpit: true,
      skids: true,
    },
  },
  {
    match: ["MI28"],
    label: "Mi-28",
    rotorcraft: {
      length: 17.01,
      width: 1.5,
      height: 2.0,
      rotorRadius: 8.6,
      blades: 5,
      tailRotorRadius: 1.9,
      tailBlades: 4,
      stubSpan: 4.88,
      tandemCockpit: true,
    },
  },
  {
    match: ["KA50", "KA52"],
    label: "Ka-50",
    rotorcraft: { length: 13.5, width: 1.4, height: 1.9, rotorRadius: 7.25, blades: 3, layout: "coaxial", stubSpan: 7.34 },
  },
  {
    match: ["KA27", "KA29"],
    label: "Ka-27",
    rotorcraft: { length: 11.3, width: 1.9, height: 2.4, rotorRadius: 7.95, blades: 3, layout: "coaxial" },
  },
  {
    match: ["SA342"],
    label: "SA 342",
    rotorcraft: { length: 9.53, width: 1.6, height: 1.8, rotorRadius: 5.25, blades: 3, fenestron: true, skids: true },
  },
  {
    match: ["OH58"],
    label: "OH-58D",
    rotorcraft: {
      length: 10.5,
      width: 1.3,
      height: 1.7,
      rotorRadius: 5.33,
      blades: 4,
      tailRotorRadius: 0.83,
      tailBlades: 2,
      skids: true,
      mastSight: true,
    },
  },
  {
    match: ["CH47"],
    label: "CH-47",
    rotorcraft: { length: 15.87, width: 3.78, height: 3.6, rotorRadius: 9.14, blades: 3, layout: "tandem" },
  },
  {
    match: ["CH53"],
    label: "CH-53E",
    rotorcraft: { length: 22.35, width: 2.7, height: 2.9, rotorRadius: 12.04, blades: 7, tailRotorRadius: 3.05, tailBlades: 4 },
  },
  {
    match: ["MI26"],
    label: "Mi-26",
    rotorcraft: { length: 33.7, width: 3.4, height: 3.8, rotorRadius: 16.0, blades: 8, tailRotorRadius: 3.8, tailBlades: 5 },
  },
];

// ---------------------------------------------------------------------------
// Lookup
// ---------------------------------------------------------------------------

export interface AirframeModel {
  /** What the outline is drawn as, e.g. "F/A-18C"; null for the generic jet. */
  label: string | null;
  /** The aircraft whose outline was stretched to this one's size, when it
   *  is not its own (a MiG-27 drawn as a Tornado); null otherwise. */
  shapeOf: string | null;
  rotorcraft: boolean;
  /** Public figures the outline is made to (m): length and span, or for a
   *  helicopter fuselage length and rotor diameter. */
  specM: { length: number; span: number };
  /** Real size of the model's unit length and its span as drawn (m). */
  lengthM: number;
  spanM: number;
  /** Triangles, unit length, nose +x, top +y, right +z. */
  positions: Float32Array;
  colors: Float32Array;
}

/** DCS type names vary in punctuation ("FA-18C_hornet", "F/A-18C"): compare
 *  on letters and digits only. */
export function normaliseAirframe(name: string): string {
  return name.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function findType(name: string | null | undefined): AirframeType | null {
  if (!name) return null;
  const key = normaliseAirframe(name);
  let best: AirframeType | null = null;
  let bestLength = 0;
  for (const type of TYPES) {
    for (const prefix of type.match) {
      if (prefix.length > bestLength && key.startsWith(prefix)) {
        best = type;
        bestLength = prefix.length;
      }
    }
  }
  return best;
}

function build(type: AirframeType | null): AirframeModel {
  const d = new Drawing();
  let stretch: Vec3 = [1, 1, 1];
  let spec: { length: number; span: number };
  let shapeOf: string | null = null;
  if (type?.rotorcraft) {
    drawRotorcraft(d, type.rotorcraft);
    spec = { length: type.rotorcraft.length, span: type.rotorcraft.rotorRadius * 2 };
  } else {
    const family: Family = FAMILIES[type?.family ?? "generic"];
    family.draw(d, type?.opts ?? {});
    const [length, span] = type?.dims ?? [family.base.lengthM, family.base.spanM];
    spec = { length, span };
    if (type?.dims) {
      const sx = length / family.base.lengthM;
      stretch = [sx, sx, span / family.base.spanM];
      if (!type.ownShape && family.base.label !== type.label) shapeOf = family.base.label;
    }
  }

  const n = d.positions.length;
  let minX = Infinity;
  let maxX = -Infinity;
  let minZ = Infinity;
  let maxZ = -Infinity;
  for (let i = 0; i < n; i += 3) {
    const x = d.positions[i] * stretch[0];
    const z = d.positions[i + 2] * stretch[2];
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minZ = Math.min(minZ, z);
    maxZ = Math.max(maxZ, z);
  }
  const lengthM = maxX - minX;
  const centre = (maxX + minX) / 2;
  const positions = new Float32Array(n);
  for (let i = 0; i < n; i += 3) {
    positions[i] = (d.positions[i] * stretch[0] - centre) / lengthM;
    positions[i + 1] = (d.positions[i + 1] * stretch[1]) / lengthM;
    positions[i + 2] = (d.positions[i + 2] * stretch[2]) / lengthM;
  }
  return {
    label: type?.label ?? null,
    shapeOf,
    rotorcraft: Boolean(type?.rotorcraft),
    specM: spec,
    lengthM,
    spanM: maxZ - minZ,
    positions,
    colors: new Float32Array(d.colors),
  };
}

const cache = new Map<AirframeType | null, AirframeModel>();

/** The model for a DCS type name; the generic jet when it is unknown. */
export function airframeModel(name: string | null | undefined): AirframeModel {
  const type = findType(name);
  let model = cache.get(type);
  if (!model) {
    model = build(type);
    cache.set(type, model);
  }
  return model;
}

/** Every model in the catalogue, plus the generic jet (for tests). */
export function allAirframeModels(): AirframeModel[] {
  return [null, ...TYPES].map((type) => {
    let model = cache.get(type);
    if (!model) {
      model = build(type);
      cache.set(type, model);
    }
    return model;
  });
}

function metres(value: number): string {
  return `${value.toFixed(1)} m`;
}

/** One sentence for the caption: what the models are drawn as. */
export function airframeCaption(name: string | null | undefined, model: AirframeModel): string {
  if (model.label === null) {
    return name
      ? `機影: 「${name}」の形は未登録のため、汎用のジェット機で描いています。`
      : "機影: 機種が記録に無いため、汎用のジェット機で描いています。";
  }
  const size = model.rotorcraft
    ? `ローター直径 ${metres(model.specM.span)}`
    : `全長 ${metres(model.specM.length)}・翼幅 ${metres(model.specM.span)}`;
  const borrowed = model.shapeOf ? `${model.shapeOf} の形を寸法に合わせて代用、` : "";
  return `機影: ${model.label} の概形（${borrowed}${size}）。`;
}
