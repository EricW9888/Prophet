const IDENTIFIER_NAME = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/;
const ABBREVIATIONS = new Map<string, string>([
  ...["ai", "ebit", "ebitda", "eps", "etf", "ev", "fcf", "fy", "gaap", "ipo", "pe", "q1", "q2", "q3", "q4", "roa", "roe", "roic", "sec", "ttm", "us"]
    .map(word => [word, word.toUpperCase()] as [string, string]),
  ["qoq", "QoQ"],
  ["yoy", "YoY"],
]);

/**
 * Turn an identifier-style name (`customer_deposits_qoq`) into words. Names
 * already written for people are returned unchanged. Mirrors `readable_name`
 * in the backend graph service.
 */
export function readableName(name: string): string {
  if (!IDENTIFIER_NAME.test(name)) return name;
  const words = name.split("_").map(word => ABBREVIATIONS.get(word) ?? word);
  if (!ABBREVIATIONS.has(name.split("_")[0])) words[0] = words[0][0].toUpperCase() + words[0].slice(1);
  return words.join(" ");
}
