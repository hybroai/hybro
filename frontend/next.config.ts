import type { NextConfig } from "next";
import { parsePublicConfig } from './src/lib/config-schema'
import { version } from './package.json'

// Server-side rewrites are compiled at build time, so routing needs a
// projection for `npm run dev`. The image builds without one: Caddy routes the
// API there, so no rewrite is compiled.
// Browser settings are NOT build input: the server serves the deployment's
// projection per request (src/app/hybro-runtime-config/route.ts) and the layout
// loads it before application code. Publishing those values through `env`
// would inline them into every bundle — including the serving route — and pin
// one image to one deployment's settings.
const serializedConfig = process.env.HYBRO_FRONTEND_CONFIG
const publicConfig = serializedConfig ? parsePublicConfig(serializedConfig) : null

const nextConfig: NextConfig = {
  // The image carries only the traced server and static files.
  output: 'standalone',
  // Build metadata, inlined as a constant; importing package.json in client code
  // would ship the whole file.
  env: {
    HYBRO_APP_VERSION: version,
  },
  experimental: {
    prefetchInlining: true,
  },
  compiler: {
    removeConsole: {
      exclude: ['error', 'warn'],
    },
  },
  async rewrites() {
    if (publicConfig === null) return [];
    // Allows seamless API proxying without CORS issues.
    // `npm run dev` proxies to a backend on the host; HYBRO_BACKEND_URL overrides it.
    const backendUrl = process.env.HYBRO_BACKEND_URL || 'http://127.0.0.1:8000';
    const configuredPrefix = publicConfig.api_prefix;
    const apiPrefix = `/${configuredPrefix.replace(/^\/+|\/+$/g, '')}`;
    // The Connect page shows `{origin}/mcp`; in development MCP runs natively on 8001.
    const mcpUrl = 'http://127.0.0.1:8001'
    return [
      {
        source: `${apiPrefix}/:path*`,
        destination: `${backendUrl}${apiPrefix}/:path*`,
      },
      { source: '/mcp', destination: `${mcpUrl}/mcp` },
      { source: '/mcp/:path*', destination: `${mcpUrl}/mcp/:path*` },
    ];
  },
};

export default nextConfig;
