"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api } from "../lib/api";

export default function Intake() {
  const router = useRouter();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    const form = new FormData(event.currentTarget);
    const candidates = String(form.get("candidates")).split("\n").filter(Boolean).map((address) => ({ address }));
    const response = await api.POST("/v1/diligence/projects", { body: { workspace_id: String(form.get("workspace")), message: String(form.get("message")), candidates } });
    setBusy(false);
    if (response.error || !response.data) return setError("Project intake failed. Check the API and candidate addresses.");
    router.push(`/projects/${(response.data as { project_id: string }).project_id}`);
  }

  return <main className="intake-page">
    <header className="intake-brand"><span className="brand-mark"/><strong>MIREYE</strong><span>Physical-world intelligence</span></header>
    <section className="intake-layout">
      <div className="intake-intro"><span className="eyebrow">New project</span><h1>Understand a site before you commit.</h1><p>Build an evidence-backed view of land, infrastructure, readiness, and the decisions still ahead.</p></div>
      <form onSubmit={submit} className="intake-form">
        <label><span>Workspace</span><input name="workspace" required defaultValue="default" aria-label="Workspace" /></label>
        <label><span>What are you evaluating?</span><textarea name="message" required placeholder="Describe the project and constraints" rows={4}/></label>
        <label><span>Candidate sites</span><textarea name="candidates" required placeholder="One real candidate address or coordinate per line" rows={5}/></label>
        <button className="primary-action" disabled={busy}>{busy ? "Creating…" : "Create project"}</button>
        {error && <p role="alert" className="form-error">{error}</p>}
      </form>
    </section>
  </main>;
}
