/**
 * Statuses returned by /v1/product/requests and its select/confirm steps.
 *
 * Lives in lib rather than beside the components so the parity test can import
 * it without pulling in React, icons, or motion.
 */
export const REQUEST_STATUSES = [
  "DISCOVERY_UNAVAILABLE",
  "MIREYE_UNAVAILABLE",
  "NOT_FOUND",
  "CLARIFICATION_REQUIRED",
  "CONFIRMATION_REQUIRED",
  "COMPLETE",
] as const;

export type RequestStatus = (typeof REQUEST_STATUSES)[number];
