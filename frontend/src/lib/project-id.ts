export function normalizedProjectId(value: string): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw.includes("://") ? raw : `https://${raw}`);
    return url.hostname
      .toLocaleLowerCase()
      .replace(/\.$/, "")
      .replace(/^www\./, "");
  } catch {
    return raw
      .toLocaleLowerCase()
      .replace(/\.$/, "")
      .replace(/^www\./, "");
  }
}

export function sameProjectId(left: string, right: string): boolean {
  return normalizedProjectId(left) === normalizedProjectId(right);
}
