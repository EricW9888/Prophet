import { calculateGraphLayout, type GraphLayoutRequest } from "./graph-layout";

self.addEventListener("message", (event: MessageEvent<GraphLayoutRequest>) => {
  self.postMessage(calculateGraphLayout(event.data));
});
