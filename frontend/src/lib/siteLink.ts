/**
 * Where the header's link back to the hosting site points, or null when
 * there is no hosting site to go back to.
 *
 * Behind a reverse-proxy subpath (e.g. "/landing-teacher/") the app is one
 * page of a larger site, whose own top page is the domain root. Served at
 * the root itself (local dev, a standalone install) the root IS this app,
 * and a link to it would only reload the page. Derived from the page's URL,
 * like the API base in api/client.ts, so one build serves both.
 */
export function siteHomeHref(pageUrl: string): string | null {
  return new URL(".", pageUrl).pathname === "/" ? null : "/";
}
