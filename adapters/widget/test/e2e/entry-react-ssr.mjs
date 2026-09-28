import { createElement as h, useState, useEffect } from "react";
import { hydrateRoot } from "react-dom/client";
import { tree } from "./app-react.mjs";
import { CONTENT } from "./content.mjs";

function App() {
  const [counter, setCounter] = useState(0);
  const [intro, setIntro] = useState(CONTENT.intro);
  const [extra, setExtra] = useState([]);
  useEffect(() => {
    window.__rerender = () => setCounter((n) => n + 1);
    window.__setIntro = (text) => setIntro(text);
    window.__addBlock = (text) => setExtra((list) => [...list, text]);
  }, []);
  return tree(h, CONTENT, counter, intro, extra);
}
hydrateRoot(document.getElementById("app"), h(App));
