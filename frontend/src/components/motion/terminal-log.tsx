"use client";

import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "motion/react";
import { cn } from "@/lib/utils";

/**
 * Loading states are terminal logs. There are no spinners in this product.
 *
 * Lines print one at a time with a per-line dwell. Under reduced motion the
 * whole log renders at once — the information is never withheld.
 */
export function TerminalLog({
  lines,
  start = true,
  lineDelay = 260,
  loop = false,
  className,
  onDone,
}: {
  lines: string[];
  start?: boolean;
  lineDelay?: number;
  loop?: boolean;
  className?: string;
  onDone?: () => void;
}) {
  const reduced = useReducedMotion();
  const animated = !reduced && start;
  const [n, setN] = useState(0);
  const count = lines.length;

  // Callers pass inline closures; keep the latest without re-running the loop.
  const doneRef = useRef(onDone);
  useEffect(() => {
    doneRef.current = onDone;
  });

  useEffect(() => {
    if (!animated) {
      doneRef.current?.();
      return;
    }
    let raf = 0;
    let fired = false;
    const t0 = performance.now();
    const tick = (t: number) => {
      const elapsed = Math.floor((t - t0) / lineDelay);
      if (loop) {
        setN(elapsed % (count + 2));
        raf = requestAnimationFrame(tick);
        return;
      }
      const i = Math.min(count, elapsed);
      setN(i);
      if (i < count) {
        raf = requestAnimationFrame(tick);
      } else if (!fired) {
        fired = true;
        doneRef.current?.();
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [count, animated, lineDelay, loop]);

  const shown = animated ? Math.min(n, count) : count;

  return (
    <div
      className={cn("font-mono text-[11px] leading-[1.9] text-mi-fg-muted", className)}
      role="status"
      aria-live="polite"
    >
      {lines.slice(0, shown).map((line, i) => {
        const isLast = i === shown - 1 && shown < count;
        const isConfidence = line.startsWith("confidence:");
        return (
          <div key={`${line}-${i}`} className="flex gap-2">
            <span aria-hidden className="select-none text-mi-grey">
              {String(i + 1).padStart(2, "0")}
            </span>
            <span className={cn(isConfidence && "text-mi-orange")}>
              {line}
              {isLast && (
                <span
                  aria-hidden
                  className="ml-1 inline-block w-[0.5em] animate-caret bg-mi-orange"
                >
                  &nbsp;
                </span>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}
