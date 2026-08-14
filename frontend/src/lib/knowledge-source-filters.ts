import type { KnowledgeSourceSummary } from "@/types";

export type KnowledgeSourceOrigin = "local" | "website";
export type KnowledgeSourceSort = "newest" | "oldest";

export function knowledgeSourceOrigin(
  source: Pick<KnowledgeSourceSummary, "source_kind">,
): KnowledgeSourceOrigin {
  return source.source_kind === "private_file" ? "local" : "website";
}

function sourceActivityTime(source: KnowledgeSourceSummary): number | null {
  const value = source.pending_fetched_at ?? source.latest_fetched_at;
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
}

export function filterKnowledgeSources(
  sources: KnowledgeSourceSummary[],
  options: {
    query: string;
    sort: KnowledgeSourceSort;
    includeLocal: boolean;
    includeWebsite: boolean;
  },
): KnowledgeSourceSummary[] {
  const query = options.query.trim().toLocaleLowerCase();

  return sources
    .filter((source) => {
      const origin = knowledgeSourceOrigin(source);
      if (origin === "local" ? !options.includeLocal : !options.includeWebsite) {
        return false;
      }
      if (!query) return true;
      return [source.display_name, source.source_id, source.canonical_url ?? ""]
        .join(" ")
        .toLocaleLowerCase()
        .includes(query);
    })
    .sort((left, right) => {
      const leftTime = sourceActivityTime(left);
      const rightTime = sourceActivityTime(right);
      if (leftTime !== rightTime) {
        if (leftTime === null) return 1;
        if (rightTime === null) return -1;
        return options.sort === "newest"
          ? rightTime - leftTime
          : leftTime - rightTime;
      }
      return left.display_name.localeCompare(right.display_name, "zh-CN");
    });
}

export function countKnowledgeSourceOrigins(sources: KnowledgeSourceSummary[]) {
  return sources.reduce(
    (counts, source) => {
      counts[knowledgeSourceOrigin(source)] += 1;
      return counts;
    },
    { local: 0, website: 0 },
  );
}
