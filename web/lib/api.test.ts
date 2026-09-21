import { describe, expect, it } from "vitest";
import { apiUrl } from "./api";

describe("apiUrl", () => {
  it("keeps the origin and does not repeat the version prefix", () => {
    // API_BASE ends in /api/v1 and the signed link already starts with it.
    expect(apiUrl("/api/v1/reports/abc/html?expires=1&signature=x")).toBe(
      "http://localhost:8000/api/v1/reports/abc/html?expires=1&signature=x",
    );
  });

  it("does not double up when the path has no prefix", () => {
    expect(apiUrl("/health")).toBe("http://localhost:8000/health");
  });
});
