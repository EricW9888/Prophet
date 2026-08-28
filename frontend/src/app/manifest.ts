import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/",
    name: "Prophet Investment Intelligence",
    short_name: "Prophet",
    description:
      "A personal investment intelligence system for portfolio-aware research, monitoring, review, and simulation.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#09090b",
    theme_color: "#0f172a",
    categories: ["finance", "productivity"],
    icons: [
      {
        src: "/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
    ],
    shortcuts: [
      { name: "Portfolio", short_name: "Portfolio", url: "/" },
      { name: "Research", short_name: "Research", url: "/chat" },
      { name: "Monitor", short_name: "Monitor", url: "/timeline" },
      { name: "Review", short_name: "Review", url: "/verification" },
    ],
  };
}
