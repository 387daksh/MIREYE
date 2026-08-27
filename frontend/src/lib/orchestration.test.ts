import { describe, expect, it } from "vitest";
import { orchestrationStage } from "./orchestration";

describe("orchestrationStage", () => {
  it("uses only persisted status and event values", () => {
    expect(orchestrationStage("WAITING_FOR_DECISION")).toBe("WAITING FOR YOU");
    expect(orchestrationStage("CANCELLED")).toBe("CANCELLED");
    expect(orchestrationStage("RUNNING", "VERIFICATION")).toBe("VERIFYING");
    expect(orchestrationStage("COMPLETED")).toBe("COMPLETED");
  });
});
