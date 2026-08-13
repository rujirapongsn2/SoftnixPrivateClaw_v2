import { defineTheme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";

// "ClawThai" (self-hosted Noto Sans Thai, see @font-face in styles.css) is
// FIRST so Thai codepoints render with it; its unicode-range is limited to
// the Thai block, so Latin/other codepoints skip it and use system-ui —
// matching ChatGPT's Latin look while guaranteeing correct Thai shaping,
// notably in Safari where the OS's system-ui Thai substitution mis-renders
// inside a contenteditable.
// OA-aligned font stack: ClawThai (Thai block only) first, then the platform UI
// font. FT Kunst Grotesk is proprietary — system-ui (SF Pro / Segoe UI Variable)
// is the closest free match. `system-ui` stays FIRST: putting the literal
// "Segoe UI" ahead of it pins Windows to the legacy static face instead of
// Segoe UI Variable, which system-ui resolves to.
const FONT_FALLBACKS = 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

export const clawTheme = defineTheme({
  name: "claw",
  extends: neutralTheme,
  typography: {
    // 16px body — matches OA/Kumo base size (1rem at 100% root).
    scale: { base: 16, ratio: 1.2 },
    body: {
      family: "ClawThai",
      fallbacks: FONT_FALLBACKS,
      weight: "normal", // 400
    },
    heading: {
      family: "ClawThai",
      fallbacks: FONT_FALLBACKS,
      weight: "semibold", // 600; largest headings go bold (700) below
      weights: { 1: "bold" },
    },
  },
  tokens: {
    // Thai vowel/tone marks need more vertical room — 1.65 matches OA's Thai rendering.
    "--text-body-leading": "1.65",
    "--size-element-sm": "30px",
    "--size-element-md": "34px",
    "--size-element-lg": "38px",
  },
});
