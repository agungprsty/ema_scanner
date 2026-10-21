/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/pages/**/*.{html,js}",
    "./src/static/**/*.{html,js}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: '#0f1012',
        surface: {
          DEFAULT: '#151619',
          2: '#1b1c20',
        },
        line: {
          DEFAULT: '#25272c',
          strong: '#34373d',
        },
        brand: {
          text: '#e6e7e9',
          muted: '#a3a6ad',
          faint: '#858991', // 4.5:1 on surface-2; #6f737b failed WCAG AA
        },
        trade: {
          long: '#3fb68b',
          'long-bg': 'rgba(63, 182, 139, 0.12)',
          short: '#e5605a',
          'short-bg': 'rgba(229, 96, 90, 0.12)',
          entry: '#7aa2f7',
          'entry-bg': 'rgba(122, 162, 247, 0.12)',
          warn: '#d4a72c',
          'warn-bg': 'rgba(212, 167, 44, 0.12)',
        },
      },
      fontFamily: {
        sans: ['IBM Plex Sans', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['IBM Plex Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      borderRadius: {
        DEFAULT: '6px',
      },
    },
  },
  plugins: [],
};
