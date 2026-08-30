"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

/**
 * Dithered terrain — the "Dithered Geospatial" brand keyword, literally.
 *
 * A procedural fBm heightmap is thresholded through a 4×4 Bayer matrix and
 * drawn as black-and-white dots. Scrolling advances the sample offset so the
 * terrain drifts. No textures, no shaders, no external assets.
 *
 * Cost control: the dot grid is coarse (DOT px), only lit dots are painted, and
 * the field is redrawn at most ~30fps and only when the scroll offset actually
 * moved. Under prefers-reduced-motion it paints one static frame and stops.
 *
 * Must be imported with `dynamic(..., { ssr: false })` — it touches window.
 */

const DOT = 6; // grid pitch in CSS px
const BAYER = [
  [0, 8, 2, 10],
  [12, 4, 14, 6],
  [3, 11, 1, 9],
  [15, 7, 13, 5],
].map((row) => row.map((v) => (v + 0.5) / 16));

/** Deterministic hash → [0,1). Stable across reloads, no RNG state. */
function hash2(x: number, y: number): number {
  const s = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
  return s - Math.floor(s);
}

function smooth(t: number) {
  return t * t * (3 - 2 * t);
}

/** Value noise with bilinear interpolation. */
function noise(x: number, y: number): number {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const xf = smooth(x - xi);
  const yf = smooth(y - yi);
  const a = hash2(xi, yi);
  const b = hash2(xi + 1, yi);
  const c = hash2(xi, yi + 1);
  const d = hash2(xi + 1, yi + 1);
  return (a + (b - a) * xf) * (1 - yf) + (c + (d - c) * xf) * yf;
}

/** Four-octave fBm. Reads as ridged terrain rather than clouds. */
function fbm(x: number, y: number): number {
  let v = 0;
  let amp = 0.5;
  let freq = 1;
  for (let i = 0; i < 4; i++) {
    v += amp * noise(x * freq, y * freq);
    freq *= 2.07;
    amp *= 0.5;
  }
  return v;
}

export default function DitherTerrain({
  className,
  /** 0 = flat wash, 1 = full contrast. */
  intensity = 1,
}: {
  className?: string;
  intensity?: number;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    const canvas = canvasRef.current;
    if (!host || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let w = 0;
    let h = 0;
    let offset = 0;
    let lastOffset = Number.NaN;
    let raf: number | null = null;
    let lastPaint = 0;

    const paint = () => {
      ctx.clearRect(0, 0, w, h);
      const cols = Math.ceil(w / DOT);
      const rows = Math.ceil(h / DOT);

      ctx.fillStyle = "#F4F4F4";
      for (let gy = 0; gy < rows; gy++) {
        // Fade the field out toward the bottom so content stays readable.
        const vFade = 1 - (gy / rows) * 0.55;
        for (let gx = 0; gx < cols; gx++) {
          // Low frequency + a hard floor gives distinct landmasses rather than
          // an even wash of static.
          const n = fbm(gx * 0.017, gy * 0.017 + offset);
          const level = Math.min(1, Math.max(0, (n - 0.42) * 3.4)) * intensity * vFade;
          if (level <= 0.02) continue;
          if (level > BAYER[gy & 3][gx & 3]) {
            ctx.globalAlpha = 0.14 + level * 0.5;
            ctx.fillRect(gx * DOT, gy * DOT, 1.6, 1.6);
          }
        }
      }
      ctx.globalAlpha = 1;
    };

    const resize = () => {
      const rect = host.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = Math.max(1, Math.round(rect.width));
      h = Math.max(1, Math.round(rect.height));
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      lastOffset = Number.NaN;
      paint();
    };

    const ro = new ResizeObserver(resize);
    ro.observe(host);
    resize();

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (reduced.matches) {
      return () => ro.disconnect();
    }

    const loop = (t: number) => {
      raf = requestAnimationFrame(loop);
      if (t - lastPaint < 33) return; // ~30fps ceiling
      lastPaint = t;
      offset = window.scrollY * 0.0016;
      if (Math.abs(offset - lastOffset) < 0.0006) return; // idle → no work
      lastOffset = offset;
      paint();
    };
    raf = requestAnimationFrame(loop);

    return () => {
      ro.disconnect();
      if (raf !== null) cancelAnimationFrame(raf);
    };
  }, [intensity]);

  return (
    <div ref={hostRef} aria-hidden className={cn("pointer-events-none absolute inset-0", className)}>
      <canvas ref={canvasRef} className="absolute inset-0" />
    </div>
  );
}
