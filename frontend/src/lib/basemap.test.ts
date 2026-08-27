import { describe, expect, it } from "vitest";
import { basemapStyle, emptyBasemapStyle } from "./basemap";

describe("basemap selection", () => {
  it("keeps authoritative layers usable without an external basemap", () => {
    expect(basemapStyle()).toEqual(emptyBasemapStyle);
  });

  it("uses only explicitly configured sources", () => {
    expect(basemapStyle("/styles/primary.json", "/styles/fallback.json")).toBe("/styles/primary.json");
    expect(basemapStyle(undefined, "/styles/fallback.json")).toBe("/styles/fallback.json");
  });
});
