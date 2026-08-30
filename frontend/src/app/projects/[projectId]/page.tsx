"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api } from "@/lib/api";
import { TerminalLog } from "@/components/motion/terminal-log";
import { Brand } from "@/components/product/ui";
import { OrchestrationPanel } from "./orchestration-panel";
import {
  IntelligencePanel,
  MapWorkspace,
  MireyeHeader,
  ProjectHeader,
  Value,
  list,
  record,
} from "./product-components";
import { SandboxPanel } from "./sandbox-panel";
import { SiteProvider } from "./site-context";
import { SiteSections } from "./site-rail";
import { ProjectSections } from "./project-rail";

const LOADING_LOG = [
  "resolving project…",
  "reading orchestration runs",
  "reconciling candidates",
  "loading site intelligence",
];

export default function ProjectPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: async () =>
      api.GET("/v1/diligence/projects/{project_id}", {
        params: { path: { project_id: projectId } },
      }),
  });
  const state = project.data?.data as Value | undefined;

  if (project.isLoading)
    return (
      <main className="grid min-h-screen place-items-center bg-mi-bg px-6">
        <div className="w-full max-w-[320px]">
          <Brand className="mb-6" />
          {/* Loading states are logs, not spinners. */}
          <TerminalLog lines={LOADING_LOG} lineDelay={280} loop />
        </div>
      </main>
    );

  if (!state)
    return (
      <main className="grid min-h-screen place-items-center bg-mi-bg px-6">
        <div className="w-full max-w-[380px] border border-mi-line bg-mi-surface p-6 shadow-raised">
          <Brand className="mb-5" />
          <p className="text-[13px] leading-relaxed text-mi-fg">
            Project intelligence could not be loaded.
          </p>
          <Link
            href="/"
            className="mt-5 inline-block cursor-pointer border-b border-mi-orange pb-0.5 font-mono text-[11px] uppercase tracking-cite text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
          >
            Return to search
          </Link>
        </div>
      </main>
    );

  const intelligence = record(state.project_intelligence);
  const projectName = String(
    record(intelligence.active_site).title ?? record(state.request).project ?? "MIREYE project",
  );
  const runs = list(state.orchestration_runs);
  const candidates = list(state.candidates);

  // Snapshot selection moved up out of SandboxPanel so the map and the rail
  // resolve the same SiteSnapshot. Selection rule itself is unchanged.
  const selected = candidates.find((candidate) => candidate.snapshot_id);
  const snapshotId = selected?.snapshot_id ? String(selected.snapshot_id) : undefined;
  const sandboxUrl = record(selected?.summary).sandbox_url;
  const worldId =
    typeof sandboxUrl === "string"
      ? new URL(sandboxUrl, "http://local").searchParams.get("world") ?? undefined
      : undefined;

  return (
    // Desktop pins the whole workspace to the viewport; below lg the page
    // scrolls normally so the map row is not squeezed by the grid.
    <div className="flex min-h-[100svh] flex-col bg-mi-bg lg:h-[100svh] lg:min-h-[640px] lg:overflow-hidden">
      <MireyeHeader
        projectName={projectName}
        workspace={state.workspace_id ? String(state.workspace_id) : undefined}
      />
      <ProjectHeader state={state} />

      {/* Map and rail sit side by side on desktop; the rail stacks under the
          map on narrow screens so neither surface gets squeezed. */}
      <SiteProvider snapshotId={snapshotId} worldId={worldId}>
        <div className="grid grid-cols-1 lg:min-h-0 lg:flex-1 lg:grid-cols-[minmax(0,1fr)_minmax(340px,400px)] lg:overflow-hidden">
          <MapWorkspace>
            <div className="relative flex h-[70svh] min-h-[460px] flex-col lg:h-full lg:min-h-0">
              <SandboxPanel
                workspaceId={state.workspace_id ? String(state.workspace_id) : undefined}
              />
              <OrchestrationPanel
                projectId={projectId}
                runs={runs}
                activeDecision={record(state.active_decision)}
              />
            </div>
          </MapWorkspace>
          <IntelligencePanel state={state}>
            <ProjectSections state={state} projectId={projectId} />
            <SiteSections
              workspaceId={state.workspace_id ? String(state.workspace_id) : undefined}
            />
          </IntelligencePanel>
        </div>
      </SiteProvider>
    </div>
  );
}
