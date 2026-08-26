export function triggerBrowserDownload(
  url: string,
  filename?: string | null,
) {
  if (typeof document === "undefined") return;

  const anchor = document.createElement("a");
  anchor.href = url;
  if (filename?.trim()) anchor.download = filename.trim();
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
