export type OrchestrationStage = "UNDERSTANDING" | "INVESTIGATING" | "VERIFYING" | "WAITING FOR YOU" | "RESUMING" | "COMPLETED" | "FAILED" | "CANCELLED";

export function orchestrationStage(status?: unknown, eventType?: unknown): OrchestrationStage {
  const value = String(eventType ?? status ?? "RUNNING");
  if (value === "COMPLETED") return "COMPLETED";
  if (value === "FAILED") return "FAILED";
  if (value === "CANCELLED") return "CANCELLED";
  if (value === "WAITING_FOR_DECISION" || value === "NEEDS_USER_DECISION") return "WAITING FOR YOU";
  if (value === "RESUMED") return "RESUMING";
  if (value === "VERIFICATION" || value === "REPLAN") return "VERIFYING";
  if (value.startsWith("TASK_") || value === "EVIDENCE_FOUND") return "INVESTIGATING";
  return "UNDERSTANDING";
}
