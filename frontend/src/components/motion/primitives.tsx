"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion, useInView, useReducedMotion } from "motion/react";
import { cn } from "@/lib/utils";

/* ─── Layout ──────────────────────────────────────────────────────────────── */

/** Page gutter. Everything on the site sits inside this. */
export function Shell({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("mx-auto w-full max-w-shell px-[clamp(16px,4vw,40px)]", className)}>
      {children}
    </div>
  );
}

/** A full-bleed section with a top hairline and generous vertical rhythm. */
export function Section({
  id,
  children,
  className,
  bleed = false,
}: {
  id?: string;
  children: ReactNode;
  className?: string;
  bleed?: boolean;
}) {
  return (
    <section
      id={id}
      className={cn(
        "relative border-t border-mi-line py-[clamp(72px,10vw,140px)]",
        className,
      )}
    >
      {bleed ? children : <Shell>{children}</Shell>}
    </section>
  );
}

/** Numbered mono eyebrow, e.g. "02 / SOURCES". */
export function SectionLabel({ index, children }: { index: string; children: ReactNode }) {
  return (
    <p className="mb-6 font-mono text-[10px] uppercase tracking-label text-mi-fg-muted">
      <span className="text-mi-orange">{index}</span>
      <span className="mx-2 text-mi-grey">/</span>
      {children}
    </p>
  );
}

/** Section headline. Sans, tight, never letterspaced. */
export function SectionTitle({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <h2
      className={cn(
        "max-w-[22ch] text-[clamp(28px,3.4vw,44px)] font-medium leading-[1.08] tracking-tight text-mi-fg-strong",
        className,
      )}
    >
      {children}
    </h2>
  );
}

/* ─── Motion ──────────────────────────────────────────────────────────────── */

/**
 * Scroll reveal. 12px translate, 350ms, ease-out — mechanical, no overshoot.
 * Collapses to a plain render under prefers-reduced-motion.
 */
export function Reveal({
  children,
  delay = 0,
  className,
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { once: true, margin: "-12% 0px" });
  const reduced = useReducedMotion();

  if (reduced) return <div className={className}>{children}</div>;

  return (
    <motion.div
      ref={ref}
      className={className}
      initial={{ opacity: 0, y: 12 }}
      animate={inView ? { opacity: 1, y: 0 } : undefined}
      transition={{ duration: 0.35, ease: [0.2, 0, 0, 1], delay }}
    >
      {children}
    </motion.div>
  );
}

/**
 * Character-by-character reveal in mono. Under reduced motion the full string
 * renders immediately — the content is never gated behind the animation.
 */
export function Typewriter({
  text,
  speed = 18,
  start = true,
  className,
  caret = true,
  onDone,
}: {
  text: string;
  speed?: number;
  start?: boolean;
  className?: string;
  caret?: boolean;
  onDone?: () => void;
}) {
  const reduced = useReducedMotion();
  const animated = !reduced && start;
  const [n, setN] = useState(0);

  const doneRef = useRef(onDone);
  useEffect(() => {
    doneRef.current = onDone;
  });

  useEffect(() => {
    if (!animated) {
      doneRef.current?.();
      return;
    }
    // Driven off the clock rather than a fixed-step interval, so the reveal
    // keeps pace if a frame is dropped.
    let raf = 0;
    let fired = false;
    const t0 = performance.now();
    const tick = (t: number) => {
      const i = Math.min(text.length, Math.floor((t - t0) / speed));
      setN(i);
      if (i < text.length) {
        raf = requestAnimationFrame(tick);
      } else if (!fired) {
        fired = true;
        doneRef.current?.();
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [text, speed, animated]);

  const shown = animated ? n : text.length;

  return (
    <span className={cn("font-mono", className)}>
      {/* Full string stays in the a11y tree; the clipped copy is decorative. */}
      <span className="sr-only">{text}</span>
      <span aria-hidden>{text.slice(0, shown)}</span>
      {caret && animated && shown < text.length && (
        <span aria-hidden className="ml-px inline-block w-[0.5em] animate-caret bg-mi-orange align-baseline">
          &nbsp;
        </span>
      )}
    </span>
  );
}

/** Numbers count up. They do not fade in. */
export function CountUp({
  to,
  decimals = 0,
  duration = 800,
  className,
}: {
  to: number;
  decimals?: number;
  duration?: number;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-10% 0px" });
  const reduced = useReducedMotion();
  const [v, setV] = useState(reduced ? to : 0);

  useEffect(() => {
    if (reduced || !inView) return;
    let raf = 0;
    const t0 = performance.now();
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / duration);
      setV(to * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, to, duration, reduced]);

  return (
    <span ref={ref} className={cn("mono-num font-mono", className)}>
      {/* Grouped, matching the vanilla toLocaleString() — 260,400 m² not 260400. */}
      {v.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })}
    </span>
  );
}

/**
 * Reveals `count` items one at a time once the element is in view, and returns
 * how many are currently lit. This is the "field chips light up one at a time"
 * behaviour — a checklist visibly running, not a list that simply appears.
 *
 * Under reduced motion every item is lit from the first frame.
 */
export function useSequence<T extends HTMLElement = HTMLDivElement>(count: number, step = 90) {
  const ref = useRef<T>(null);
  const inView = useInView(ref, { once: true, margin: "-8% 0px" });
  const reduced = useReducedMotion();
  const [lit, setLit] = useState(0);

  useEffect(() => {
    // Reduced motion is handled by the derived return below, not by state.
    if (reduced || !inView) return;
    let raf = 0;
    const t0 = performance.now();
    const tick = (t: number) => {
      const n = Math.min(count, Math.floor((t - t0) / step) + 1);
      setLit(n);
      if (n < count) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, count, step, reduced]);

  return { ref, lit: reduced ? count : lit };
}

/**
 * A value that arrives rather than appears: the number counts up and a hairline
 * sweeps under it once. Used for every headline metric in the workspace.
 */
export function Metric({
  value,
  unit,
  decimals = 0,
  className,
}: {
  value: number;
  unit?: string;
  decimals?: number;
  className?: string;
}) {
  const finite = Number.isFinite(value);
  return (
    <span className={cn("inline-flex items-baseline gap-1.5", className)}>
      <span className="mono-num font-mono text-[30px] font-medium leading-none tracking-tight text-mi-fg-strong">
        {finite ? <CountUp to={value} decimals={decimals} /> : "—"}
      </span>
      {unit && <span className="font-mono text-[11px] text-mi-fg-muted">{unit}</span>}
    </span>
  );
}

/* ─── Controls ────────────────────────────────────────────────────────────── */

/**
 * Square, hairline button. Orange is a 1px underline on hover, never a fill —
 * see the orange law in design-system/mireye/MASTER.md.
 */
export function Button({
  children,
  href,
  variant = "ghost",
  className,
  ...rest
}: {
  children: ReactNode;
  href?: string;
  variant?: "solid" | "ghost";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const cls = cn(
    "group relative inline-flex cursor-pointer items-center gap-2 rounded-none px-5 py-3",
    "font-mono text-[11px] uppercase tracking-cite transition-colors duration-micro ease-mi",
    variant === "solid"
      ? "bg-mi-chalk text-mi-black hover:bg-mi-white"
      : "border border-mi-line text-mi-fg hover:border-mi-line-strong hover:text-mi-fg-strong",
    className,
  );

  const inner = (
    <>
      {children}
      <span
        aria-hidden
        className="absolute inset-x-5 bottom-[9px] h-px origin-left scale-x-0 bg-mi-orange transition-transform duration-micro ease-mi group-hover:scale-x-100"
      />
    </>
  );

  if (href) {
    return (
      <a href={href} className={cls}>
        {inner}
      </a>
    );
  }
  return (
    <button type="button" className={cls} {...rest}>
      {inner}
    </button>
  );
}
