import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

/**
 * Mireye brand theme.
 *
 * The palette is closed: black, white, chalk, grey, orange. Nothing else.
 * See design-system/mireye/MASTER.md — orange is a signal, never a surface.
 *
 * NOTE: the existing product app (`/intake`, `/projects/[id]`) is styled by the
 * hand-written CSS in globals.css and does not consume these tokens. Adding them
 * here is additive and does not affect that surface.
 */
export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        mi: {
          black: "#000000",
          white: "#FFFFFF",
          chalk: "#F4F4F4",
          grey: "#71717A",
          orange: "#FF6600",
          bg: "var(--mi-bg)",
          surface: "var(--mi-surface)",
          "surface-2": "var(--mi-surface-2)",
          "surface-3": "var(--mi-surface-3)",
          fg: "var(--mi-fg)",
          "fg-strong": "var(--mi-fg-strong)",
          // Themed. Never use raw #71717A for text — it fails 4.5:1 on both
          // Jet Black and Chalk White.
          "fg-muted": "var(--mi-fg-muted)",
          line: "var(--mi-border)",
          "line-strong": "var(--mi-border-2)",
          // Orange as TEXT. Darkens in light mode to clear 4.5:1.
          "orange-text": "var(--mi-orange-text)",
          // Provenance semantics: what is real, what is computed, what is a design.
          observed: "var(--mi-observed)",
          derived: "var(--mi-derived)",
          proposed: "var(--mi-proposed)",
        },
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-geist-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      borderRadius: {
        // Effectively square. The logo is hard-edged; the UI matches.
        none: "0px",
        sm: "1px",
        DEFAULT: "2px",
        md: "2px",
        lg: "3px",
      },
      boxShadow: {
        // Soft-UI depth. Achromatic only, no hue, no glow, no translucency —
        // see the brand kit. Themed so light mode is not black-on-black.
        raised: "var(--mi-shadow-raised)",
        plateau: "var(--mi-shadow-plateau)",
        well: "var(--mi-shadow-well)",
        press: "var(--mi-shadow-press)",
        panel: "var(--mi-shadow-panel)",
        hairline: "0 0 0 1px var(--mi-border)",
      },
      letterSpacing: {
        display: "-0.045em",
        tight: "-0.03em",
        label: "0.14em",
        cite: "0.08em",
      },
      transitionTimingFunction: {
        // Mechanical. No overshoot.
        mi: "cubic-bezier(0.2, 0, 0, 1)",
      },
      transitionDuration: {
        micro: "150ms",
        reveal: "260ms",
        section: "350ms",
      },
      maxWidth: {
        shell: "1280px",
      },
      keyframes: {
        "mi-marquee": {
          from: { transform: "translateX(0)" },
          to: { transform: "translateX(-50%)" },
        },
        "mi-caret": {
          "0%, 49%": { opacity: "1" },
          "50%, 100%": { opacity: "0" },
        },
        "mi-scan": {
          from: { transform: "translateY(-100%)" },
          to: { transform: "translateY(100%)" },
        },
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
      },
      animation: {
        marquee: "mi-marquee 42s linear infinite",
        caret: "mi-caret 1s steps(1) infinite",
        scan: "mi-scan 4s linear infinite",
        "accordion-down": "accordion-down 260ms cubic-bezier(0.2,0,0,1)",
        "accordion-up": "accordion-up 260ms cubic-bezier(0.2,0,0,1)",
      },
    },
  },
  plugins: [animate],
} satisfies Config;
