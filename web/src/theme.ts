import { defineTheme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";

// Base body text at 18px (up from the default 14px) with the same 1.2 ratio,
// plus a larger spacing/control scale — comfortable defaults for readers of
// all ages rather than the design system's dense default sizing.
export const clawTheme = defineTheme({
  name: "claw",
  extends: neutralTheme,
  typography: {
    scale: { base: 18, ratio: 1.2 },
  },
  radius: {
    base: 4,
    multiplier: 1.15,
  },
  tokens: {
    "--spacing-0-5": "3px",
    "--spacing-1": "5px",
    "--spacing-1-5": "8px",
    "--spacing-2": "10px",
    "--spacing-3": "15px",
    "--spacing-4": "20px",
    "--spacing-5": "25px",
    "--spacing-6": "30px",
    "--spacing-7": "35px",
    "--spacing-8": "40px",
    "--spacing-9": "45px",
    "--spacing-10": "50px",
    "--spacing-11": "55px",
    "--spacing-12": "60px",
    "--size-element-sm": "34px",
    "--size-element-md": "40px",
    "--size-element-lg": "44px",
  },
});
