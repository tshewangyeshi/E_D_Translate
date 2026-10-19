// dzweb demo extension: boots the widget on the G2C portal, as its script tag will.
//
// The widget modules are the real build (adapters/widget/dist, copied into
// widget/ by tools/build_extension.py), loaded from the extension. This file
// does what src/main.ts does for a script tag -- control, notice, typography,
// watching -- with one difference: network calls go through the background
// worker (background.js). Any failure leaves the portal exactly as it was.

(async () => {
  try {
    const url = (path) => chrome.runtime.getURL(path);
    const { Widget, savePreference, savedPreference, whenQuiet } = await import(url("widget/widget.js"));
    const { Notice } = await import(url("widget/notice.js"));
    const { sendFeedback } = await import(url("widget/api.js"));
    const { installTypography } = await import(url("widget/typography.js"));
    const { LABEL_SWITCH_TO_DZ, LABEL_SWITCH_TO_EN } = await import(url("widget/locale-dz.js"));

    // fetch, relayed through the background worker; enough of a Response for the widget.
    const fetchImpl = async (target, init = {}) => {
      const answer = await chrome.runtime.sendMessage({
        kind: "dzweb-fetch",
        url: String(target),
        method: init.method,
        headers: init.headers,
        body: init.body,
      });
      return {
        ok: Boolean(answer?.ok),
        status: answer?.status ?? 0,
        json: async () => JSON.parse(answer?.text ?? ""),
      };
    };

    const api = { base: "http://127.0.0.1:8000", site: "portal", fetchImpl };
    const widget = new Widget(api, () => document.body);
    const notice = new Notice({
      send: (report) => sendFeedback(api, report),
      keyFor: (block) => widget.segmentKeyFor(block),
    });
    widget.onMachineOutput = () => notice.show();
    widget.beforeWrite = () => installTypography(document, url("widget/fonts/dzweb-dzongkha.woff2"));
    if (!(await widget.start())) return; // the API is not running: offer nothing

    // The portal has no styles for the widget's own button and notice.
    const style = document.createElement("style");
    style.setAttribute("data-dz-style-controls", "");
    style.textContent =
      "button[data-dz-control]{position:fixed;right:20px;bottom:20px;z-index:2147483647;padding:10px 18px;" +
      "border-radius:22px;border:1px solid #0288c7;background:#fff;color:#0288c7;font:16px system-ui;" +
      "cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25)}" +
      "aside[data-dz-notice]{position:fixed;left:20px;bottom:20px;z-index:2147483647;max-width:420px;" +
      "background:#fffbe6;border:1px solid #e0c46c;border-radius:8px;padding:10px 14px;display:grid;gap:6px;" +
      "font:14px system-ui;color:#333}aside[data-dz-notice] button{justify-self:start}";
    document.head.appendChild(style);

    const button = document.createElement("button");
    button.type = "button";
    button.textContent = LABEL_SWITCH_TO_DZ;
    button.setAttribute("data-dz-control", "");
    button.setAttribute("translate", "no");
    button.addEventListener("click", async () => {
      if (widget.language === "en") {
        savePreference("dz");
        await widget.translate();
        button.textContent = LABEL_SWITCH_TO_EN;
      } else {
        savePreference("en");
        widget.toggleBack();
        notice.hide();
        button.textContent = LABEL_SWITCH_TO_DZ;
      }
    });
    document.body.appendChild(button);
    widget.watch();

    if (savedPreference() === "dz") {
      whenQuiet(() => {
        void widget.translate().then(() => {
          button.textContent = LABEL_SWITCH_TO_EN;
        });
      });
    }
  } catch (error) {
    console.warn("dzweb demo: not started", error); // the portal stays English
  }
})();
