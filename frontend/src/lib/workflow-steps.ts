import type { WorkflowAssistantStep } from "@/types";

export const WORKFLOW_STEP_LABELS: Record<string, string> = {
  list_projects: "查看项目",
  list_tasks: "查看文章任务",
  read_project_context: "读取项目资料",
  evidence_query: "查询知识库",
  read_plan_status: "查看计划状态",
  update_project_notes: "更新项目提示词",
  create_task: "创建文章任务",
  generate_titles: "生成标题候选",
  select_title: "确认标题",
  generate_products: "生成产品候选",
  confirm_products: "确认产品",
  generate_outline: "生成大纲",
  start_research: "知识库研究",
  generate_article: "生成正文",
  review: "正文复检",
  humanize: "降 AI / 人化",
  restore_links: "恢复并校验链接",
  prepare_images: "准备图片",
  export_docx: "导出 Word",
  generate_tdk: "生成 TDK",
  package_delivery: "生成交付包",
};

export function stepSummaryText(
  summary: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function isReadyDeliveryStep(step: WorkflowAssistantStep): boolean {
  return step.status === "succeeded"
    && Boolean(step.article_task_id)
    && step.output_summary.artifact_kind === "delivery_package"
    && Boolean(String(step.output_summary.asset_id || "").trim());
}
