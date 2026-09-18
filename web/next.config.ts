import type { NextConfig } from "next";

// The desktop workbench runs through `next start`; no container-only standalone
// bundle is produced. Keeping one production artifact avoids a green build that
// cannot be launched by the documented command.
const nextConfig: NextConfig = {};

export default nextConfig;
