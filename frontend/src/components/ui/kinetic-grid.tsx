"use client";

import { useEffect, useRef, useCallback, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/components/theme";

/**
 * Kinetic grid — a warping lattice that bends toward the pointer and ripples on
 * click. Adapted from the 21st.dev component for Mireye:
 *
 *  - Palette forced achromatic. The blue theme is gone; orange appears only on
 *    the click ripple, within the orange budget.
 *  - Canvas is container-scoped (absolute + ResizeObserver), not window-fixed,
 *    so it can sit behind one section instead of the whole document.
 *  - Pointer coordinates are container-relative, so the warp tracks correctly
 *    when the grid is not at the viewport origin.
 *  - Device-pixel-ratio aware — the original was soft on retina.
 *  - Honours prefers-reduced-motion: renders one static frame, no rAF, no
 *    listeners.
 */

interface Point {
  x: number;
  y: number;
}

interface Ripple {
  x: number;
  y: number;
  radius: number;
  opacity: number;
  born: number;
}

const CELL_SIZE = 55;
const INFLUENCE_RADIUS = 260;
const MAX_WARP = 24;
const DOT_SPACING = 28;
const LERP_SPEED = 0.08;

const NODE_BASE_RADIUS = 1.6;
const NODE_ACTIVE_RADIUS = 3;

/**
 * Two fields, one per theme. Denim grey lattice at rest; the pointer raises it
 * toward the foreground colour. Orange appears only on the click ripple.
 */
const THEMES = {
  dark: {
    bg: "#000000",
    dots: "rgba(244,244,244,0.05)",
    lineBase: { r: 113, g: 113, b: 122, a: 0.3 },
    lineActive: { r: 244, g: 244, b: 244, a: 0.85 },
    nodeBase: { r: 113, g: 113, b: 122, a: 0.35 },
    nodeActive: { r: 255, g: 255, b: 255, a: 1 },
    glow: "255,255,255",
    ripple: "255,102,0",
  },
  light: {
    bg: "#ffffff",
    dots: "rgba(0,0,0,0.05)",
    lineBase: { r: 113, g: 113, b: 122, a: 0.34 },
    lineActive: { r: 0, g: 0, b: 0, a: 0.75 },
    nodeBase: { r: 113, g: 113, b: 122, a: 0.42 },
    nodeActive: { r: 0, g: 0, b: 0, a: 1 },
    glow: "0,0,0",
    ripple: "194,74,0",
  },
} as const;

function lerpN(a: number, b: number, t: number) {
  return a + (b - a) * t;
}

function lerpColor(
  base: { r: number; g: number; b: number; a: number },
  active: { r: number; g: number; b: number; a: number },
  t: number,
): string {
  const r = Math.round(lerpN(base.r, active.r, t));
  const g = Math.round(lerpN(base.g, active.g, t));
  const b = Math.round(lerpN(base.b, active.b, t));
  const a = lerpN(base.a, active.a, t);
  return `rgba(${r},${g},${b},${a.toFixed(3)})`;
}

export default function KineticGrid({
  children,
  className,
}: {
  children?: ReactNode;
  className?: string;
}) {
  // The field follows the app theme rather than a fixed palette — a black
  // canvas under a light page would just be a black rectangle.
  const { theme } = useTheme();
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const mouseRef = useRef<Point>({ x: -9999, y: -9999 });
  const targetMouseRef = useRef<Point>({ x: -9999, y: -9999 });
  const ripplesRef = useRef<Ripple[]>([]);
  const rafRef = useRef<number | null>(null);
  const sizeRef = useRef<{ w: number; h: number }>({ w: 0, h: 0 });

  const getWarpedPoint = useCallback(
    (
      gx: number,
      gy: number,
      col: number,
      row: number,
      mouse: Point,
      ripples: Ripple[],
      cols: number,
      rows: number,
    ): { pt: Point; proximity: number } => {
      // Pin the boundary rows/cols so the lattice never tears away from its edges.
      const edgeMargin = 1.5;
      const colPin = Math.min(col / edgeMargin, (cols - 1 - col) / edgeMargin, 1);
      const rowPin = Math.min(row / edgeMargin, (rows - 1 - row) / edgeMargin, 1);
      const pinFactor = colPin * colPin * rowPin * rowPin;

      const dx = gx - mouse.x;
      const dy = gy - mouse.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const proximity = Math.max(0, 1 - dist / INFLUENCE_RADIUS) * pinFactor;

      let rx = 0;
      let ry = 0;
      for (const r of ripples) {
        const rdx = gx - r.x;
        const rdy = gy - r.y;
        const rdist = Math.sqrt(rdx * rdx + rdy * rdy);
        const waveWidth = 55;
        const diff = rdist - r.radius;
        if (Math.abs(diff) < waveWidth) {
          const strength = (1 - Math.abs(diff) / waveWidth) * r.opacity * 18 * pinFactor;
          const angle = Math.atan2(rdy, rdx);
          const sign = diff < 0 ? -1 : 1;
          rx += Math.cos(angle) * strength * sign * -1;
          ry += Math.sin(angle) * strength * sign * -1;
        }
      }

      if (dist < INFLUENCE_RADIUS && dist > 0 && pinFactor > 0) {
        const t = dist / INFLUENCE_RADIUS;
        const eased = t < 0.01 ? 0 : (1 - t) * (1 - t) * Math.min(1, dist / 60);
        const warpAmt = eased * MAX_WARP * pinFactor;
        const angle = Math.atan2(dy, dx);
        return {
          pt: {
            x: gx - Math.cos(angle) * warpAmt + rx,
            y: gy - Math.sin(angle) * warpAmt + ry,
          },
          proximity,
        };
      }

      return { pt: { x: gx + rx, y: gy + ry }, proximity };
    },
    [],
  );

  const themeName = theme;
  const draw = useCallback(
    (now: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      const { w: W, h: H } = sizeRef.current;
      if (W === 0 || H === 0) return;

      const mouse = mouseRef.current;
      const ripples = ripplesRef.current;
      const palette = THEMES[themeName];

      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = palette.bg;
      ctx.fillRect(0, 0, W, H);

      // Static dot texture underneath the lattice.
      ctx.fillStyle = palette.dots;
      for (let x = DOT_SPACING / 2; x < W; x += DOT_SPACING) {
        for (let y = DOT_SPACING / 2; y < H; y += DOT_SPACING) {
          ctx.beginPath();
          ctx.arc(x, y, 0.7, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      for (let i = ripples.length - 1; i >= 0; i--) {
        const r = ripples[i];
        const age = (now - r.born) / 1000;
        r.radius = Math.max(0, age * 400);
        r.opacity = Math.max(0, 1 - age * 1.2);
        if (r.opacity <= 0) ripples.splice(i, 1);
      }

      const cols = Math.max(2, Math.ceil(W / CELL_SIZE)) + 1;
      const rows = Math.max(2, Math.ceil(H / CELL_SIZE)) + 1;
      const cellW = W / (cols - 1);
      const cellH = H / (rows - 1);

      const pts: Point[][] = [];
      const prox: number[][] = [];

      for (let row = 0; row < rows; row++) {
        pts[row] = [];
        prox[row] = [];
        for (let col = 0; col < cols; col++) {
          const { pt, proximity } = getWarpedPoint(
            col * cellW,
            row * cellH,
            col,
            row,
            mouse,
            ripples,
            cols,
            rows,
          );
          pts[row][col] = pt;
          prox[row][col] = proximity;
        }
      }

      const drawSeg = (p1: Point, p2: Point, pr1: number, pr2: number) => {
        const avg = (pr1 + pr2) / 2;
        const t = avg * avg * (3 - 2 * avg);
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.strokeStyle = lerpColor(palette.lineBase, palette.lineActive, t);
        ctx.lineWidth = lerpN(0.8, 1.4, t);
        ctx.stroke();
      };

      ctx.lineCap = "butt";

      for (let row = 0; row < rows; row++)
        for (let col = 0; col < cols - 1; col++)
          drawSeg(pts[row][col], pts[row][col + 1], prox[row][col], prox[row][col + 1]);

      for (let col = 0; col < cols; col++)
        for (let row = 0; row < rows - 1; row++)
          drawSeg(pts[row][col], pts[row + 1][col], prox[row][col], prox[row + 1][col]);

      for (let row = 0; row < rows; row++) {
        for (let col = 0; col < cols; col++) {
          const p = pts[row][col];
          const pr = prox[row][col];
          const t = pr * pr * (3 - 2 * pr);
          const r = lerpN(NODE_BASE_RADIUS, NODE_ACTIVE_RADIUS, t);

          if (t > 0.3) {
            const glowR = r + lerpN(0, 6, (t - 0.3) / 0.7);
            const grd = ctx.createRadialGradient(p.x, p.y, r * 0.5, p.x, p.y, glowR);
            grd.addColorStop(0, `rgba(${palette.glow},${(t * 0.22).toFixed(3)})`);
            grd.addColorStop(1, `rgba(${palette.glow},0)`);
            ctx.beginPath();
            ctx.arc(p.x, p.y, glowR, 0, Math.PI * 2);
            ctx.fillStyle = grd;
            ctx.fill();
          }

          ctx.beginPath();
          ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
          ctx.fillStyle = lerpColor(palette.nodeBase, palette.nodeActive, t);
          ctx.fill();
        }
      }

      for (const r of ripples) {
        ctx.beginPath();
        ctx.arc(r.x, r.y, Math.max(0, r.radius), 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${palette.ripple},${(r.opacity * 0.32).toFixed(3)})`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    },
    [getWarpedPoint, themeName],
  );

  useEffect(() => {
    const host = hostRef.current;
    const canvas = canvasRef.current;
    if (!host || !canvas) return;

    const ctx = canvas.getContext("2d");
    const setSize = () => {
      const rect = host.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.round(rect.width));
      const h = Math.max(1, Math.round(rect.height));
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
      sizeRef.current = { w, h };
    };

    setSize();

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const ro = new ResizeObserver(() => {
      setSize();
      if (reduced.matches) draw(performance.now());
    });
    ro.observe(host);

    if (reduced.matches) {
      // One static frame. No loop, no pointer tracking.
      draw(performance.now());
      return () => ro.disconnect();
    }

    const onPointerMove = (e: PointerEvent) => {
      const rect = host.getBoundingClientRect();
      targetMouseRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    };

    const onPointerLeave = () => {
      targetMouseRef.current = { x: -9999, y: -9999 };
    };

    const onClick = (e: PointerEvent) => {
      const rect = host.getBoundingClientRect();
      ripplesRef.current.push({
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
        radius: 0,
        opacity: 1,
        born: performance.now(),
      });
    };

    // The loop lives here rather than in a useCallback so it can schedule
    // itself without referencing a binding that is still initialising.
    const loop = (now: number) => {
      const m = mouseRef.current;
      const target = targetMouseRef.current;
      m.x = lerpN(m.x, target.x, LERP_SPEED);
      m.y = lerpN(m.y, target.y, LERP_SPEED);
      draw(now);
      rafRef.current = requestAnimationFrame(loop);
    };

    host.addEventListener("pointermove", onPointerMove);
    host.addEventListener("pointerleave", onPointerLeave);
    host.addEventListener("pointerdown", onClick);
    rafRef.current = requestAnimationFrame(loop);

    return () => {
      ro.disconnect();
      host.removeEventListener("pointermove", onPointerMove);
      host.removeEventListener("pointerleave", onPointerLeave);
      host.removeEventListener("pointerdown", onClick);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [draw]);

  return (
    <div ref={hostRef} className={cn("relative w-full overflow-hidden bg-mi-bg", className)}>
      <canvas ref={canvasRef} aria-hidden className="absolute inset-0 z-0 pointer-events-none" />
      <div className="relative z-10 h-full w-full">{children}</div>
    </div>
  );
}
