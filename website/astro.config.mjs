// Docs site for hermes-plugin-teams-voice, published to GitHub Pages at
// https://komaa-com.github.io/hermes-plugin-teams-voice/ by .github/workflows/docs.yml.
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

export default defineConfig({
  site: "https://komaa-com.github.io",
  base: "/hermes-plugin-teams-voice",
  integrations: [
    starlight({
      title: "MS Teams Voice for Hermes",
      description:
        "Microsoft Teams voice and video for a Hermes AI agent: realtime speech-to-speech, vision, avatar cues, meetings, and outbound call-backs via the StandIn media bridge.",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/komaa-com/hermes-plugin-teams-voice",
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
