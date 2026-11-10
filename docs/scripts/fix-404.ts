// Post-render step for the 404 page (run by Quarto with its own Deno; no other tool needed).
//
// GitHub Pages serves 404.html at whatever wrong address was asked for, so the page's links must
// not be relative. Quarto already writes them from the root ("/java/index.html"), but the site is
// served under /xdssat/. Quarto's site-url setting would add that prefix, but it also drops the
// "xdssat/" folder from the Guide menu links, because the folder has the site's own name. So this
// script adds the prefix itself, to every root link of 404.html and nothing else.

const outDir = Deno.env.get("QUARTO_PROJECT_OUTPUT_DIR") ?? "_site";
const file = `${outDir}/404.html`;
const base = "/xdssat/";
const marker = "<!-- 404 links prefixed -->";

let html: string;
try {
  html = Deno.readTextFileSync(file);
} catch {
  Deno.exit(0); // 404 page not rendered this time
}

if (!html.includes(marker)) {
  // href="/x", src="/x", including Quarto's "/./x"; protocol-relative "//host" stays as it is
  html = html.replace(/\b(href|src)="\/(?:\.\/)?(?!\/)/g, `$1="${base}`);
  // the search box finds search.json from this offset
  html = html.replace('name="quarto:offset" content="/"', `name="quarto:offset" content="${base}"`);
  html = html.replace("</head>", `${marker}\n</head>`);
  Deno.writeTextFileSync(file, html);
}
