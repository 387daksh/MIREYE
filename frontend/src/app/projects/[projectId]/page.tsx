"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api } from "../../../lib/api";
import { OrchestrationPanel } from "./orchestration-panel";
import { IntelligencePanel, MapWorkspace, MireyeHeader, ProjectHeader, Value, list, record } from "./product-components";
import { SandboxPanel } from "./sandbox-panel";

export default function ProjectPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: async () => api.GET("/v1/diligence/projects/{project_id}", { params: { path: { project_id: projectId } } }),
  });
  const state = project.data?.data as Value | undefined;
  if (project.isLoading) return <main className="loading-screen"><span className="brand-mark"/> Loading MIREYE…</main>;
  if (!state) return <main className="loading-screen"><p>Project intelligence could not be loaded.</p><Link href="/">Return to search</Link></main>;
  const intelligence = record(state.project_intelligence);
  const projectName = String(record(intelligence.active_site).title ?? record(state.request).project ?? "MIREYE project");
  const runs = list(state.orchestration_runs);
  const candidates = list(state.candidates);
  return <div className="product-shell">
    <MireyeHeader projectName={projectName} workspace={state.workspace_id ? String(state.workspace_id) : undefined}/>
    <ProjectHeader state={state}/>
    <div className="workspace-grid">
      <MapWorkspace>
        <SandboxPanel projectId={projectId} workspaceId={state.workspace_id ? String(state.workspace_id) : undefined} candidates={candidates}/>
        <OrchestrationPanel projectId={projectId} runs={runs} activeDecision={record(state.active_decision)}/>
      </MapWorkspace>
      <IntelligencePanel state={state}/>
    </div>
  </div>;
}
