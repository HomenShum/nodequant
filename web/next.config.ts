import { withAui } from "@assistant-ui/next";
import type { NextConfig } from "next";

const BACKEND = (
  process.env.TRIALSCOPE_BACKEND_URL ?? "http://localhost:8000/api/chat"
).replace(/\/api\/chat$/, "");

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      // The neuromap iframe loads Cytoscape with a RELATIVE src, which resolves
      // against this origin (:3000), not the backend (:8000) that serves the
      // vendored bundle. Without this rewrite the script 404s and the iframe
      // renders an empty graph container -- no console error loud enough to
      // notice, just a blank box where the map should be.
      //
      // A rewrite rather than an absolute backend URL baked into the HTML: the
      // iframe is sandboxed without allow-same-origin, so an absolute
      // cross-origin script is one more thing a policy nobody remembers setting
      // can block. Same-origin keeps it boring.
      { source: "/vendor/:path*", destination: `${BACKEND}/vendor/:path*` },
    ];
  },
};

export default withAui(nextConfig);
