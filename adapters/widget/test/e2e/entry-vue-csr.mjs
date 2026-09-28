import { createApp, h } from "vue";
import { options } from "./app-vue.mjs";
import { CONTENT } from "./content.mjs";

const vm = createApp(options(h, CONTENT)).mount("#app");
window.__rerender = () => { vm.counter += 1; };
window.__setIntro = (text) => { vm.intro = text; };
