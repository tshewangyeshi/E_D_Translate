import { createApp, h } from "vue";
import { options } from "./app-vue.mjs";
import { CONTENT } from "./content.mjs";

const vm = createApp(options(h, CONTENT)).mount("#app");
window.__rerender = () => { vm.counter += 1; };
window.__setIntro = (text) => { vm.intro = text; };
window.__addBlock = (text) => { vm.extra.push(text); };
// An SPA route change: every added block goes, twenty new ones arrive (S4.3).
window.__route = (n) => { vm.extra = Array.from({ length: 20 }, (_, i) => `Route ${n}, block ${i}.`); };
