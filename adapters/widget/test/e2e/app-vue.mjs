// The Vue fixture page, shared by the server render and the client render.
// Mirrors app-react.mjs, including the mixed-content paragraph that makes a
// broken node identity observable.
//
// A render function rather than a `template` string: bundling "vue" resolves to
// the runtime-only build, which carries no template compiler.

export function options(h, content) {
  return {
    data() {
      return { ...content, counter: 0 };
    },
    render() {
      return h("main", null, [
        h("h1", null, this.heading),
        h("p", { id: "intro" }, [
          this.intro,
          h("a", { href: "/help" }, "the help desk"),
          " today.",
        ]),
        h("p", null, this.fee),
        h("p", { class: "legal" }, this.legal),
        h("p", { id: "counter" }, `Counter: ${this.counter}`),
      ]);
    },
  };
}
