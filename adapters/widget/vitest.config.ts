// Vitest runs the jsdom unit tests only.
//
// The browser fixtures under test/e2e are Playwright specs: they need a real
// browser and a running fixture server, and vitest would otherwise collect
// them by their .spec.ts name and fail on the first import. Two runners, two
// clearly separated directories.

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    exclude: ["test/e2e/**", "node_modules/**", "dist/**"],
  },
});
