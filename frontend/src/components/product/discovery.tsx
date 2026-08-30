"use client";

import Link from "next/link";
import { ArrowRight, ArrowUpRight } from "lucide-react";
import { CountUp, Reveal, useSequence } from "@/components/motion/primitives";
import {
  Card,
  Eyebrow,
  FieldLabel,
  PrimaryButton,
  Ridge,
  VerdictChip,
  fieldClass,
  type Verdict,
} from "./ui";

export type Value = Record<string, unknown>;

const rec = (v: unknown): Value =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Value) : {};
const arr = (v: unknown): Value[] =>
  Array.isArray(v) ? v.filter((i): i is Value => Boolean(i) && typeof i === "object") : [];

export { REQUEST_STATUSES, type RequestStatus } from "@/lib/product-status";

export type Tone = "info" | "warning" | "success";

/* ─── Understanding ───────────────────────────────────────────────────────── */

/** What MIREYE extracted from the prompt, one chip per fact it can represent. */
export function Understanding({ items }: { items: string[] }) {
  const { ref, lit } = useSequence(items.length, 90);
  if (!items.length) return null;
  return (
    <div ref={ref}>
      <Eyebrow className="mb-3">Understood</Eyebrow>
      <div className="flex flex-wrap gap-2">
        {items.map((item, index) => (
          <span
            key={item}
            className={`rounded-[2px] bg-mi-surface px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-cite shadow-raised transition-colors duration-reveal ease-mi ${
              index < lit ? "text-mi-fg-strong" : "text-mi-fg-muted"
            }`}
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}

/* ─── Stages ──────────────────────────────────────────────────────────────── */

/**
 * The four-step spine: resolve → quote → context → freshness. Status strings
 * come straight from the API (`complete`, `active`, `pending`, `needs_input`,
 * `unavailable`) and are only mapped to marks here.
 */
export function Stages({ stages }: { stages: Value[] }) {
  if (!stages.length) return null;
  return (
    <ol className="grid gap-px bg-mi-line sm:grid-cols-2 lg:grid-cols-4">
      {stages.map((stage, index) => {
        const status = String(stage.status ?? "pending");
        const done = status === "complete";
        const active = status === "active";
        const needs = status === "needs_input";
        const off = status === "unavailable";
        return (
          <li key={String(stage.id ?? index)} className="bg-mi-surface px-4 py-3.5">
            <div className="flex items-center gap-2">
              <span
                aria-hidden
                className={`grid h-3.5 w-3.5 shrink-0 place-items-center font-mono text-[8px] leading-none ${
                  done
                    ? "bg-mi-fg-strong text-mi-bg"
                    : needs || active
                      ? "animate-caret bg-mi-orange text-mi-bg"
                      : "border border-mi-line-strong text-transparent"
                }`}
              >
                {done ? "✓" : off ? "–" : ""}
              </span>
              <span className="font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                {String(index + 1).padStart(2, "0")}
              </span>
            </div>
            <p
              className={`mt-2 text-[12px] leading-snug ${
                done || active || needs ? "text-mi-fg" : "text-mi-fg-muted"
              }`}
            >
              {String(stage.label ?? "")}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

/* ─── Notice ──────────────────────────────────────────────────────────────── */

export function Notice({
  title,
  message,
  tone = "info",
}: {
  title: string;
  message: string;
  tone?: Tone;
}) {
  return (
    <Card
      depth="raised"
      role="status"
      className={`flex items-start gap-3 p-4 ${
        tone === "warning" ? "border-l-2 border-l-mi-orange" : ""
      }`}
    >
      <span
        aria-hidden
        className={`mt-1 h-2 w-2 shrink-0 ${
          tone === "warning" ? "bg-mi-orange" : tone === "success" ? "bg-mi-fg-strong" : "bg-mi-derived"
        }`}
      />
      <div className="min-w-0">
        <h2 className="text-[14px] font-medium text-mi-fg-strong">{title}</h2>
        <p className="mt-1 text-[12px] leading-relaxed text-mi-fg-muted">{message}</p>
      </div>
    </Card>
  );
}

/* ─── Clarification choices ───────────────────────────────────────────────── */

export function Choices({
  items,
  onSelect,
  busy,
}: {
  items: Value[];
  onSelect: (index: number) => void;
  busy: boolean;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {items.map((item, i) => (
        <button
          key={String(item.index ?? i)}
          type="button"
          disabled={busy}
          onClick={() => onSelect(Number(item.index ?? i))}
          className="group flex cursor-pointer items-center justify-between gap-3 rounded-[2px] bg-mi-surface px-4 py-3 text-left text-[13px] text-mi-fg shadow-raised transition-all duration-micro ease-mi hover:text-mi-fg-strong hover:shadow-plateau active:translate-x-px active:translate-y-px active:shadow-press disabled:cursor-wait disabled:opacity-50"
        >
          <span className="min-w-0">{String(item.label ?? "")}</span>
          <ArrowRight
            className="h-3.5 w-3.5 shrink-0 transition-transform duration-micro ease-mi group-hover:translate-x-0.5"
            strokeWidth={1.5}
          />
        </button>
      ))}
    </div>
  );
}

/* ─── Property handoff ────────────────────────────────────────────────────── */

/**
 * Shown when discovery is unavailable: MIREYE can analyse a named property even
 * when it cannot enumerate candidates across a region.
 */
export function PropertyHandoff({
  onSubmit,
  busy,
}: {
  onSubmit: (address: string) => void;
  busy: boolean;
}) {
  return (
    <Card depth="raised" className="p-5">
      <Eyebrow>Analyze a real property</Eyebrow>
      <h2 className="mt-1.5 text-[19px] font-medium tracking-tight text-mi-fg-strong">
        Have an address or coordinates?
      </h2>
      <p className="mt-2 max-w-[56ch] text-[12px] leading-relaxed text-mi-fg-muted">
        Use this property to check the request now. A wider search needs a parcel source
        that can enumerate candidates.
      </p>
      <form
        className="mt-4 flex flex-col gap-2 sm:flex-row"
        onSubmit={(event) => {
          event.preventDefault();
          const value = new FormData(event.currentTarget).get("address");
          if (value) onSubmit(String(value));
        }}
      >
        <div className="min-w-0 flex-1">
          <FieldLabel htmlFor="handoff-address">Property address or coordinates</FieldLabel>
          <input
            id="handoff-address"
            name="address"
            required
            placeholder="1 Tesla Road, Austin, TX 78725"
            className={fieldClass}
          />
        </div>
        <PrimaryButton disabled={busy} className="shrink-0 self-end">
          Analyze property
          <ArrowRight className="h-3.5 w-3.5" strokeWidth={1.5} />
        </PrimaryButton>
      </form>
    </Card>
  );
}

/* ─── Spend gate ──────────────────────────────────────────────────────────── */

/**
 * The intake spend gate. Cost is always shown before anything is retrieved, and
 * the assurance line is not optional — it is the product's promise.
 */
export function Confirmation({
  data,
  onConfirm,
  busy,
}: {
  data: Value;
  onConfirm: () => void;
  busy: boolean;
}) {
  const credits = data.estimated_credits;
  const fieldCount = Number(data.field_count);
  return (
    <Card depth="plateau" className="border-t-2 border-t-mi-orange p-5">
      <Eyebrow>Ready to check this property</Eyebrow>
      <h2 className="mt-1.5 text-[19px] font-medium tracking-tight text-mi-fg-strong">
        MIREYE site intelligence
      </h2>
      <p className="mt-3 max-w-[62ch] text-[13px] leading-relaxed text-mi-fg">
        {String(data.location_label ?? "This property")}.{" "}
        {Number.isFinite(fieldCount) ? (
          <>
            <span className="mono-num font-mono text-mi-fg-strong">
              <CountUp to={fieldCount} duration={600} />
            </span>{" "}
            relevant fields will be checked.{" "}
          </>
        ) : null}
        {credits === null || credits === undefined ? (
          "MIREYE will confirm the cost before retrieval."
        ) : (
          <>
            Estimated cost:{" "}
            <span className="mono-num font-mono text-mi-orange-text">
              {String(credits)} credits
            </span>
            .
          </>
        )}
      </p>
      <Ridge className="my-4" />
      <div className="flex flex-wrap items-center gap-4">
        <PrimaryButton onClick={onConfirm} disabled={busy}>
          {busy ? "Checking site…" : "Continue with MIREYE"}
        </PrimaryButton>
        <p className="font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
          Nothing is charged until you confirm
        </p>
      </div>
    </Card>
  );
}

/* ─── Candidate results ───────────────────────────────────────────────────── */

const asVerdict = (outcome: unknown): Verdict => {
  const v = String(outcome ?? "UNRESOLVED").toUpperCase();
  if (v === "PASS") return "PASS";
  if (v === "FAIL" || v === "BLOCKED") return "BLOCKED";
  if (v === "PARTIAL") return "PARTIAL";
  return "UNRESOLVED";
};

/** Mirrors formatDistance() in app/static/app.js. */
function formatDistance(meters: unknown): string {
  const value = Number(meters);
  if (!Number.isFinite(value)) return "Not verified";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} km` : `${Math.round(value)} m`;
}

export function CandidateCard({ candidate, index }: { candidate: Value; index: number }) {
  const checks = arr(candidate.checks);
  const area = Number(candidate.area_acres);
  const sandboxUrl = String(candidate.sandbox_url ?? "");
  const facts: [string, string][] = [
    ["Transmission", formatDistance(candidate.transmission_distance_m)],
    ["Road", formatDistance(candidate.road_distance_m)],
    ["Zoning code", String(candidate.zoning_code || "Unresolved")],
  ];

  return (
    <Card depth="raised" hover className="p-5">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
          Site {index + 1}
        </span>
        <VerdictChip verdict={asVerdict(candidate.overall_status)} />
      </div>

      <div className="mt-3 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-[21px] font-medium leading-tight tracking-tight text-mi-fg-strong">
            {String(candidate.title ?? "Candidate site")}
          </h3>
          <p className="mono-num mt-1 font-mono text-[12px] text-mi-fg-muted">
            {Number.isFinite(area)
              ? `${area.toLocaleString(undefined, { maximumFractionDigits: 2 })} acres`
              : "Area not provided"}
          </p>
        </div>
        {sandboxUrl && (
          <Link
            href={sandboxUrl}
            className="inline-flex shrink-0 cursor-pointer items-center gap-2 rounded-[2px] bg-mi-fg-strong px-4 py-2.5 font-mono text-[11px] uppercase tracking-cite text-mi-bg shadow-raised transition-all duration-micro ease-mi hover:shadow-plateau active:translate-x-px active:translate-y-px active:shadow-press"
          >
            Open site
            <ArrowUpRight className="h-3.5 w-3.5" strokeWidth={1.5} />
          </Link>
        )}
      </div>

      <dl className="mt-4 grid grid-cols-1 gap-px bg-mi-line sm:grid-cols-3">
        {facts.map(([label, value]) => (
          <div key={label} className="bg-mi-surface px-3 py-2.5">
            <dt className="font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
              {label}
            </dt>
            <dd className="mono-num mt-1 font-mono text-[13px] font-medium text-mi-fg-strong">
              {value}
            </dd>
          </div>
        ))}
      </dl>

      <div className="mt-4 space-y-px">
        {(checks.length
          ? checks
          : [
              {
                label: "Site evidence",
                outcome: "UNRESOLVED",
                reason: "No project constraints were supplied.",
              },
            ]
        ).map((check, i) => (
          <div key={`${String(check.label)}-${i}`} className="rounded-[2px] bg-mi-surface-3 p-3 shadow-well">
            <div className="flex items-start justify-between gap-3">
              <span className="text-[12px] font-medium text-mi-fg-strong">
                {String(check.label ?? "")}
              </span>
              <VerdictChip verdict={asVerdict(check.outcome)} />
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-mi-fg-muted">
              {String(check.reason ?? "")}
            </p>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function CandidateResults({ items }: { items: Value[] }) {
  if (!items.length) return null;
  return (
    <div>
      <div className="mb-4 flex items-end justify-between gap-4">
        <div>
          <Eyebrow>Verified property</Eyebrow>
          <h2 className="mt-1.5 text-[clamp(24px,3vw,34px)] font-medium tracking-tight text-mi-fg-strong">
            Candidate sites
          </h2>
        </div>
        <span className="mono-num shrink-0 font-mono text-[11px] text-mi-fg-muted">
          {items.length} result{items.length === 1 ? "" : "s"}
        </span>
      </div>
      <div className="space-y-4">
        {items.map((candidate, index) => (
          <Reveal key={String(candidate.title ?? index)} delay={index * 0.06}>
            <CandidateCard candidate={candidate} index={index} />
          </Reveal>
        ))}
      </div>
    </div>
  );
}

export { rec, arr };
