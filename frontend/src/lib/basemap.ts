export const emptyBasemapStyle = {
  version: 8 as const,
  sources: {},
  layers: [{ id: "map-background", type: "background" as const, paint: { "background-color": "#d9dfda" } }],
};

/**
 * `background` only applies to the no-basemap fallback, where MIREYE draws the
 * authoritative layers over a flat field. It exists so that field can follow the
 * app theme instead of being a light grey slab behind a dark UI. Configured
 * basemaps are returned untouched.
 */
export function basemapStyle(primary?: string, fallback?: string, background?: string) {
  if (primary || fallback) return primary || fallback;
  if (!background) return emptyBasemapStyle;
  return {
    ...emptyBasemapStyle,
    layers: [{ ...emptyBasemapStyle.layers[0], paint: { "background-color": background } }],
  };
}
