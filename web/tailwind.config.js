/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#05070b",
          900: "#0a0e14",
          800: "#121821",
          700: "#1a2330",
          600: "#243044",
        },
        mist: {
          100: "#e8eef5",
          200: "#c5d0de",
          300: "#8b9bb0",
          400: "#5c6d82",
        },
        signal: {
          DEFAULT: "#3ddea8",
          dim: "#1fa878",
          glow: "rgba(61, 222, 168, 0.35)",
        },
        warn: "#f0a050",
      },
      fontFamily: {
        display: ["var(--font-display)", "system-ui", "sans-serif"],
        body: ["var(--font-body)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      boxShadow: {
        card: "0 0 0 1px rgba(61,222,168,0.08), 0 18px 48px rgba(0,0,0,0.45)",
        glow: "0 0 40px rgba(61,222,168,0.12)",
      },
      keyframes: {
        rise: {
          from: { opacity: "0", transform: "translateY(12px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.55" },
        },
      },
      animation: {
        rise: "rise 0.65s ease both",
        "rise-delay": "rise 0.75s ease 0.08s both",
        pulseSoft: "pulseSoft 2.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
