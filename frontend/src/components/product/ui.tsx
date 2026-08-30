import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/* ─── Brand ───────────────────────────────────────────────────────────────── */

/**
 * Hard-edged sheared-bar mark, derived from the brand book logo. Geometric
 * approximation — swap in the authored SVG when it is available.
 */
export function BrandMark({ className }: { className?: string }) {
  const bars: [number, number, number][] = [
    // [row, left edge, width]
    [0, 13, 9],
    [1, 9, 13],
    [2, 5, 17],
    [3, 5, 13],
    [4, 5, 9],
  ];
  const right: [number, number, number][] = [
    [1, 26, 11],
    [2, 26, 13],
    [3, 26, 11],
    [4, 26, 9],
  ];
  const bar = (row: number, left: number, width: number, key: string) => {
    const y = row * 6.4;
    const shear = 3.4;
    return (
      <path
        key={key}
        d={`M${left + shear} ${y} H${left + shear + width} L${left + width} ${y + 5} H${left} Z`}
      />
    );
  };
  return (
    <svg viewBox="0 0 40 31" className={cn("h-[17px] w-auto", className)} aria-hidden focusable="false">
      <g fill="currentColor">
        {bars.map(([r, l, w]) => bar(r, l, w, `l${r}`))}
        {right.map(([r, l, w]) => bar(r, l, w, `r${r}`))}
      </g>
    </svg>
  );
}

export function Brand({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5 text-mi-fg-strong", className)}>
      <BrandMark />
      <span className="text-[13px] font-semibold tracking-[0.14em]">MIREYE</span>
    </span>
  );
}

/* ─── Type ────────────────────────────────────────────────────────────────── */

/** Mono uppercase kicker. The only place letterspacing is allowed. */
export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p
      className={cn(
        "font-mono text-[10px] uppercase tracking-label text-mi-fg-muted",
        className,
      )}
    >
      {children}
    </p>
  );
}

/* ─── Provenance ──────────────────────────────────────────────────────────── */

export type Origin = "observed" | "derived" | "proposed";

const ORIGIN_LABEL: Record<Origin, string> = {
  observed: "Observed",
  derived: "Derived",
  proposed: "Proposed",
};

const ORIGIN_DOT: Record<Origin, string> = {
  observed: "bg-mi-observed",
  derived: "bg-mi-derived",
  proposed: "bg-mi-proposed",
};

/**
 * What is real, what was computed, what is only a design. This distinction is
 * the product's core claim, so it is always visible and never colour-only —
 * the word is always present next to the mark.
 */
export function OriginTag({ origin, className }: { origin: Origin; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 border border-mi-line px-2 py-1 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted",
        className,
      )}
    >
      <span aria-hidden className={cn("h-1.5 w-1.5", ORIGIN_DOT[origin])} />
      {ORIGIN_LABEL[origin]}
    </span>
  );
}

export function OriginLegend({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-3", className)}>
      {(Object.keys(ORIGIN_LABEL) as Origin[]).map((origin) => (
        <span
          key={origin}
          className="inline-flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted"
        >
          <span aria-hidden className={cn("h-1.5 w-1.5", ORIGIN_DOT[origin])} />
          {ORIGIN_LABEL[origin]}
        </span>
      ))}
    </div>
  );
}

/* ─── Verdicts ────────────────────────────────────────────────────────────── */

/**
 * Deterministic outcome chips.
 *
 * The palette has no green and no red, so severity is not carried by hue.
 * Instead: a clean result is quiet, an unknown is grey and hollow, and only a
 * real problem gets orange. Only problems draw the eye — which is the correct
 * reading order for a diligence tool.
 *
 * The label is always rendered, so this never depends on colour alone.
 */
export type Verdict = "PASS" | "BLOCKED" | "PARTIAL" | "UNRESOLVED" | "NOT ASSESSED";

const VERDICT_STYLE: Record<Verdict, { chip: string; mark: string }> = {
  PASS: { chip: "border-mi-line-strong text-mi-fg-strong", mark: "bg-mi-fg-strong" },
  BLOCKED: { chip: "border-mi-orange text-mi-orange-text", mark: "bg-mi-orange" },
  PARTIAL: { chip: "border-mi-line-strong text-mi-fg", mark: "bg-mi-fg-muted" },
  UNRESOLVED: { chip: "border-mi-line text-mi-fg-muted", mark: "border border-mi-fg-muted" },
  "NOT ASSESSED": { chip: "border-mi-line text-mi-fg-muted opacity-70", mark: "border border-dashed border-mi-fg-muted" },
};

export function VerdictChip({ verdict, className }: { verdict: Verdict; className?: string }) {
  const style = VERDICT_STYLE[verdict] ?? VERDICT_STYLE["NOT ASSESSED"];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 border px-2 py-1 font-mono text-[9px] font-medium uppercase tracking-cite",
        style.chip,
        className,
      )}
    >
      <span aria-hidden className={cn("h-1.5 w-1.5", style.mark)} />
      {verdict}
    </span>
  );
}

/* ─── Surfaces ────────────────────────────────────────────────────────────── */

/**
 * Soft-UI surface. Light is modelled from the top-left, so `raised` and
 * `plateau` carry a white edge there and black bottom-right, while `well`
 * inverts it and reads as carved into the page.
 *
 * Achromatic only — no hue, no glow, no translucency. Corners stay at 2px:
 * square soft-UI is the whole point, and the brand kit bans rounded-everything.
 */
export type Depth = "raised" | "plateau" | "well" | "flat";

const DEPTH: Record<Depth, string> = {
  raised: "bg-mi-surface shadow-raised",
  plateau: "bg-mi-surface-2 shadow-plateau",
  well: "bg-mi-surface-3 shadow-well",
  flat: "bg-mi-surface border border-mi-line",
};

export function Card({
  depth = "raised",
  hover = false,
  className,
  children,
  ...rest
}: {
  depth?: Depth;
  /** Lifts to plateau on hover. Use only on cards that do something. */
  hover?: boolean;
  className?: string;
  children: ReactNode;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-[2px] transition-shadow duration-reveal ease-mi",
        DEPTH[depth],
        hover && "hover:shadow-plateau",
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}

/**
 * An extruded divider: a black groove with a white lip under it. Reads as a
 * physical seam rather than a drawn line.
 */
export function Ridge({ className }: { className?: string }) {
  return <div aria-hidden className={cn("mi-ridge", className)} />;
}

/** A rail section: hairline separated, generous padding, square corners. */
export function RailSection({
  children,
  className,
  ...rest
}: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLElement>) {
  return (
    <section className={cn("mi-seam px-5 py-6", className)} {...rest}>
      {children}
    </section>
  );
}

export function RailHeading({
  kicker,
  title,
  aside,
}: {
  kicker: string;
  title: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="mb-5 flex items-start justify-between gap-3">
      <div className="min-w-0">
        <Eyebrow>{kicker}</Eyebrow>
        <h2 className="mt-1.5 text-[19px] font-medium leading-tight tracking-tight text-mi-fg-strong">
          {title}
        </h2>
      </div>
      {aside}
    </div>
  );
}

/** Label / value row, mono value, tabular figures. */
export function Fact({
  label,
  value,
  strong = false,
}: {
  label: ReactNode;
  value: ReactNode;
  strong?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-t border-mi-line py-2.5 first:border-t-0">
      <dt className="shrink-0 text-[12px] text-mi-fg-muted">{label}</dt>
      <dd
        className={cn(
          "mono-num min-w-0 truncate text-right font-mono text-[12px]",
          strong ? "text-mi-fg-strong" : "text-mi-fg",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

export function Quiet({ children }: { children: ReactNode }) {
  return <p className="text-[12px] leading-relaxed text-mi-fg-muted">{children}</p>;
}

/* ─── Controls ────────────────────────────────────────────────────────────── */

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & { children: ReactNode };

/**
 * Primary action. Solid chalk on black / black on white — high contrast without
 * spending the orange budget on a fill.
 */
export function PrimaryButton({ children, className, ...rest }: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex cursor-pointer items-center justify-center gap-2 rounded-[2px] bg-mi-fg-strong px-4 py-2.5",
        "font-mono text-[11px] uppercase tracking-cite text-mi-bg",
        // Sits proud of the page, then physically depresses on click.
        "shadow-raised transition-all duration-micro ease-mi hover:shadow-plateau",
        "active:translate-x-px active:translate-y-px active:shadow-press",
        "disabled:cursor-wait disabled:opacity-50 disabled:shadow-well",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export function GhostButton({ children, className, ...rest }: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex cursor-pointer items-center justify-center gap-2 rounded-[2px] bg-mi-surface px-4 py-2.5",
        "font-mono text-[11px] uppercase tracking-cite text-mi-fg",
        "shadow-raised transition-all duration-micro ease-mi hover:shadow-plateau hover:text-mi-fg-strong",
        "active:translate-x-px active:translate-y-px active:shadow-press",
        "disabled:cursor-wait disabled:opacity-50 disabled:shadow-well",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export function TextButton({ children, className, ...rest }: ButtonProps) {
  return (
    <button
      className={cn(
        "group relative inline-flex cursor-pointer items-center gap-1.5 py-1",
        "font-mono text-[11px] uppercase tracking-cite text-mi-fg-muted",
        "transition-colors duration-micro ease-mi hover:text-mi-fg-strong",
        "disabled:cursor-wait disabled:opacity-50",
        className,
      )}
      {...rest}
    >
      {children}
      <span
        aria-hidden
        className="absolute inset-x-0 bottom-0 h-px origin-left scale-x-0 bg-mi-orange transition-transform duration-micro ease-mi group-hover:scale-x-100"
      />
    </button>
  );
}

/** Text input / textarea shell. */
/** Inputs are carved into the surface rather than drawn on it. */
export const fieldClass =
  "w-full rounded-[2px] bg-mi-surface-3 px-3 py-2.5 text-[13px] text-mi-fg shadow-well " +
  "placeholder:text-mi-fg-muted/70 outline-none transition-shadow duration-micro ease-mi " +
  "focus:shadow-press";

export function FieldLabel({ children, htmlFor }: { children: ReactNode; htmlFor?: string }) {
  return (
    <label
      htmlFor={htmlFor}
      className="mb-2 block font-mono text-[10px] uppercase tracking-label text-mi-fg-muted"
    >
      {children}
    </label>
  );
}
