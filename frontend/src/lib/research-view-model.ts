import type { ResearchRunStatus } from "@/types";

export const TERMINAL_RESEARCH_STATUSES = new Set<ResearchRunStatus>([
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
]);

export function researchStatusLabel(status: ResearchRunStatus) {
  const labels: Record<ResearchRunStatus, string> = {
    queued: "排队中",
    running: "运行中",
    waiting_for_review: "等待人工复核",
    completed: "已完成",
    completed_with_warnings: "完成但有提醒",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status];
}

export function researchStatusVariant(
  status: ResearchRunStatus,
): "destructive" | "secondary" | "outline" {
  if (status === "failed" || status === "cancelled") return "destructive";
  if (status === "completed" || status === "completed_with_warnings") {
    return "secondary";
  }
  return "outline";
}

export function formatResearchDate(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function newResearchRequestId(prefix: "start" | "resume") {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}
