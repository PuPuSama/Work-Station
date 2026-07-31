import type { AccessibleProject } from "@/types";

export const SERVER_ACTIVE_JOB_STATUSES = new Set([
  "queued",
  "retry_wait",
  "running",
]);

export const SERVER_RETRYABLE_JOB_STATUSES = new Set([
  "failed",
  "cancelled",
  "conflict",
]);

export function serverOperationLabel(operation: string) {
  const labels: Record<string, string> = {
    article: "正文初稿",
    humanize: "自动人化",
    knowledge_research: "资料研究",
    outline: "大纲生成",
    product_rediscovery: "产品重新发现",
    restore_links: "链接恢复",
    seo_review: "SEO Review",
    titles: "标题候选",
  };
  return labels[operation] ?? operation;
}

export function serverJobStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: "排队中",
    retry_wait: "等待重试",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    conflict: "版本冲突",
    completed_with_errors: "含失败项",
  };
  return labels[status] ?? status;
}

export function serverJobStep(operation: string) {
  if (operation === "titles" || operation === "product_rediscovery") {
    return "setup";
  }
  if (operation === "outline") return "outline";
  if (operation === "knowledge_research") return "research";
  if (operation === "article") return "draft";
  if (
    operation === "humanize" ||
    operation === "restore_links" ||
    operation === "seo_review"
  ) {
    return "review";
  }
  return "setup";
}

export function serverJobHref(
  customer: string,
  taskId: string,
  operation: string,
) {
  const project = encodeURIComponent(customer);
  if (operation === "knowledge_research") {
    return `/projects/${project}/knowledge?tab=research`;
  }
  return `/projects/${project}/articles/${encodeURIComponent(taskId)}?step=${serverJobStep(operation)}`;
}

export function canControlServerJob(
  role: AccessibleProject["effective_role"] | null,
  operation: string,
) {
  if (operation === "knowledge_research") {
    // Research Resume is a domain command. Generic Job cancel/retry remains
    // fail-closed until Run/Checkpoint cancellation semantics are implemented.
    return false;
  }
  if (role === "org_admin" || role === "team_lead" || role === "editor") {
    return true;
  }
  return role === "reviewer" && operation === "seo_review";
}
