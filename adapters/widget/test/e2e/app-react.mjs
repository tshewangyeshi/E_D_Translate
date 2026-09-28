// The React fixture page, shared by the server render and the client render.
// One definition, so SSR and hydration cannot disagree for a reason that has
// nothing to do with the widget.
//
// Plain createElement rather than JSX: no transform step, so what the browser
// runs is what is written here.

export function tree(h, content, counter = 0, intro = content.intro, extra = []) {
  return h("main", null, [
    h("h1", { key: "h" }, content.heading),
    // MIXED content: a text node, an element, another text node. This shape is
    // the one that can catch a broken node identity. With a single text child a
    // framework just sets textContent on the parent and never notices which
    // node was there; with siblings it must patch one specific Text node, the
    // one it remembers -- so replacing that node makes the update disappear.
    h("p", { key: "i", id: "intro" }, [
      intro,
      h("a", { key: "a", href: "/help" }, "the help desk"),
      " today.",
    ]),
    h("p", { key: "f" }, content.fee),
    // An attribute surface, so alt text is exercised in a real browser too.
    h("img", { key: "m", id: "photo", src: "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==", alt: content.alt }),
    h("p", { key: "l", className: "legal" }, content.legal),
    h("p", { key: "c", id: "counter" }, `Counter: ${counter}`),
    // Blocks the host adds after load, the S4.2 case.
    ...extra.map((text, n) => h("p", { key: `x${n}`, className: "added" }, text)),
  ]);
}
