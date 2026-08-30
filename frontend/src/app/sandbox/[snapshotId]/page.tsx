"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { TerminalLog } from "@/components/motion/terminal-log";
import { ThemeToggle } from "@/components/theme";
import { Brand } from "@/components/product/ui";
import { SandboxPanel } from "@/app/projects/[projectId]/sandbox-panel";
import { SandboxChat } from "@/app/projects/[projectId]/sandbox-chat";
import { SiteProvider, rec, useSite } from "@/app/projects/[projectId]/site-context";
import { SiteSections } from "@/app/projects/[projectId]/site-rail";

/**
 * Standalone site workspace — the Next.js equivalent of app/static/sandbox.html.
 *
 * This is where every `sandbox_url` the backend builds points to; the API
 * returns `/sandbox/{snapshot_id}?world={world_snapshot_id}` from five separate
 * places, so the route has to exist under exactly that shape.
 *
 * It is keyed to a SiteSnapshot, not a project: no orchestration runs, no
 * project readiness, no candidate list. Just the authoritative site, the scene,
 * and everything derived from them.
 */
export default function SandboxPage() {
  return (
    <Suspense fallback={<SandboxLoading />}>
      <SandboxWorkspace />
    </Suspense>
  );
}

const LOADING_LOG = [
  "resolving snapshot_id…",
  "loading authoritative scene",
  "pinning world snapshot",
  "reading evidence plan",
];

function SandboxLoading() {
  return (
    <main className="grid min-h-screen place-items-center bg-mi-bg px-6">
      <div className="w-full max-w-[320px]">
        <Brand className="mb-6" />
        <TerminalLog lines={LOADING_LOG} lineDelay={280} loop />
      </div>
    </main>
  );
}

function SandboxWorkspace() {
  const { snapshotId } = useParams<{ snapshotId: string }>();
  // The world snapshot rides along as a query param, exactly as the backend
  // writes it. Absent is fine — SiteProvider will prepare one.
  const worldId = useSearchParams().get("world") ?? undefined;

  return (
    <SiteProvider snapshotId={snapshotId} worldId={worldId}>
      <div className="flex min-h-[100svh] flex-col bg-mi-bg lg:h-[100svh] lg:min-h-[640px] lg:overflow-hidden">
        <SandboxHeader />
        <div className="grid grid-cols-1 lg:min-h-0 lg:flex-1 lg:grid-cols-[minmax(0,1fr)_minmax(340px,400px)] lg:overflow-hidden">
          <section
            className="relative min-w-0 overflow-hidden border-mi-line bg-mi-surface-2 lg:border-r"
            aria-label="Physical-world workspace"
          >
            <div className="relative flex h-[70svh] min-h-[460px] flex-col lg:h-full lg:min-h-0">
              <SandboxPanel />
              <SandboxChat />
            </div>
          </section>
          <aside
            className="mi-scroll min-h-0 overflow-y-auto bg-mi-surface"
            aria-label="Site intelligence"
          >
            <SiteSections />
          </aside>
        </div>
      </div>
    </SiteProvider>
  );
}

/** Mirrors the vanilla sandbox header: brand, parcel identity, new search. */
function SandboxHeader() {
  const { snapshot } = useSite();
  const identity = rec(snapshot?.identity);
  const title = String(identity.parcel_address ?? identity.parcel_id ?? "Loading site");
  // is_expired is the same flag the vanilla read for this line.
  const state =
    snapshot === undefined
      ? "Checking site intelligence"
      : snapshot.is_expired
        ? "Site intelligence needs an update"
        : "MIREYE intelligence current";

  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-mi-line bg-mi-surface px-4 md:px-6">
      <Link href="/" className="shrink-0" aria-label="MIREYE home">
        <Brand />
      </Link>

      <div className="mx-auto hidden min-w-0 flex-col items-center md:flex">
        <strong className="truncate text-[13px] font-medium text-mi-fg-strong">{title}</strong>
        <span className="font-mono text-[10px] text-mi-fg-muted">{state}</span>
      </div>

      <nav className="ml-auto flex items-center gap-3 md:ml-0" aria-label="Workspace controls">
        <Link
          href="/"
          className="group relative cursor-pointer py-1 font-mono text-[11px] uppercase tracking-cite text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
        >
          New search
          <span
            aria-hidden
            className="absolute inset-x-0 bottom-0 h-px origin-left scale-x-0 bg-mi-orange transition-transform duration-micro ease-mi group-hover:scale-x-100"
          />
        </Link>
        <ThemeToggle />
      </nav>
    </header>
  );
}
