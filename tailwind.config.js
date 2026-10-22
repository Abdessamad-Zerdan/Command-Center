// Tailwind 3.4.17 — the same version and settings the pages used to load
// from cdn.tailwindcss.com, which compiled the CSS in the browser on every
// page load. Now built once into static/css/app.css: run `uv run make-css`
// after changing templates (see src/command_center/build_css.py). CI fails
// if app.css is out of date, and the app logs a warning at startup.
module.exports = {
  content: [
    "./src/command_center/templates/**/*.html",
    // Classes also live in Python (chart SVG builders, chips) and in JS.
    "./src/command_center/**/*.py",
    "./static/js/*.js",  // not vendor/: minified Alpine would add junk classes
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        rust: "#b5563a",
        "rust-light": "#d97757",
      },
    },
  },
};
