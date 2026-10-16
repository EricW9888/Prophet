import { JSDOM } from "jsdom";

// Synthetic component tests only: no script/resource loading or live requests.
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost:3000",
});
for (const name of ["window", "document", "HTMLElement", "Node", "Event", "MouseEvent"]) {
  globalThis[name] = name === "window" ? dom.window : dom.window[name];
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.self = dom.window;
