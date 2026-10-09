// dzweb demo extension: the background worker (MV3 service worker).
//
// The widget runs inside the G2C portal page, but its calls to the dzweb API go
// through here. A page on a public site reaching a service on this computer is
// blocked by the browser's local-network protection; the extension, granted
// host permission for the API, is not. The API sees this extension's own origin
// (chrome-extension://<id>), which is the only origin the demo enrols.
//
// Only the dzweb API is ever fetched: any other URL is refused, so a page
// cannot use the extension to reach anything else on this computer.

const API = "http://127.0.0.1:8000/";

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.kind !== "dzweb-fetch") return false;
  if (typeof message.url !== "string" || !message.url.startsWith(API)) {
    reply({ ok: false, status: 0, text: "" });
    return false;
  }
  fetch(message.url, {
    method: message.method ?? "GET",
    headers: message.headers ?? {},
    body: message.body ?? undefined,
    signal: AbortSignal.timeout(10_000),
  })
    .then(async (response) => reply({ ok: response.ok, status: response.status, text: await response.text() }))
    .catch(() => reply({ ok: false, status: 0, text: "" })); // the page stays English
  return true; // the reply is asynchronous
});
