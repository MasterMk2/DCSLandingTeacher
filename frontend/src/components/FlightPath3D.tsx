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
  medianSpeed,
  modelLength,
  pointAt,
  sceneBounds,
  venueGeometry,
  type FlightPath,
  type FlightPoint,
  type SceneBounds,
  type Strip,
  type Vec3,
  type VenueGeometry,
} from "../lib/flight3d";
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
  hull: THREE.Color;
  glideslope: THREE.Color;
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
    hull: new THREE.Color("#8a8e95"),
    glideslope: css("--info", "#7f9bb5"),
    touchdown: css("--danger", "#cd6a61"),
  };
}

// ---------------------------------------------------------------------------
// The aircraft model: a generic jet, unit length, nose +x, top +y, right +z.
// ---------------------------------------------------------------------------

function aircraftGeometry(): THREE.BufferGeometry {
  const positions: number[] = [];
  const colors: number[] = [];
  const BODY: Vec3 = [1, 1, 1];
  const CANOPY: Vec3 = [0.22, 0.26, 0.3];
  const tri = (a: Vec3, b: Vec3, c: Vec3, color: Vec3 = BODY) => {
    positions.push(...a, ...b, ...c);
    colors.push(...color, ...color, ...color);
  };
  const quad = (a: Vec3, b: Vec3, c: Vec3, d: Vec3) => {
    tri(a, b, c);
    tri(a, c, d);
  };
  // Fuselage cross-sections: top, right, bottom, left.
  const ring = (x: number, half: number, top: number, bottom: number): Vec3[] => [
    [x, top, 0],
    [x, 0, half],
    [x, -bottom, 0],
    [x, 0, -half],
  ];
  const nose: Vec3 = [0.5, 0, 0];
  const front = ring(0.22, 0.05, 0.055, 0.045);
  const rear = ring(-0.36, 0.055, 0.045, 0.04);
  const tail: Vec3 = [-0.5, 0.01, 0];
  for (let i = 0; i < 4; i++) {
    const j = (i + 1) % 4;
    tri(nose, front[i], front[j]);
    quad(front[i], rear[i], rear[j], front[j]);
    tri(rear[i], tail, rear[j]);
  }
  // Canopy: a dark ridge on top, so which way is up is never in doubt.
  const canopyFront: Vec3 = [0.3, 0.04, 0];
  const canopyTop: Vec3 = [0.12, 0.1, 0];
  const canopyBack: Vec3 = [0.0, 0.05, 0];
  for (const side of [-1, 1]) {
    const edge: Vec3 = [0.14, 0.05, 0.035 * side];
    tri(canopyFront, canopyTop, edge, CANOPY);
    tri(canopyTop, canopyBack, edge, CANOPY);
  }
  for (const side of [-1, 1]) {
    // Wing
    quad([0.12, 0, 0.05 * side], [-0.14, 0, 0.36 * side], [-0.22, 0, 0.36 * side], [-0.26, 0, 0.05 * side]);
    // Horizontal stabiliser
    quad([-0.34, 0, 0.04 * side], [-0.43, 0, 0.17 * side], [-0.48, 0, 0.17 * side], [-0.5, 0, 0.04 * side]);
  }
  // Fin
  quad([-0.28, 0.04, 0], [-0.43, 0.22, 0], [-0.49, 0.22, 0], [-0.5, 0.03, 0]);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
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
  modelLength: number;
  touchdownTime: number | null;
}

class FlightScene {
  private readonly renderer: THREE.WebGLRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera = new THREE.PerspectiveCamera(40, 1, 1, 100_000);
  private readonly controls: OrbitControls;
  private readonly aircraft = aircraftGeometry();
  private readonly resizeObserver: ResizeObserver;
  private content = new THREE.Group();
  private contentDisposables: { dispose(): void }[] = [];
  private lineMaterials: LineMaterial[] = [];
  private readonly cursorMesh: THREE.Mesh;
  private readonly cursorDrop: THREE.Line;
  private framed = false;
  private bounds: SceneBounds | null = null;
  private exaggeration = 1;
  private dirty = true;
  private animationFrame = 0;
  private width = 1;
  private height = 1;

  constructor(
    private readonly host: HTMLElement,
    private readonly palette: Palette,
  ) {
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
    // nautical miles, centred on the path.
    const step = gridStepM(bounds.span);
    const cells = Math.ceil((bounds.span * 1.5) / step / 2) * 2;
    const size = cells * step;
    const cx = Math.round(bounds.center[0] / step) * step;
    const cz = Math.round(bounds.center[2] / step) * step;
    const ground = new THREE.Mesh(
      this.track(new THREE.PlaneGeometry(size * 4, size * 4).rotateX(-Math.PI / 2)),
      this.track(new THREE.MeshBasicMaterial({ color: palette.ground })),
    );
    ground.position.set(cx, -1, cz);
    group.add(ground);
    // Both grid colours alike: GridHelper's own centre lines fall wherever
    // the grid happens to be centred and read as a course line.
    const grid = new THREE.GridHelper(size, cells, palette.grid, palette.grid);
    this.track(grid.geometry);
    this.track(grid.material as THREE.Material);
    grid.position.set(cx, 0, cz);
    group.add(grid);

    // Extended landing course on the ground, dashed, like the plan view's
    // centreline: the line the final should have been flown down.
    if (venue.course) {
      const { from, to } = venue.course;
      const centerline = new THREE.Line(
        this.track(
          new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(from[0], 0.8, from[2]),
            new THREE.Vector3(to[0], 0.8, to[2]),
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

    // Runway, or the ship and its landing area.
    if (venue.runway) group.add(this.strip(venue.runway, 0.3, palette.runway, 0.85));
    if (venue.hull) {
      const hull = venue.hull;
      const length = Math.abs(hull.to[0] - hull.from[0]);
      const box = new THREE.Mesh(
        this.track(new THREE.BoxGeometry(length, 14, hull.width)),
        this.track(new THREE.MeshStandardMaterial({ color: palette.hull, roughness: 0.9 })),
      );
      box.position.set((hull.from[0] + hull.to[0]) / 2, -7, 0);
      group.add(box);
    }
    if (venue.landingArea) {
      group.add(this.strip(venue.landingArea, 0.4, palette.runway, 0.9));
    }

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
          points.map((p) => new THREE.Vector3(p.x, 0.5, p.z)),
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
      const matrix = new THREE.Matrix4();
      ghosts.forEach((p, i) => mesh.setMatrixAt(i, poseMatrix(p, ex, opts.modelLength, matrix)));
      mesh.instanceMatrix.needsUpdate = true;
      mesh.computeBoundingSphere();
      this.contentDisposables.push({ dispose: () => mesh.dispose() });
      group.add(mesh);

      const drops: THREE.Vector3[] = [];
      for (const p of ghosts) {
        drops.push(new THREE.Vector3(p.x, 0, p.z), new THREE.Vector3(p.x, p.y * ex, p.z));
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
    const td = opts.touchdownTime !== null ? pointAt(points, opts.touchdownTime, 1) : null;
    if (td) {
      const r = opts.modelLength * 0.45;
      const ring = new THREE.Mesh(
        this.track(new THREE.RingGeometry(r, r * 1.35, 40).rotateX(-Math.PI / 2)),
        this.track(new THREE.MeshBasicMaterial({ color: palette.touchdown, side: THREE.DoubleSide })),
      );
      ring.position.set(td.x, 0.6, td.z);
      group.add(ring);
    }

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

  setCursor(point: FlightPoint | null, exaggeration: number, length: number) {
    if (!point) {
      this.cursorMesh.visible = false;
      this.cursorDrop.visible = false;
    } else {
      poseMatrix(point, exaggeration, length * 1.15, this.cursorMesh.matrix);
      this.cursorMesh.matrixWorldNeedsUpdate = true;
      const drop = this.cursorDrop.geometry.getAttribute("position") as THREE.BufferAttribute;
      drop.setXYZ(0, point.x, 0, point.z);
      drop.setXYZ(1, point.x, point.y * exaggeration, point.z);
      drop.needsUpdate = true;
      this.cursorDrop.geometry.computeBoundingSphere();
      this.cursorMesh.visible = true;
      this.cursorDrop.visible = true;
    }
    this.dirty = true;
  }

  /** Frame the whole path from one of the presets. */
  view(preset: ViewPreset) {
    this.frame(vec(VIEW_DIRECTIONS[preset]));
  }

  /** Look at the whole path from `direction` (target -> camera): the camera
   *  backs off just far enough for every corner of the bounds to be in the
   *  frustum. */
  private frame(direction: THREE.Vector3) {
    const bounds = this.bounds;
    if (!bounds || direction.lengthSq() === 0) return;
    const ex = this.exaggeration;
    const min = new THREE.Vector3(bounds.min[0], bounds.min[1] * ex, bounds.min[2]);
    const max = new THREE.Vector3(bounds.max[0], bounds.max[1] * ex, bounds.max[2]);
    const center = min.clone().add(max).multiplyScalar(0.5);
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

export default function FlightPath3D({ track, metrics }: FlightPath3DProps) {
  const legTimes = useMemo(
    () => legTimesFrom(metrics, track.touchdown_time),
    [metrics, track.touchdown_time],
  );
  const path = useMemo(() => buildFlightPath(track, legTimes), [track, legTimes]);
  const bounds = useMemo(() => (path ? sceneBounds(path.points) : null), [path]);

  const [ghostIntervalS, setGhostIntervalS] = useState(2);
  const [exaggeration, setExaggeration] = useState(1);
  const [modelScale, setModelScale] = useState(1);
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
  const length = bounds ? modelLength(bounds.span, speed, ghostIntervalS, modelScale) : 0;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !path) return;
    let scene: FlightScene;
    try {
      scene = new FlightScene(host, readPalette());
    } catch {
      setUnsupported(true);
      return;
    }
    sceneRef.current = scene;
    return () => {
      scene.dispose();
      sceneRef.current = null;
    };
  }, [path]);

  useEffect(() => {
    if (!path) return;
    sceneRef.current?.setContent(path, venueGeometry(track, path.shipFrame, glideslopeLength(path)), {
      ghostIntervalS,
      exaggeration,
      modelLength: length,
      touchdownTime,
    });
  }, [path, track, ghostIntervalS, exaggeration, length, touchdownTime]);

  const cursor = path ? pointAt(path.points, current, 1) : null;
  useEffect(() => {
    sceneRef.current?.setCursor(cursor, exaggeration, length);
  }, [cursor, exaggeration, length, path]);

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
          {ghostIntervalS} 秒ごとの機影
        </span>
        <span className="pattern-legend-item">
          <span className="flight3d-swatch flight3d-swatch-cursor" />
          選択中の時刻
        </span>
        {venueHasGlideslope(track) && (
          <span className="pattern-legend-item">
            <span className="pattern-legend-swatch flight3d-swatch-glideslope" />
            理想グライドスロープ
          </span>
        )}
      </figcaption>
      <p className="flight3d-note">
        {path.shipFrame
          ? "艦と一緒に動く座標（奥行き = 艦首方向）で描いています。"
          : "着陸方向を奥行きに取った座標で描いています。"}
        {exaggeration > 1 && ` 高さを ${exaggeration} 倍に強調しています（機影の姿勢は実際の角度のまま）。`}
        {note && ` ${note}`}
      </p>
    </figure>
  );
}

function venueHasGlideslope(track: ApproachTrack): boolean {
  return num(track.glideslope_deg) !== null;
}

/** Glide path drawn back to where the final starts, within 1-3 nm. */
function glideslopeLength(path: FlightPath): number {
  const finals = path.points.filter((p) => p.leg === "final");
  const source = finals.length > 1 ? finals : path.points;
  const farthest = Math.max(...source.map((p) => Math.hypot(p.x, p.z)));
  return Math.min(Math.max(farthest, 1852), 3 * 1852);
}
