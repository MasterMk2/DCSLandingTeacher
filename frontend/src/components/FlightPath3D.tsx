/**
 * 3D view of the flight path, with the aircraft drawn every few seconds so
 * the attitude -- the bank in the break, the pitch in the flare, the crab
 * on final -- can be read off the picture and turned around with the mouse.
 *
 * Drawn in the plan view's frame (the landing course; a carrier in the
 * ship's own frame), so the two views agree. Loaded lazily from the detail
 * page: three.js is large and nothing else needs it.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import {
  attitudeBasis,
  buildFlightPath,
  ghostTimes,
  gridStepM,
  lengthAtDistance,
  medianSpeed,
  modelLength,
  pointAt,
  sceneBounds,
  venueGeometry,
  type ApproachBeams,
  type CarrierModel,
  type FlightPath,
  type FlightPoint,
  type SceneBounds,
  type Strip,
  type Vec3,
  type VenueGeometry,
} from "../lib/flight3d";
import { airframeCaption, airframeModel, type AirframeModel } from "../lib/airframes";
import { legTimesFrom, type Leg } from "../lib/patternGeometry";
import { mToFt, msToKnots } from "../lib/format";
import { legLabelsFor } from "./PatternTrack";
import type { ApproachTrack } from "../types/api";

type ViewPreset = "oblique" | "top" | "side" | "behind";

const VIEW_LABELS: [ViewPreset, string][] = [
  ["oblique", "斜め"],
  ["top", "真上"],
  ["side", "横から"],
  ["behind", "進入側から"],
];

/** Where the camera sits relative to the target, per preset (unnormalised).
 *  Top keeps a sliver of -x so the landing course points up the screen, as
 *  in the plan view; side looks from the right so the approach runs left to
 *  right; behind looks up the final. */
const VIEW_DIRECTIONS: Record<ViewPreset, Vec3> = {
  oblique: [-0.9, 0.6, 0.75],
  top: [-0.001, 1, 0],
  side: [0, 0.12, 1],
  behind: [-1, 0.14, 0],
};

const GHOST_INTERVALS_S = [1, 2, 3, 5, 10];
const EXAGGERATIONS = [1, 2, 3, 5];
const MODEL_SCALES: [number, string][] = [
  [0.6, "小"],
  [1, "中"],
  [1.6, "大"],
];
const PLAY_SPEEDS = [1, 4, 10];

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

// ---------------------------------------------------------------------------
// Palette: the CSS tokens, so the 3D view matches the plan view's legs.
// ---------------------------------------------------------------------------

interface Palette {
  background: THREE.Color;
  ground: THREE.Color;
  grid: THREE.Color;
  centerline: THREE.Color;
  legs: Record<Leg, THREE.Color>;
  shadow: THREE.Color;
  ghost: THREE.Color;
  cursor: THREE.Color;
  runway: THREE.Color;
  sea: THREE.Color;
  hull: THREE.Color;
  deck: THREE.Color;
  island: THREE.Color;
  marking: THREE.Color;
  glideslope: THREE.Color;
  beam: THREE.Color;
  touchdown: THREE.Color;
}

function readPalette(): Palette {
  const style = getComputedStyle(document.documentElement);
  const css = (name: string, fallback: string) =>
    new THREE.Color(style.getPropertyValue(name).trim() || fallback);
  const dim = css("--text-dim", "#8b8a86");
  return {
    background: css("--bg-scope", "#0b0c0e"),
    ground: new THREE.Color("#131518"),
    grid: css("--border", "#272a2f"),
    centerline: css("--accent", "#c2a76c"),
    legs: {
      prior: dim,
      entry: css("--leg-entry", "#8faa85"),
      break: css("--leg-break", "#c08a6e"),
      downwind: css("--leg-downwind", "#8fb2c9"),
      base: css("--leg-base", "#a08fae"),
      final: css("--accent", "#c2a76c"),
      rollout: dim,
    },
    shadow: dim,
    ghost: new THREE.Color("#d4d1c9"),
    cursor: new THREE.Color("#ffb347"),
    runway: css("--warn", "#c39a4e"),
    sea: new THREE.Color("#0f171d"),
    hull: new THREE.Color("#8d939c"),
    deck: new THREE.Color("#50545b"),
    island: new THREE.Color("#b3b8bf"),
    marking: new THREE.Color("#e8e6e1"),
    glideslope: css("--info", "#7f9bb5"),
    beam: new THREE.Color("#9fd0f0"),
    touchdown: css("--danger", "#cd6a61"),
  };
}

// ---------------------------------------------------------------------------
// The aircraft model: the airframe's outline (lib/airframes), unit length,
// nose +x, top +y, right +z.
// ---------------------------------------------------------------------------

function aircraftGeometry(model: AirframeModel): THREE.BufferGeometry {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(model.positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(model.colors, 3));
  geometry.computeVertexNormals();
  return geometry;
}

const tmpMatrix = new THREE.Matrix4();
const tmpQuat = new THREE.Quaternion();
const tmpScale = new THREE.Vector3();
const tmpPos = new THREE.Vector3();
const vec = (v: Vec3) => new THREE.Vector3(v[0], v[1], v[2]);

function poseMatrix(p: FlightPoint, exaggeration: number, length: number, out: THREE.Matrix4) {
  const { nose, up, right } = attitudeBasis(p.heading, p.pitch, p.roll);
  tmpMatrix.makeBasis(vec(nose), vec(up), vec(right));
  tmpQuat.setFromRotationMatrix(tmpMatrix);
  tmpPos.set(p.x, p.y * exaggeration, p.z);
  tmpScale.set(length, length, length);
  return out.compose(tmpPos, tmpQuat, tmpScale);
}

// ---------------------------------------------------------------------------
// The scene: owns every three.js object, so React only passes numbers in.
// ---------------------------------------------------------------------------

interface ContentOptions {
  ghostIntervalS: number;
  exaggeration: number;
  /** Upper bound on the model length (m); see `lengthAtDistance`. */
  modelLength: number;
  modelScale: number;
  touchdownTime: number | null;
  showBeams: boolean;
}

class FlightScene {
  private readonly renderer: THREE.WebGLRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(40, 1, 1, 100_000);
  private readonly controls: OrbitControls;
  private readonly aircraft: THREE.BufferGeometry;
  /** The airframe's real length: the models' size up against a deck. */
  private readonly realLength: number;
  private readonly resizeObserver: ResizeObserver;
  private content = new THREE.Group();
  private contentDisposables: { dispose(): void }[] = [];
  private lineMaterials: LineMaterial[] = [];
  private readonly cursorMesh: THREE.Mesh;
  private readonly cursorDrop: THREE.Line;
  private framed = false;
  private bounds: SceneBounds | null = null;
  private exaggeration = 1;
  private groundY = 0;
  private approachBack: [number, number] = [-1, 0];
  private approachEnd: Vec3 = [0, 0, 0];
  private approachSlopeDeg: number | null = null;
  private finalDistance = 1500;
  private ghostMesh: THREE.InstancedMesh | null = null;
  private ghostPoints: FlightPoint[] = [];
  private touchdownRing: THREE.Mesh | null = null;
  private cursorPoint: FlightPoint | null = null;
  private maxModelLength: number;
  private modelScale = 1;
  private appliedLength = -1;
  private dirty = true;
  private animationFrame = 0;
  private width = 1;
  private height = 1;

  constructor(
    private readonly host: HTMLElement,
    private readonly palette: Palette,
    model: AirframeModel,
  ) {
    this.aircraft = aircraftGeometry(model);
    this.realLength = model.lengthM;
    this.maxModelLength = model.lengthM;
    // Throws when WebGL is unavailable; the component shows a message.
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      // Metres from a deck to a 20 km circuit: a linear depth buffer
      // z-fights the runway into the ground at that range.
      logarithmicDepthBuffer: true,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.domElement.className = "flight3d-canvas";
    host.appendChild(this.renderer.domElement);

    this.scene.background = palette.background;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3d42, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-0.4, 1, 0.3);
    this.scene.add(sun);
    this.scene.add(this.content);

    this.cursorMesh = new THREE.Mesh(
      this.aircraft,
      new THREE.MeshStandardMaterial({
        color: palette.cursor,
        vertexColors: true,
        side: THREE.DoubleSide,
        roughness: 0.6,
        emissive: palette.cursor,
        emissiveIntensity: 0.25,
      }),
    );
    this.cursorMesh.matrixAutoUpdate = false;
    this.cursorDrop = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
      new THREE.LineBasicMaterial({ color: palette.cursor, transparent: true, opacity: 0.7 }),
    );
    this.cursorMesh.visible = false;
    this.cursorDrop.visible = false;
    this.scene.add(this.cursorMesh, this.cursorDrop);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.screenSpacePanning = true;
    // Zoom toward what the mouse is over: the way to get close to one
    // aircraft in a circuit several miles across.
    this.controls.zoomToCursor = true;
    // Never below the ground: the view is for reading the flight, not the
    // underside of the grid.
    this.controls.maxPolarAngle = Math.PI / 2 - 0.01;
    this.controls.addEventListener("change", () => {
      this.dirty = true;
    });

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.resize();

    const loop = () => {
      this.animationFrame = requestAnimationFrame(loop);
      const moved = this.controls.update();
      if (moved || this.dirty) {
        this.dirty = false;
        this.applyModelLength();
        this.renderer.render(this.scene, this.camera);
      }
    };
    loop();
  }

  private resize() {
    const width = Math.max(1, this.host.clientWidth);
    const height = Math.max(1, this.host.clientHeight);
    this.width = width;
    this.height = height;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    for (const m of this.lineMaterials) m.resolution.set(width, height);
    this.dirty = true;
  }

  private track<T extends { dispose(): void }>(item: T): T {
    this.contentDisposables.push(item);
    return item;
  }

  private strip(strip: Strip, y: number, color: THREE.Color, opacity = 1): THREE.Mesh {
    const dx = strip.to[0] - strip.from[0];
    const dz = strip.to[2] - strip.from[2];
    const geometry = this.track(new THREE.PlaneGeometry(Math.hypot(dx, dz), strip.width));
    geometry.rotateX(-Math.PI / 2);
    const mesh = new THREE.Mesh(
      geometry,
      this.track(
        new THREE.MeshBasicMaterial({
          color,
          side: THREE.DoubleSide,
          transparent: opacity < 1,
          opacity,
        }),
      ),
    );
    mesh.position.set((strip.from[0] + strip.to[0]) / 2, y, (strip.from[2] + strip.to[2]) / 2);
    mesh.rotation.y = -Math.atan2(dz, dx);
    return mesh;
  }

  /** The carrier, built in its own frame and placed by the model's transform. */
  private carrier(model: CarrierModel): THREE.Group {
    const palette = this.palette;
    const ship = new THREE.Group();
    ship.position.set(...model.position);
    ship.rotation.y = model.rotationY;
    const hull = this.track(new THREE.MeshStandardMaterial({ color: palette.hull, roughness: 0.85 }));
    const deck = this.track(new THREE.MeshStandardMaterial({ color: palette.deck, roughness: 0.95 }));
    // An outline in the ship frame ([x, z]), extruded between two heights:
    // top and bottom faces take the deck colour, the sides the hull's.
    const slab = (outline: [number, number][], bottom: number, top: number) => {
      const shape = new THREE.Shape(outline.map(([x, z]) => new THREE.Vector2(x, -z)));
      const geometry = this.track(
        new THREE.ExtrudeGeometry(shape, { depth: top - bottom, bevelEnabled: false }),
      );
      geometry.rotateX(-Math.PI / 2);
      geometry.translate(0, bottom, 0);
      return new THREE.Mesh(geometry, [deck, hull]);
    };
    ship.add(slab(model.waterline, -model.deckHeight, -2.5));
    ship.add(slab(model.flightDeck, -2.5, 0));
    ship.add(slab(model.angledDeck, -5, 0));

    const { x, z, length, width, height } = model.island;
    const island = new THREE.Mesh(
      this.track(new THREE.BoxGeometry(length, height, width)),
      this.track(new THREE.MeshStandardMaterial({ color: palette.island, roughness: 0.8 })),
    );
    island.position.set(x, height / 2, z);
    ship.add(island);

    // Landing-area edges and a dashed centreline, just above the deck.
    const area = model.landingArea;
    const dx = area.to[0] - area.from[0];
    const dz = area.to[1] - area.from[1];
    const norm = Math.hypot(dx, dz) || 1;
    const [px, pz] = [(-dz / norm) * area.halfWidth, (dx / norm) * area.halfWidth];
    const edges = new THREE.LineSegments(
      this.track(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(area.from[0] + px, 0.3, area.from[1] + pz),
          new THREE.Vector3(area.to[0] + px, 0.3, area.to[1] + pz),
          new THREE.Vector3(area.from[0] - px, 0.3, area.from[1] - pz),
          new THREE.Vector3(area.to[0] - px, 0.3, area.to[1] - pz),
        ]),
      ),
      this.track(new THREE.LineBasicMaterial({ color: palette.runway })),
    );
    ship.add(edges);
    const centreline = new THREE.Line(
      this.track(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(area.from[0], 0.3, area.from[1]),
          new THREE.Vector3(area.to[0], 0.3, area.to[1]),
        ]),
      ),
      this.track(new THREE.LineDashedMaterial({ color: palette.marking, dashSize: 8, gapSize: 6 })),
    );
    centreline.computeLineDistances();
    ship.add(centreline);
    return ship;
  }

  /** The localizer / glide-path cross: two translucent planes, brightest on
   *  the glide path and fading to nothing at their edges and far end. */
  private beams(beams: ApproachBeams, ex: number): THREE.Group {
    const color = this.palette.beam;
    const peak = 0.45;
    // The edges keep a trace of the centre's brightness, so the extent of
    // each arm (where the needle pegs) stays visible.
    const edge = 0.25;
    const group = new THREE.Group();
    for (const rows of [beams.glideslope, beams.localizer]) {
      const positions: number[] = [];
      const colors: number[] = [];
      const push = (p: Vec3, alpha: number) => {
        positions.push(p[0], p[1] * ex, p[2]);
        colors.push(color.r, color.g, color.b, alpha);
      };
      for (let i = 0; i + 1 < rows.length; i++) {
        const a0 = beams.strength[i] * peak;
        const a1 = beams.strength[i + 1] * peak;
        const [l0, c0, r0] = rows[i];
        const [l1, c1, r1] = rows[i + 1];
        // Two quads per band: edge -> centre on each side of the glide path.
        for (const [p, q] of [
          [l0, l1],
          [r0, r1],
        ] as [Vec3, Vec3][]) {
          push(p, a0 * edge);
          push(c0, a0);
          push(c1, a1);
          push(p, a0 * edge);
          push(c1, a1);
          push(q, a1 * edge);
        }
      }
      const geometry = this.track(new THREE.BufferGeometry());
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      // Four components: three.js turns on per-vertex alpha for these.
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 4));
      const material = this.track(
        new THREE.MeshBasicMaterial({
          vertexColors: true,
          transparent: true,
          depthWrite: false,
          side: THREE.DoubleSide,
          blending: THREE.AdditiveBlending,
        }),
      );
      group.add(new THREE.Mesh(geometry, material));
    }

    // The cross-shaped gates: a horizontal and a vertical bar each.
    const positions: number[] = [];
    const colors: number[] = [];
    const quad = (
      centre: Vec3,
      along: Vec3,
      across: Vec3,
      halfAlong: number,
      halfAcross: number,
      alpha: number,
    ) => {
      const corner = (a: number, b: number) => {
        positions.push(
          centre[0] + along[0] * a + across[0] * b,
          (centre[1] + along[1] * a + across[1] * b) * ex,
          centre[2] + along[2] * a + across[2] * b,
        );
        colors.push(color.r, color.g, color.b, alpha);
      };
      corner(-halfAlong, -halfAcross);
      corner(halfAlong, -halfAcross);
      corner(halfAlong, halfAcross);
      corner(-halfAlong, -halfAcross);
      corner(halfAlong, halfAcross);
      corner(-halfAlong, halfAcross);
    };
    for (const gate of beams.gates) {
      // Bar thickness: a slice of the glide path's half-height, never so
      // thin it vanishes.
      const thickness = Math.max(gate.halfHeight * 0.12, 1.5);
      const alpha = 0.55 * gate.strength;
      quad(gate.centre, gate.right, gate.up, gate.halfWidth, thickness, alpha);
      quad(gate.centre, gate.up, gate.right, gate.halfHeight, thickness, alpha);
    }
    if (positions.length > 0) {
      const geometry = this.track(new THREE.BufferGeometry());
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 4));
      group.add(
        new THREE.Mesh(
          geometry,
          this.track(
            new THREE.MeshBasicMaterial({
              vertexColors: true,
              transparent: true,
              depthWrite: false,
              side: THREE.DoubleSide,
              blending: THREE.AdditiveBlending,
            }),
          ),
        ),
      );
    }
    return group;
  }

  setContent(path: FlightPath, venue: VenueGeometry, opts: ContentOptions) {
    this.scene.remove(this.content);
    for (const d of this.contentDisposables) d.dispose();
    this.contentDisposables = [];
    this.lineMaterials = [];
    const group = new THREE.Group();
    this.content = group;
    this.scene.add(group);

    const { points } = path;
    const ex = opts.exaggeration;
    const bounds = sceneBounds(points);
    const previousExaggeration = this.exaggeration;
    this.bounds = bounds;
    this.exaggeration = ex;
    const palette = this.palette;

    // Ground: a plane a shade lighter than the sky and a grid in round
    // nautical miles, centred on the path. For a carrier it is the sea, a
    // deck-height below the flight deck the heights are measured from.
    const groundY = venue.groundY;
    this.groundY = groundY;
    this.approachBack = venue.approachBack;
    this.approachEnd = venue.approachEnd;
    // The pilot's-eye preset sits just beyond the far end of the glide path
    // (which reaches back to the start of the track), so the whole beam and
    // the whole approach are ahead of it rather than around it.
    const [bx, bz] = venue.approachBack;
    const [x0, , z0] = venue.approachEnd;
    const glideLength = venue.glideslope
      ? Math.hypot(venue.glideslope.to[0] - x0, venue.glideslope.to[2] - z0)
      : Math.max(0, ...points.map((p) => (p.x - x0) * bx + (p.z - z0) * bz));
    this.finalDistance = Math.max(glideLength * 1.15, 1500);
    this.approachSlopeDeg = venue.glideslope
      ? (Math.atan2(
          venue.glideslope.to[1] - venue.glideslope.from[1],
          Math.hypot(
            venue.glideslope.to[0] - venue.glideslope.from[0],
            venue.glideslope.to[2] - venue.glideslope.from[2],
          ),
        ) *
          180) /
        Math.PI
      : null;
    const step = gridStepM(bounds.span);
    const cells = Math.ceil((bounds.span * 1.5) / step / 2) * 2;
    const size = cells * step;
    const cx = Math.round(bounds.center[0] / step) * step;
    const cz = Math.round(bounds.center[2] / step) * step;
    const ground = new THREE.Mesh(
      this.track(new THREE.PlaneGeometry(size * 4, size * 4).rotateX(-Math.PI / 2)),
      this.track(
        new THREE.MeshBasicMaterial({ color: venue.carrier ? palette.sea : palette.ground }),
      ),
    );
    ground.position.set(cx, groundY - 1, cz);
    group.add(ground);
    // Both grid colours alike: GridHelper's own centre lines fall wherever
    // the grid happens to be centred and read as a course line.
    const grid = new THREE.GridHelper(size, cells, palette.grid, palette.grid);
    this.track(grid.geometry);
    this.track(grid.material as THREE.Material);
    grid.position.set(cx, groundY, cz);
    group.add(grid);

    // Extended landing course on the ground, dashed, like the plan view's
    // centreline: the line the final should have been flown down.
    if (venue.course) {
      const { from, to } = venue.course;
      const centerline = new THREE.Line(
        this.track(
          new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(from[0], groundY + 0.8, from[2]),
            new THREE.Vector3(to[0], groundY + 0.8, to[2]),
          ]),
        ),
        this.track(
          new THREE.LineDashedMaterial({
            color: palette.centerline,
            dashSize: bounds.span / 90,
            gapSize: bounds.span / 110,
            transparent: true,
            opacity: 0.55,
          }),
        ),
      );
      centerline.computeLineDistances();
      group.add(centerline);
    }

    // Runway, or the ship with its angled deck.
    if (venue.runway) group.add(this.strip(venue.runway, 0.3, palette.runway, 0.85));
    if (venue.carrier) {
      group.add(this.carrier(venue.carrier));
    } else if (venue.landingArea) {
      group.add(this.strip(venue.landingArea, 0.4, palette.runway, 0.9));
    }

    // The localizer / glide-path cross, translucent, under everything else.
    if (opts.showBeams && venue.beams) group.add(this.beams(venue.beams, ex));

    // Ideal glide path, dashed.
    if (venue.glideslope) {
      const { from, to } = venue.glideslope;
      const line = new THREE.Line(
        this.track(
          new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(from[0], from[1] * ex, from[2]),
            new THREE.Vector3(to[0], to[1] * ex, to[2]),
          ]),
        ),
        this.track(
          new THREE.LineDashedMaterial({
            color: palette.glideslope,
            dashSize: bounds.span / 120,
            gapSize: bounds.span / 160,
            transparent: true,
            opacity: 0.8,
          }),
        ),
      );
      line.computeLineDistances();
      group.add(line);
    }

    // The path, coloured by leg like the plan view.
    const positions: number[] = [];
    const colors: number[] = [];
    for (const p of points) {
      positions.push(p.x, p.y * ex, p.z);
      const c = palette.legs[p.leg];
      colors.push(c.r, c.g, c.b);
    }
    const pathGeometry = this.track(new LineGeometry());
    pathGeometry.setPositions(positions);
    pathGeometry.setColors(colors);
    const pathMaterial = this.track(
      new LineMaterial({ vertexColors: true, linewidth: 2.5, worldUnits: false }),
    );
    pathMaterial.resolution.set(this.width, this.height);
    this.lineMaterials.push(pathMaterial);
    group.add(new Line2(pathGeometry, pathMaterial));

    // Its shadow on the ground: where it was over the map, whatever the height.
    const shadow = new THREE.Line(
      this.track(
        new THREE.BufferGeometry().setFromPoints(
          points.map((p) => new THREE.Vector3(p.x, groundY + 0.5, p.z)),
        ),
      ),
      this.track(
        new THREE.LineBasicMaterial({ color: palette.shadow, transparent: true, opacity: 0.45 }),
      ),
    );
    group.add(shadow);

    // Ghost aircraft every `ghostIntervalS`, each dropped to the ground.
    const first = points[0].time;
    const last = points[points.length - 1].time;
    const ghosts = ghostTimes(first, last, opts.touchdownTime, opts.ghostIntervalS)
      .map((t) => pointAt(points, t, Math.min(1, opts.ghostIntervalS / 2)))
      .filter((p): p is FlightPoint => p !== null);
    this.ghostPoints = ghosts;
    this.ghostMesh = null;
    if (ghosts.length > 0) {
      const mesh = new THREE.InstancedMesh(
        this.aircraft,
        this.track(
          new THREE.MeshStandardMaterial({
            color: palette.ghost,
            vertexColors: true,
            side: THREE.DoubleSide,
            roughness: 0.7,
            metalness: 0.05,
          }),
        ),
        ghosts.length,
      );
      // Posed (and sized) in `applyModelLength`, on every camera move.
      this.ghostMesh = mesh;
      this.contentDisposables.push({ dispose: () => mesh.dispose() });
      group.add(mesh);

      const drops: THREE.Vector3[] = [];
      for (const p of ghosts) {
        drops.push(new THREE.Vector3(p.x, groundY, p.z), new THREE.Vector3(p.x, p.y * ex, p.z));
      }
      group.add(
        new THREE.LineSegments(
          this.track(new THREE.BufferGeometry().setFromPoints(drops)),
          this.track(
            new THREE.LineBasicMaterial({ color: palette.shadow, transparent: true, opacity: 0.3 }),
          ),
        ),
      );
    }

    // Touchdown: a ring on the surface under the touchdown sample.
    // Unit radius, scaled with the models in `applyModelLength`.
    const td = opts.touchdownTime !== null ? pointAt(points, opts.touchdownTime, 1) : null;
    this.touchdownRing = null;
    if (td) {
      const ring = new THREE.Mesh(
        this.track(new THREE.RingGeometry(1, 1.35, 40).rotateX(-Math.PI / 2)),
        this.track(new THREE.MeshBasicMaterial({ color: palette.touchdown, side: THREE.DoubleSide })),
      );
      ring.position.set(td.x, 0.6, td.z);
      this.touchdownRing = ring;
      group.add(ring);
    }
    this.maxModelLength = opts.modelLength;
    this.modelScale = opts.modelScale;
    this.appliedLength = -1;

    this.camera.near = Math.max(0.5, bounds.span / 20_000);
    this.camera.far = bounds.span * 40;
    this.camera.updateProjectionMatrix();
    this.controls.minDistance = Math.max(10, bounds.span * 0.01);
    this.controls.maxDistance = bounds.span * 8;

    if (!this.framed) {
      this.framed = true;
      this.view("oblique");
    } else if (ex !== previousExaggeration) {
      // Stretching the heights moves the path out of whatever the user was
      // looking at: frame it again, from the direction they were looking.
      this.frame(this.camera.position.clone().sub(this.controls.target));
    }
    this.dirty = true;
  }

  /**
   * Size every model for the current camera distance (`lengthAtDistance`):
   * readable when zoomed out, real size up against the deck. Only re-poses
   * when the length has actually changed by more than a couple of percent.
   */
  private applyModelLength(force = false) {
    const distance = this.camera.position.distanceTo(this.controls.target);
    const length = lengthAtDistance(
      distance,
      this.camera.fov,
      this.maxModelLength,
      this.modelScale,
      this.realLength,
    );
    if (!force && Math.abs(length - this.appliedLength) <= this.appliedLength * 0.02) return;
    this.appliedLength = length;
    const ex = this.exaggeration;
    const mesh = this.ghostMesh;
    if (mesh) {
      const matrix = new THREE.Matrix4();
      this.ghostPoints.forEach((p, i) => mesh.setMatrixAt(i, poseMatrix(p, ex, length, matrix)));
      mesh.instanceMatrix.needsUpdate = true;
      mesh.computeBoundingSphere();
    }
    this.touchdownRing?.scale.setScalar(length * 0.45);
    if (this.cursorPoint) {
      poseMatrix(this.cursorPoint, ex, length * 1.15, this.cursorMesh.matrix);
      this.cursorMesh.matrixWorldNeedsUpdate = true;
    }
  }

  setCursor(point: FlightPoint | null, exaggeration: number) {
    this.cursorPoint = point;
    if (!point) {
      this.cursorMesh.visible = false;
      this.cursorDrop.visible = false;
    } else {
      this.applyModelLength(true);
      const drop = this.cursorDrop.geometry.getAttribute("position") as THREE.BufferAttribute;
      drop.setXYZ(0, point.x, this.groundY, point.z);
      drop.setXYZ(1, point.x, point.y * exaggeration, point.z);
      drop.needsUpdate = true;
      this.cursorDrop.geometry.computeBoundingSphere();
      this.cursorMesh.visible = true;
      this.cursorDrop.visible = true;
    }
    this.dirty = true;
  }

  /** Frame the whole path from one of the presets. "From the approach" is
   *  a pilot's view: from out on the extended final, looking at the glide
   *  path's end -- up the angled deck on a carrier, 9 deg off the ship's
   *  axis the other presets use -- so the beams are seen end-on. */
  view(preset: ViewPreset) {
    if (preset === "behind") {
      // A pilot's seat: out on the extended final, where the final leg
      // starts, just above the (possibly stretched) glide path, looking at
      // its end. Both beam planes are then seen nearly edge-on and the
      // gates stack into the cross a pilot flies into.
      const [bx, bz] = this.approachBack;
      const [x, y, z] = this.approachEnd;
      const rise =
        this.approachSlopeDeg !== null
          ? Math.tan((this.approachSlopeDeg * Math.PI) / 180) * this.exaggeration +
            Math.tan((0.3 * Math.PI) / 180)
          : VIEW_DIRECTIONS.behind[1];
      const target = new THREE.Vector3(x, y * this.exaggeration, z);
      const distance = this.finalDistance;
      this.controls.target.copy(target);
      this.camera.position.set(
        target.x + bx * distance,
        target.y + rise * distance,
        target.z + bz * distance,
      );
      this.camera.lookAt(target);
      this.controls.update();
      this.dirty = true;
    } else {
      this.frame(vec(VIEW_DIRECTIONS[preset]));
    }
  }

  /** Look from `direction` (target -> camera) at `center` (default: the
   *  middle of the path): the camera backs off just far enough for every
   *  corner of the bounds to be in the frustum. */
  private frame(direction: THREE.Vector3, center?: THREE.Vector3) {
    const bounds = this.bounds;
    if (!bounds || direction.lengthSq() === 0) return;
    const ex = this.exaggeration;
    const min = new THREE.Vector3(bounds.min[0], bounds.min[1] * ex, bounds.min[2]);
    const max = new THREE.Vector3(bounds.max[0], bounds.max[1] * ex, bounds.max[2]);
    center = center ?? min.clone().add(max).multiplyScalar(0.5);
    const back = direction.clone().normalize();
    const right = new THREE.Vector3().crossVectors(back.clone().negate(), THREE.Object3D.DEFAULT_UP);
    // Straight down (orbited onto the pole) has no horizon to take "right"
    // from; the top preset's screen-right is +z.
    if (right.lengthSq() < 1e-12) right.set(0, 0, 1);
    right.normalize();
    const up = new THREE.Vector3().crossVectors(right, back.clone().negate());
    const tanV = Math.tan(((this.camera.fov / 2) * Math.PI) / 180);
    const tanH = tanV * this.camera.aspect;
    let distance = 50;
    for (const x of [min.x, max.x]) {
      for (const y of [min.y, max.y]) {
        for (const z of [min.z, max.z]) {
          const v = new THREE.Vector3(x, y, z).sub(center);
          const depth = v.dot(back);
          distance = Math.max(
            distance,
            depth + Math.abs(v.dot(right)) / tanH,
            depth + Math.abs(v.dot(up)) / tanV,
          );
        }
      }
    }
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(back, distance * 1.08);
    this.camera.lookAt(center);
    this.controls.update();
    this.dirty = true;
  }

  dispose() {
    cancelAnimationFrame(this.animationFrame);
    this.resizeObserver.disconnect();
    this.controls.dispose();
    for (const d of this.contentDisposables) d.dispose();
    this.aircraft.dispose();
    (this.cursorMesh.material as THREE.Material).dispose();
    this.cursorDrop.geometry.dispose();
    (this.cursorDrop.material as THREE.Material).dispose();
    // Browsers cap live WebGL contexts (~16); paging through landings would
    // otherwise start evicting the oldest ones with a console warning.
    this.renderer.forceContextLoss();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

// ---------------------------------------------------------------------------
// The component
// ---------------------------------------------------------------------------

export interface FlightPath3DProps {
  track: ApproachTrack;
  metrics?: Record<string, unknown> | null;
  /** DCS type name of the aircraft, which picks the model drawn. */
  airframe?: string | null;
}

function signed(value: number, digits: number): string {
  return `${value > 0 ? "+" : value < 0 ? "−" : "±"}${Math.abs(value).toFixed(digits)}`;
}

/** Which angles were estimated, and for how much of the track: a handful
 *  of corrupt samples must not read as "the attitude is made up". */
function estimatedNote(path: FlightPath): string | null {
  const total = path.points.length;
  const count = (n: number) => (n === total ? "全点" : `${n} 点`);
  const parts: string[] = [];
  const { heading, roll, pitch } = path.estimatedCount;
  if (heading > 0) parts.push(`機首方位 ${count(heading)}（進行方向から）`);
  if (roll > 0) parts.push(`バンク ${count(roll)}（旋回率から）`);
  if (pitch > 0) parts.push(`ピッチ ${count(pitch)}（経路角と迎角から）`);
  if (parts.length === 0) return null;
  return `記録が無いか異常値だった姿勢は軌跡から推定しています: ${parts.join("、")}。`;
}

export default function FlightPath3D({ track, metrics, airframe }: FlightPath3DProps) {
  const legTimes = useMemo(
    () => legTimesFrom(metrics, track.touchdown_time),
    [metrics, track.touchdown_time],
  );
  const path = useMemo(() => buildFlightPath(track, legTimes), [track, legTimes]);
  const bounds = useMemo(() => (path ? sceneBounds(path.points) : null), [path]);
  const model = useMemo(() => airframeModel(airframe), [airframe]);

  const [ghostIntervalS, setGhostIntervalS] = useState(2);
  const [exaggeration, setExaggeration] = useState(1);
  const [modelScale, setModelScale] = useState(1);
  const [showBeams, setShowBeams] = useState(true);
  const [playSpeed, setPlaySpeed] = useState(4);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState<number | null>(null);
  const [unsupported, setUnsupported] = useState(false);

  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<FlightScene | null>(null);

  const touchdownTime = num(track.touchdown_time);
  const start = path ? path.points[0].time : 0;
  const end = path ? path.points[path.points.length - 1].time : 0;
  const initialTime =
    touchdownTime !== null && touchdownTime >= start && touchdownTime <= end ? touchdownTime : end;
  const current = time ?? initialTime;
  const timeRef = useRef(current);
  timeRef.current = current;
  const speed = useMemo(() => (path ? medianSpeed(path.points) : 0), [path]);
  const length = bounds
    ? modelLength(bounds.span, speed, ghostIntervalS, modelScale, model.lengthM)
    : 0;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !path) return;
    let scene: FlightScene;
    try {
      scene = new FlightScene(host, readPalette(), model);
    } catch {
      setUnsupported(true);
      return;
    }
    sceneRef.current = scene;
    return () => {
      scene.dispose();
      sceneRef.current = null;
    };
  }, [path, model]);

  const venue = useMemo(
    () => (path ? venueGeometry(track, path.shipFrame, path.points) : null),
    [track, path],
  );

  useEffect(() => {
    if (!path || !venue) return;
    sceneRef.current?.setContent(path, venue, {
      ghostIntervalS,
      exaggeration,
      modelLength: length,
      modelScale,
      touchdownTime,
      showBeams,
    });
  }, [
    path,
    venue,
    model,
    ghostIntervalS,
    exaggeration,
    length,
    modelScale,
    touchdownTime,
    showBeams,
  ]);

  const cursor = path ? pointAt(path.points, current, 1) : null;
  useEffect(() => {
    sceneRef.current?.setCursor(cursor, exaggeration);
  }, [cursor, exaggeration, length, path, model]);

  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    let last = performance.now();
    const step = (now: number) => {
      const next = Math.min(timeRef.current + ((now - last) / 1000) * playSpeed, end);
      last = now;
      timeRef.current = next;
      setTime(next);
      if (next >= end) {
        setPlaying(false);
        return;
      }
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [playing, playSpeed, end]);

  if (!path || !bounds) {
    return <p className="empty-message">3D で描ける軌跡がありません（位置と高さのあるサンプルが不足）。</p>;
  }
  if (unsupported) {
    return <p className="empty-message">このブラウザでは 3D 表示（WebGL）を使えません。</p>;
  }

  const legLabels = legLabelsFor(path.shipFrame, metrics?.["pattern_entry"] === "turn");
  const legsShown = Array.from(new Set(path.points.map((p) => p.leg)));
  const note = estimatedNote(path);
  const togglePlay = () => {
    if (!playing && current >= end) setTime(start);
    setPlaying(!playing);
  };

  return (
    <figure className="flight3d" aria-label="3D 飛行軌跡">
      <div className="flight3d-toolbar">
        <div className="flight3d-views" role="group" aria-label="視点">
          {VIEW_LABELS.map(([preset, label]) => (
            <button
              key={preset}
              type="button"
              className="btn"
              onClick={() => sceneRef.current?.view(preset)}
            >
              {label}
            </button>
          ))}
        </div>
        <label>
          残像の間隔
          <select
            value={ghostIntervalS}
            onChange={(e) => setGhostIntervalS(Number(e.target.value))}
          >
            {GHOST_INTERVALS_S.map((s) => (
              <option key={s} value={s}>
                {s} 秒
              </option>
            ))}
          </select>
        </label>
        <label>
          機影サイズ
          <select value={modelScale} onChange={(e) => setModelScale(Number(e.target.value))}>
            {MODEL_SCALES.map(([s, label]) => (
              <option key={s} value={s}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          高さの強調
          <select value={exaggeration} onChange={(e) => setExaggeration(Number(e.target.value))}>
            {EXAGGERATIONS.map((x) => (
              <option key={x} value={x}>
                {x === 1 ? "なし（等倍）" : `${x} 倍`}
              </option>
            ))}
          </select>
        </label>
        {venue?.beams && (
          <label className="flight3d-check">
            <input
              type="checkbox"
              checked={showBeams}
              onChange={(e) => setShowBeams(e.target.checked)}
            />
            進入ビーム
          </label>
        )}
      </div>

      <div className="flight3d-stage">
        {/* three.js owns this element's children; React never renders into it. */}
        <div className="flight3d-canvas-host" ref={hostRef} />
        {cursor && (
          <dl className="flight3d-readout" aria-live="off">
            <div>
              <dt>時刻</dt>
              <dd>
                {touchdownTime !== null
                  ? `接地 ${signed(cursor.time - touchdownTime, 1)} 秒`
                  : `${cursor.time.toFixed(1)} 秒`}
              </dd>
            </div>
            <div>
              <dt>区間</dt>
              <dd>{legLabels[cursor.leg]}</dd>
            </div>
            <div>
              <dt>高さ</dt>
              <dd>{mToFt(cursor.y).toFixed(0)} ft</dd>
            </div>
            <div>
              <dt>速度</dt>
              <dd>
                {num(cursor.sample.speed) !== null
                  ? `${msToKnots(cursor.sample.speed as number).toFixed(0)} kt`
                  : "-"}
              </dd>
            </div>
            <div>
              <dt>迎角</dt>
              <dd>
                {num(cursor.sample.aoa) !== null
                  ? `${(cursor.sample.aoa as number).toFixed(1)}°`
                  : "-"}
              </dd>
            </div>
            <div>
              <dt>バンク</dt>
              <dd>
                {Math.abs(cursor.roll) < 0.5
                  ? "0°"
                  : `${cursor.roll > 0 ? "右" : "左"} ${Math.abs(cursor.roll).toFixed(0)}°`}
                {cursor.estimated.roll && "（推定）"}
              </dd>
            </div>
            <div>
              <dt>ピッチ</dt>
              <dd>
                {signed(cursor.pitch, 1)}°{cursor.estimated.pitch && "（推定）"}
              </dd>
            </div>
            <div>
              <dt>{path.shipFrame ? "機首（BRC 比）" : "機首（コース比）"}</dt>
              <dd>
                {Math.abs(cursor.heading) < 0.5
                  ? "0°"
                  : `${cursor.heading > 0 ? "右" : "左"} ${Math.abs(cursor.heading).toFixed(1)}°`}
                {cursor.estimated.heading && "（推定）"}
              </dd>
            </div>
            <div>
              <dt>荷重</dt>
              <dd>
                {num(cursor.sample.load_factor) !== null
                  ? `${(cursor.sample.load_factor as number).toFixed(2)} G`
                  : "-"}
              </dd>
            </div>
          </dl>
        )}
        <p className="flight3d-hint">左ドラッグ: 回転 ／ 右ドラッグ: 移動 ／ ホイール: ズーム</p>
      </div>

      <div className="flight3d-timeline">
        <button type="button" className="btn" onClick={togglePlay} aria-pressed={playing}>
          {playing ? "❚❚ 停止" : "▶ 再生"}
        </button>
        <select
          aria-label="再生速度"
          value={playSpeed}
          onChange={(e) => setPlaySpeed(Number(e.target.value))}
        >
          {PLAY_SPEEDS.map((s) => (
            <option key={s} value={s}>
              ×{s}
            </option>
          ))}
        </select>
        <input
          type="range"
          aria-label="表示する時刻"
          min={start}
          max={end}
          step={0.1}
          value={current}
          onChange={(e) => {
            setPlaying(false);
            setTime(Number(e.target.value));
          }}
        />
      </div>

      <figcaption className="pattern-legend">
        {legsShown.map((leg) => (
          <span key={leg} className="pattern-legend-item">
            <span className={`pattern-legend-swatch pattern-leg-${leg}`} />
            {legLabels[leg]}
          </span>
        ))}
        <span className="pattern-legend-item">
          <span className="flight3d-swatch flight3d-swatch-ghost" />
          {ghostIntervalS} 秒ごとの機影{model.label && `（${model.label}）`}
        </span>
        <span className="pattern-legend-item">
          <span className="flight3d-swatch flight3d-swatch-cursor" />
          選択中の時刻
        </span>
        {venue?.glideslope && (
          <span className="pattern-legend-item">
            <span className="pattern-legend-swatch flight3d-swatch-glideslope" />
            理想グライドスロープ
          </span>
        )}
        {showBeams && venue?.beams && (
          <span className="pattern-legend-item">
            <span className="flight3d-swatch flight3d-swatch-beam" />
            進入ビーム（ローカライザー／グライドパス）
          </span>
        )}
      </figcaption>
      <p className="flight3d-note">
        {path.shipFrame
          ? "艦と一緒に動く座標（奥行き = 艦首方向）で描いています。"
          : "着陸方向を奥行きに取った座標で描いています。"}
        {` ${airframeCaption(airframe, model)}`}
        {venue?.carrier && " 艦の形は着艦エリアの設定値から描いた概形です。"}
        {showBeams &&
          venue?.beams &&
          (venue.runway
            ? " 進入ビームの十字の腕の端は、ILS の計器がフルスケールになる位置の目安です（FAA AIM: ローカライザーは進入端で幅 700 ft、グライドパスは上下 1.4°）。"
            : " 進入ビームは表示用の目安です（グライドパスは上下 1.4°、ローカライザーは左右 ±2°）。")}
        {exaggeration > 1 && ` 高さを ${exaggeration} 倍に強調しています（機影の姿勢は実際の角度のまま）。`}
        {note && ` ${note}`}
      </p>
    </figure>
  );
}
