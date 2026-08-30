import type { NextConfig } from "next";
// agentRules: the repo already carries its own AGENTS.md at the root; opt out of
// Next's generated frontend/AGENTS.md + CLAUDE.md so they don't compete with it.
const nextConfig: NextConfig = { output: "standalone", agentRules: false };
export default nextConfig;
