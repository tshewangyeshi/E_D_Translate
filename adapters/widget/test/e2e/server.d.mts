// Types for the fixture server, which is plain JS so it can be imported by the
// Playwright spec, the Node SSR path and esbuild without a build step.

export interface FixtureServer {
  server: { close(): void };
  port: number;
}

export declare function start(port?: number): Promise<FixtureServer>;

export declare const CONTENT: {
  heading: string;
  intro: string;
  fee: string;
  legal: string;
};
