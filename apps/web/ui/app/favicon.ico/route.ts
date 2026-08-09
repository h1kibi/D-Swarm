const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#ffffff"/>
  <path fill-rule="evenodd" fill="#0D5C45" d="M11 5h17.5c15.25 0 22.5 10.5 22.5 27S43.75 59 28.5 59H11V5zm10 11.25v27.5h7.5c8.5 0 11.25-5.5 11.25-13.75S37 16.25 28.5 16.25H21z"/>
</svg>`;

export const dynamic = "force-static";

export function GET() {
  return new Response(svg, {
    headers: {
      "Content-Type": "image/svg+xml",
      "Cache-Control": "public, max-age=31536000, immutable",
    },
  });
}
