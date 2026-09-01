export const PROJECT_TIME_ZONE = "Asia/Shanghai";

const TIME_ZONE_SUFFIX = /(Z|[+-]\d{2}:?\d{2})$/i;

/**
 * Parse API timestamps for the project's display timezone.
 *
 * New server timestamps carry an explicit offset. Older task payloads were
 * emitted by UTC containers without an offset, so treat those legacy values
 * as UTC instead of inheriting the browser's timezone.
 */
export function parseProjectDate(value: string | null | undefined): Date | null {
  const raw = String(value ?? "").trim();
  if (!raw) return null;

  let normalized = raw.includes(" ") && !raw.includes("T")
    ? raw.replace(" ", "T")
    : raw;
  if (!TIME_ZONE_SUFFIX.test(normalized)) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
      normalized = `${normalized}T00:00:00`;
    }
    normalized += "Z";
  }

  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatProjectDate(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = {
    dateStyle: "medium",
    timeStyle: "short",
  },
): string {
  const parsed = parseProjectDate(value);
  if (!parsed) return String(value ?? "");
  return new Intl.DateTimeFormat("zh-CN", {
    ...options,
    timeZone: PROJECT_TIME_ZONE,
  }).format(parsed);
}
