# dzweb demo extension (developer mode)

Shows the real widget on the real G2C portal (`https://g2c.tech.gov.bt`) before
GovTech adds the script tag. The extension runs only on that site, carries the
widget build and the Dzongkha font, and sends the widget's requests to the
dzweb API on this computer (`http://127.0.0.1:8000`) — nothing else. The page
changes only in the browser that has the extension; the portal itself does not.

Development only: it relies on the local demo API and the demo enrolment in
`tools/demo_site/sites.json`. It is not the production integration (FR-200).

## Build

```bash
cd adapters/widget && npm run build && cd ../..
python tools/build_extension.py        # writes adapters/extension/dist
```

## Load it once (Chrome or Edge)

1. Open `chrome://extensions` (Edge: `edge://extensions`).
2. Turn on **Developer mode**.
3. **Load unpacked** → choose `adapters/extension/dist`.
4. Check the ID is `akplhnejomokdklicmdjpppindfjcccd`. The API accepts that ID only.

After a rebuild, press the extension's reload arrow.

## Run the demo

```bash
docker compose up -d
set -a && . tools/demo_site/demo.env && . ./.env && set +a
python -m uvicorn orchestrator.main:create --factory --host 127.0.0.1 --port 8000   # terminal 1
python -m orchestrator.queue.run_worker                                             # terminal 2 (same settings)
```

Open `https://g2c.tech.gov.bt/g2cportal`. A **རྫོང་ཁ** button appears bottom right;
click it to switch, **English** to switch back. No button means the API is not
running.

## How it works

- `content.js` boots the widget modules from the extension, as `src/main.ts`
  does from a script tag.
- `background.js` makes the API calls. A public page may not call a service on
  this computer directly (Chrome's local-network protection); the extension,
  with host permission for the API only, may. It refuses any other address.
- Chrome leaves `Origin` off an extension's GET requests; `rules.json` sets it
  to the extension's origin on requests to the API, so the API can check the
  enrolment as it does for any site (NFR-301).
- The `key` in `manifest.json` fixes the extension ID on every computer. Its
  private key was not kept: the extension is never packed or published.
