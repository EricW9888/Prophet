import type { GraphNeighborhood, GraphWebNode } from "../../src/lib/api";

/** Separate connected groups, one large star and optional isolated records. */
export function mixedGraph(componentCount = 60, perComponent = 20, spokes = 600, isolates = 0): GraphNeighborhood {
  const nodes: GraphWebNode[] = [];
  const edges: GraphNeighborhood["edges"] = [];
  const addNode = (key: string, root = false) => nodes.push({
    key, id: key, label: `Synthetic ${key}`,
    node_type: root ? "portfolio" : key.endsWith(":0") ? "entity" : "fact",
    layer: "knowledge", is_root: root, is_autonomous: false,
  });
  const addEdge = (source_key: string, target_key: string) => edges.push({
    id: `edge:${edges.length}`, source_key, target_key, relationship_type: "synthetic_link", confidence: 1,
  });
  addNode("root", true);
  for (let i = 0; i < spokes; i += 1) {
    const key = `root:${i}`; addNode(key); addEdge("root", key);
  }
  for (let group = 0; group < componentCount; group += 1) {
    for (let i = 0; i < perComponent; i += 1) addNode(`group:${group}:${i}`);
    for (let i = 1; i < perComponent; i += 1) {
      const key = `group:${group}:${i}`;
      addEdge(`group:${group}:0`, key);
      addEdge(key, `group:${group}:${(i + 1) % perComponent}`);
      addEdge(key, `group:${group}:${(i + 3) % perComponent}`);
    }
  }
  for (let i = 0; i < isolates; i += 1) addNode(`isolated:${i}`);
  return { root_key: "root", depth: 2, nodes, edges };
}
