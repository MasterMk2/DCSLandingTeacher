import { describe, expect, it } from "vitest";
import { siteHomeHref } from "./siteLink";

describe("siteHomeHref", () => {
  it("links to the domain root when the app sits under a subpath", () => {
    expect(siteHomeHref("https://example.com/landing-teacher/")).toBe("/");
    expect(siteHomeHref("https://example.com/landing-teacher/#/landings/42")).toBe("/");
    expect(siteHomeHref("https://example.com/landing-teacher/index.html")).toBe("/");
  });

  it("offers no link when the app is the site", () => {
    expect(siteHomeHref("http://localhost:5173/")).toBeNull();
    expect(siteHomeHref("http://localhost:5173/#/landings/42")).toBeNull();
    expect(siteHomeHref("http://localhost:8000/index.html")).toBeNull();
  });
});
