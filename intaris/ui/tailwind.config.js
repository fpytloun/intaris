/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./intaris/ui/static/**/*.{html,js}"],
  theme: {
    extend: {
      colors: {
        brand: {
          bg: '#0B1220',
          surface: '#121A2B',
          elevated: '#1A2438',
          accent: '#22D3EE',
          'accent-hover': '#67E8F9',
          'accent-active': '#0891B2',
          border: '#1E293B',
        },
      },
      textColor: {
        primary: '#E6EDF3',
        secondary: '#94A3B8',
        muted: '#64748B',
        disabled: '#475569',
      },
      boxShadow: {
        glow: '0 0 24px rgba(34, 211, 238, 0.35)',
        'glow-sm': '0 0 12px rgba(34, 211, 238, 0.2)',
      },
    },
  },
  plugins: [],
}
