export const emptyBasemapStyle = {
  version: 8 as const,
  sources: {},
  layers: [{ id: "map-background", type: "background" as const, paint: { "background-color": "#d9dfda" } }],
};

export function basemapStyle(primary?: string, fallback?: string) {
  return primary || fallback || emptyBasemapStyle;
}
