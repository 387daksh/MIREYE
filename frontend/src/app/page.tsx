"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { ThemeToggle } from "@/components/theme";
import { Reveal } from "@/components/motion/primitives";
import { TerminalLog } from "@/components/motion/terminal-log";
import {
  Brand,
  Eyebrow,
  FieldLabel,
  PrimaryButton,
  Ridge,
  fieldClass,
} from "@/components/product/ui";
import {
  CandidateResults,
  Choices,
  Confirmation,
  Notice,
  PropertyHandoff,
  Stages,
  Understanding,
  arr,
  rec,
  type Tone,
  type Value,
} from "@/components/product/discovery";

// Canvas-backed, touch window — never server-rendered.
const DitherTerrain = dynamic(() => import("@/components/motion/dither-terrain"), {
  ssr: false,
});
const KineticGrid = dynamic(() => import("@/components/ui/kinetic-grid"), { ssr: false });

/** Printed while the project is being created. Loading is a log, not a spinner. */
const CREATING_LOG = [
  "compiling request…",
  "parsing candidate lines",
  "POST /v1/diligence/projects",
  "awaiting project_id…",
];

const EXAMPLES = [
  "Compare 3 sites for a 100 MW data center",
  "Diligence 1 Tesla Road, Austin, TX 78725",
  "Show me what I could build at 38.8977, -77.0365",
  "Find land for a solar farm",
];

/** Notice copy per status, lifted from renderResponse() in app/static/app.js. */
const STATUS_NOTICE: Record<string, { title: string; tone: Tone }> = {
  DISCOVERY_UNAVAILABLE: { title: "Start with a specific property", tone: "warning" },
  MIREYE_UNAVAILABLE: { title: "MIREYE is not connected", tone: "warning" },
  NOT_FOUND: { title: "Property not found", tone: "warning" },
  CLARIFICATION_REQUIRED: { title: "One detail before I continue", tone: "info" },
  COMPLETE: { title: "Site intelligence ready", tone: "success" },
};

/**
 * Project intake.
 *
 * Two paths, both preserved from the original implementation:
 *
 *  - candidate lines supplied → POST /v1/diligence/projects, then straight to
 *    the project workspace.
 *  - no candidates → POST /v1/product/requests and walk the compile → clarify →
 *    confirm → complete machine in place. The confirm step is the intake spend
 *    gate; nothing is retrieved before the user accepts the quote.
 */
export default function Intake() {
  const router = useRouter();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [request, setRequest] = useState<Value>();

  const status = String(request?.status ?? "");
  const notice = STATUS_NOTICE[status];

  /** Every step of the product path funnels its payload through here. */
  function applyResponse(payload: Value) {
    setRequest(payload);
    setError(undefined);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    setRequest(undefined);
    const form = new FormData(event.currentTarget);
    const text = String(form.get("message"));
    const candidates = String(form.get("candidates"))
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);

    // Supplied candidates go straight to a diligence project, as before.
    if (candidates.length) {
      const response = await api.POST("/v1/diligence/projects", {
        body: {
          workspace_id: String(form.get("workspace")),
          message: text,
          candidates: candidates.map((address) => ({ address })),
        },
      });
      setBusy(false);
      if (response.error || !response.data)
        return setError("Project intake failed. Check the API and candidate addresses.");
      router.push(`/projects/${(response.data as { project_id: string }).project_id}`);
      return;
    }

    const response = await api.POST("/v1/product/requests", { body: { message: text } as never });
    setBusy(false);
    if (response.error || !response.data)
      return setError("I couldn't complete that request.");
    applyResponse(response.data as Value);
  }

  async function selectChoice(index: number) {
    if (!request?.request_id) return;
    setBusy(true);
    const response = await api.POST("/v1/product/requests/{request_id}/select", {
      params: { path: { request_id: String(request.request_id) } },
      body: { candidate_index: index } as never,
    });
    setBusy(false);
    if (response.error || !response.data) return setError("I couldn't use that property.");
    applyResponse(response.data as Value);
  }

  /** The spend gate. Nothing is retrieved until this resolves. */
  async function confirmRequest() {
    if (!request?.request_id) return;
    setBusy(true);
    const response = await api.POST("/v1/product/requests/{request_id}/confirm", {
      params: { path: { request_id: String(request.request_id) } },
      body: { confirmed: true } as never,
    });
    setBusy(false);
    if (response.error || !response.data)
      return setError(
        "MIREYE couldn't analyze this property. No existing site data was changed.",
      );
    applyResponse(response.data as Value);
  }

  /** Discovery-unavailable fallback: analyse one named property instead. */
  async function analyzeProperty(address: string) {
    setBusy(true);
    setError(undefined);
    const response = await api.POST("/v1/product/requests", {
      body: { message: `${message.trim()} ${address}`.trim() } as never,
    });
    setBusy(false);
    if (response.error || !response.data) return setError("I couldn't analyze that property.");
    applyResponse(response.data as Value);
  }

  return (
    <KineticGrid className="min-h-screen">
      {/* Scrim only where the type sits. The lattice and terrain stay visible
          at the edges rather than being washed flat across the whole field. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(70%_55%_at_28%_45%,var(--mi-scrim)_0%,transparent_100%)]"
      />
      <DitherTerrain intensity={0.95} />

      <div className="relative flex min-h-screen flex-col">
        <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-mi-line px-[clamp(16px,4vw,40px)]">
          <Brand />
          <div className="flex items-center gap-4">
            <span className="hidden font-mono text-[10px] uppercase tracking-label text-mi-fg-muted sm:inline">
              Physical-world intelligence
            </span>
            <ThemeToggle />
          </div>
        </header>

        <main className={`mx-auto flex w-full max-w-shell flex-1 flex-col px-[clamp(16px,4vw,40px)] py-[clamp(40px,8vh,96px)] ${request ? "justify-start" : "justify-center"}`}>
          <div className="grid gap-[clamp(32px,6vw,80px)] lg:grid-cols-[1fr_minmax(400px,540px)] lg:items-start">
            {/* ── The ask ────────────────────────────────────────────────── */}
            <div className="lg:pt-4">
              <Reveal>
                <p className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-label text-mi-fg-muted">
                  <span aria-hidden className="h-1.5 w-1.5 bg-mi-orange" />
                  New project
                </p>
              </Reveal>
              <Reveal delay={0.06}>
                <h1 className="mt-4 max-w-[14ch] text-[clamp(40px,5.5vw,68px)] font-medium leading-[0.98] tracking-display text-mi-fg-strong">
                  Understand a site before you commit.
                </h1>
              </Reveal>
              <Reveal delay={0.12}>
                <p className="mt-6 max-w-[44ch] text-[15px] leading-relaxed text-mi-fg-muted">
                  Build an evidence-backed view of land, infrastructure, readiness, and the
                  decisions still ahead.
                </p>
              </Reveal>

              <Reveal delay={0.18}>
                <div className="mt-10 border-t border-mi-line pt-5">
                  <Eyebrow className="mb-3">Try</Eyebrow>
                  <div className="flex flex-wrap gap-2">
                    {EXAMPLES.map((example, index) => (
                      <button
                        key={example}
                        type="button"
                        onClick={() => setMessage(example)}
                        className="group relative cursor-pointer rounded-[2px] bg-mi-surface px-3 py-2 text-left text-[12px] text-mi-fg-muted shadow-raised transition-all duration-micro ease-mi hover:text-mi-fg-strong hover:shadow-plateau active:translate-x-px active:translate-y-px active:shadow-press"
                      >
                        <span className="mr-2 font-mono text-[10px] text-mi-fg-muted/60">
                          {String(index + 1).padStart(2, "0")}
                        </span>
                        {example}
                        <span
                          aria-hidden
                          className="absolute inset-x-3 bottom-1.5 h-px origin-left scale-x-0 bg-mi-orange transition-transform duration-micro ease-mi group-hover:scale-x-100"
                        />
                      </button>
                    ))}
                  </div>
                </div>
              </Reveal>
            </div>

            {/* ── The form ───────────────────────────────────────────────── */}
            <Reveal delay={0.1}>
            <form
              onSubmit={submit}
              className="rounded-[2px] bg-mi-surface shadow-plateau"
            >
              <div className="flex items-center justify-between gap-3 px-5 py-4">
                <Eyebrow>Project intake</Eyebrow>
                <span className="font-mono text-[10px] text-mi-fg-muted">
                  POST /v1/diligence/projects
                </span>
              </div>

              <div className="space-y-5 px-5 py-5">
                <div>
                  <FieldLabel htmlFor="workspace">Workspace</FieldLabel>
                  <input
                    id="workspace"
                    name="workspace"
                    required
                    defaultValue="default"
                    aria-label="Workspace"
                    className={`${fieldClass} font-mono`}
                  />
                </div>

                <div>
                  <FieldLabel htmlFor="message">What are you evaluating?</FieldLabel>
                  <textarea
                    id="message"
                    name="message"
                    required
                    rows={4}
                    value={message}
                    onChange={(event) => setMessage(event.target.value)}
                    placeholder="Describe the project and constraints"
                    className={`${fieldClass} resize-y`}
                  />
                </div>

                <div>
                  <FieldLabel htmlFor="candidates">Candidate properties</FieldLabel>
                  <textarea
                    id="candidates"
                    name="candidates"
                    rows={5}
                    placeholder={"1 Broker Road, Dallas, TX\n32.7767, -96.7970\nAPN: 123-456-789"}
                    className={`${fieldClass} resize-y font-mono text-[12px] leading-[1.9]`}
                  />
                  <p className="mt-2 font-mono text-[10px] text-mi-fg-muted">
                    Optional · one address, coordinate, or APN per line
                  </p>
                </div>
              </div>

              {/* Submitting prints a log rather than swapping in a spinner. */}
              {busy && (
                <div className="px-5 py-4">
                  <Ridge className="mb-4" />
                  <TerminalLog lines={CREATING_LOG} lineDelay={260} loop />
                </div>
              )}

              <div className="flex items-center justify-between gap-4 px-5 pb-5 pt-1">
                <span className="inline-flex items-center gap-2 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
                  <span
                    aria-hidden
                    className={`h-1.5 w-1.5 bg-mi-orange ${busy ? "animate-caret" : ""}`}
                  />
                  MIREYE site intelligence
                </span>
                <PrimaryButton disabled={busy}>
                  {busy ? "Working…" : "Run"}
                  {!busy && <ArrowRight className="h-3.5 w-3.5" strokeWidth={1.5} />}
                </PrimaryButton>
              </div>

              {error && (
                <p
                  role="alert"
                  className="mx-5 mb-5 rounded-[2px] bg-mi-surface-3 px-4 py-3 text-[12px] text-mi-orange-text shadow-well"
                >
                  {error}
                </p>
              )}
            </form>
            </Reveal>
          </div>

          {/* ── Work section ──────────────────────────────────────────────
              Everything below appears only once a product request exists. */}
          {request && (
            <section aria-live="polite" className="mt-[clamp(40px,7vh,80px)] space-y-8">
              <Understanding items={(request.understanding as string[] | undefined) ?? []} />

              <Stages stages={arr(request.stages)} />

              {notice && (
                <Notice
                  title={notice.title}
                  message={String(request.message ?? "")}
                  tone={notice.tone}
                />
              )}

              {status === "DISCOVERY_UNAVAILABLE" && (
                <PropertyHandoff onSubmit={analyzeProperty} busy={busy} />
              )}

              {status === "CLARIFICATION_REQUIRED" && arr(request.choices).length > 0 && (
                <Choices items={arr(request.choices)} onSelect={selectChoice} busy={busy} />
              )}

              {status === "CONFIRMATION_REQUIRED" && (
                <Confirmation
                  data={rec(request.confirmation)}
                  onConfirm={confirmRequest}
                  busy={busy}
                />
              )}

              {status === "COMPLETE" && <CandidateResults items={arr(request.candidates)} />}
            </section>
          )}
        </main>

        <footer className="flex shrink-0 flex-col gap-2 border-t border-mi-line px-[clamp(16px,4vw,40px)] py-5 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted sm:flex-row sm:items-center sm:justify-between">
          <span>Observed facts stay separate from proposed designs</span>
          <span>PASS / FAIL / UNRESOLVED comes from deterministic evaluation</span>
        </footer>
      </div>
    </KineticGrid>
  );
}
