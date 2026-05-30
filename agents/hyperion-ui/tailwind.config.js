/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0b0e14",
        panel: "#131826",
        edge: "#222a3d",
      },
    },
  },
  plugins: [],
};
