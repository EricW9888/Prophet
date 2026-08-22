export type OriginLike = {
  origin_kind?: string | null;
  origin_label?: string | null;
  origin_detail?: string | null;
};

export type NormalizedSourceOrigin = {
  origin_kind: string;
  origin_label: string;
  origin_detail: string | null;
};

export function normalizeSourceOrigin(origin?: OriginLike | null): NormalizedSourceOrigin {
  return {
    origin_kind: origin?.origin_kind?.trim() || "unknown",
    origin_label: origin?.origin_label?.trim() || "Unspecified origin",
    origin_detail: origin?.origin_detail?.trim() || null,
  };
}
