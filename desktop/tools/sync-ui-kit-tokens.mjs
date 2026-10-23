// Regenerates src/MathChecker.Desktop/Themes/KitTokens.xaml from the installed
// ui-style-kit-css package.
//
// The WPF client cannot load CSS, so the kit is integrated at its documented
// token layer: this script reads the published preset bundle, extracts the
// shared `--usk-*-rgb` roles for the chosen theme + mode, and writes them as
// WPF Color/Brush resources. Themes/Tokens.xaml maps the app's own semantic
// brushes onto these, so a kit upgrade is one command:
//
//   node desktop/tools/sync-ui-kit-tokens.mjs [theme] [mode]
//
// Defaults: the paper-editorial preset, walnut-clay theme, dark mode.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const cssPath = join(
  here,
  "../../frontend/node_modules/ui-style-kit-css/dist/visual/paper-editorial.css",
);
const outPath = join(here, "../src/MathChecker.Desktop/Themes/KitTokens.xaml");

const theme = process.argv[2] ?? "walnut-clay";
const mode = process.argv[3] ?? "dark";

const css = readFileSync(cssPath, "utf8");
const blockPattern = new RegExp(
  `\\[data-theme="${theme}"\\]\\[data-mode="${mode}"\\]\\)\\s*\\{([\\s\\S]*?)\\}`,
);
const block = css.match(blockPattern);
if (!block) {
  console.error(`No token block found for ${theme} / ${mode} in ${cssPath}`);
  process.exit(1);
}

const channels = new Map();
for (const line of block[1].split(";")) {
  const match = line.match(/--usk-([a-z-]+)-rgb:\s*([\d ]+)/);
  if (match) {
    channels.set(match[1], match[2].trim().split(/\s+/).map(Number));
  }
}

const hex = ([r, g, b]) =>
  `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0").toUpperCase()).join("")}`;

// Role -> XAML key. `primary-text` is the ink used on a filled primary action,
// `secondary-text` the ink on a filled secondary action.
const roles = [
  ["bg", "Bg"],
  ["surface", "Surface"],
  ["surface-strong", "SurfaceStrong"],
  ["surface-soft", "SurfaceSoft"],
  ["text", "Text"],
  ["text-muted", "TextMuted"],
  ["border", "Border"],
  ["primary", "Primary"],
  ["primary-hover", "PrimaryHover"],
  ["primary-text", "PrimaryInk"],
  ["secondary", "Secondary"],
  ["secondary-hover", "SecondaryHover"],
  ["secondary-text", "SecondaryInk"],
  ["accent", "Accent"],
  ["success", "Success"],
  ["warning", "Warning"],
  ["danger", "Danger"],
  ["link", "Link"],
  ["focus", "Focus"],
];

const missing = roles.filter(([role]) => !channels.has(role)).map(([role]) => role);
if (missing.length) {
  console.error(`Missing roles: ${missing.join(", ")}`);
  process.exit(1);
}

const colors = roles.map(
  ([role, name]) => `    <Color x:Key="Kit${name}Color">${hex(channels.get(role))}</Color>`,
);
const brushes = roles.map(
  ([, name]) =>
    `    <SolidColorBrush x:Key="Kit${name}Brush" Color="{StaticResource Kit${name}Color}" />`,
);

const xaml = `<!-- ============================================================
     GENERATED FILE - do not edit by hand.
     Source: ui-style-kit-css@2.6.1, preset "paper-editorial",
             theme "${theme}", mode "${mode}".
     The kit's CSS custom properties (usk-*-rgb) become WPF colours;
     Themes/Tokens.xaml maps the application's brushes onto them.
     Regenerate with: node desktop/tools/sync-ui-kit-tokens.mjs
     ============================================================ -->
<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">

${colors.join("\n")}

${brushes.join("\n")}
</ResourceDictionary>
`;

writeFileSync(outPath, xaml, "utf8");
console.log(`Wrote ${outPath} (${roles.length} roles, ${theme}/${mode})`);
