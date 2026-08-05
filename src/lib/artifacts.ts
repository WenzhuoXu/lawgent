/**
 * Artifact/link helpers ported from main.jsx (rewriteSandboxHref:48-59,
 * artifactOrigin:455-459).
 */

/**
 * Models sometimes emit `sandbox:` links or raw filesystem paths for generated
 * files. Rewrite them to the server's artifact endpoint so clicks download.
 */
export function rewriteSandboxHref(href: string): string {
  if (typeof href !== "string" || !href) return href;
  const m = href.match(/^sandbox:.*\/chat_artifacts\/([^/]+)\/(.+)$/);
  if (m) return `/api/artifacts/chat_artifacts/${m[1]}/${m[2]}`;
  const m2 = href.match(/\/outputs\/chat_artifacts\/([^/]+)\/(.+)$/);
  if (m2) return `/api/artifacts/chat_artifacts/${m2[1]}/${m2[2]}`;
  return href;
}

export function artifactOrigin(origin?: string): string {
  switch (origin) {
    case "fetched":
      return "Downloaded";
    case "generated":
      return "Generated";
    case "uploaded":
      return "Uploaded";
    default:
      return "Saved";
  }
}
