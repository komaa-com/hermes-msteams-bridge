// Docs site for hermes-msteams-bridge, published to GitHub Pages at
// https://komaa-com.github.io/hermes-msteams-bridge/ by .github/workflows/docs.yml.
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";
import mermaid from "astro-mermaid";

export default defineConfig({
  site: "https://komaa-com.github.io",
  base: "/hermes-msteams-bridge",
  integrations: [
    // Client-side Mermaid rendering (theme-aware, offline). Must come BEFORE starlight.
    mermaid({ theme: "default", autoTheme: true }),
    starlight({
      head: [
        // Google Analytics 4 (shared StandIn property; filter by hostname in GA).
        { tag: "script", attrs: { async: true, src: "https://www.googletagmanager.com/gtag/js?id=G-M02N9C42XH" } },
        {
          tag: "script",
          content:
            "window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-M02N9C42XH');",
        },
      ],
      title: "Microsoft Teams Bridge for Hermes Agent",
      description:
        "Microsoft Teams voice and video for a Hermes AI agent: realtime speech-to-speech, vision, avatar cues, meetings, and outbound call-backs via the StandIn media bridge.",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/komaa-com/hermes-msteams-bridge",
        },
      ],
      sidebar: [
        { label: "Overview", slug: "" },
        { label: "Getting Started", slug: "getting-started" },
        { label: "Connecting to StandIn", slug: "connecting-to-standin" },
        { label: "Architecture", slug: "architecture" },
        { label: "Configuration Reference", slug: "configuration-reference" },
        { label: "Voice Modes & Providers", slug: "voice-modes-and-providers" },
        { label: "Wire Protocol", slug: "wire-protocol" },
        { label: "Features", slug: "features" },
        { label: "Outbound Calls", slug: "outbound-calls" },
        { label: "Troubleshooting", slug: "troubleshooting" },
        { label: "Contributing", slug: "contributing" },
      ],
    }),
  ],
});
