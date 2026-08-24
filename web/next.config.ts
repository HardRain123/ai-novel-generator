import type { NextConfig } from "next";

const isDesktop = process.env.NEXT_DESKTOP === "1";
const nextConfig: NextConfig = isDesktop
  ? {
      output: "export",
      assetPrefix: ".",
      trailingSlash: true,
      images: { unoptimized: true },
    }
  : { output: "standalone" };

export default nextConfig;

