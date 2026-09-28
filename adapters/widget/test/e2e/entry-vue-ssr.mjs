import { createSSRApp, h } from "vue";
import { options } from "./app-vue.mjs";
import { CONTENT } from "./content.mjs";

const vm = createSSRApp(options(h, CONTENT)).mount("#app");
window.__rerender = () => { vm.counter += 1; };
window.__setIntro = (text) => { vm.intro = text; };
window.__addBlock = (text) => { vm.extra.push(text); };
