"use client";

import { Download, Package, Sparkles } from "lucide-react";

import { FileRow, WorkflowStep } from "@/components/article-workbench-ui";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import type { BatchOperation, PublicConfig, TaskRecord } from "@/types";

export function ArticleDeliveryStep({
  task,
  config,
  busy,
  hasActiveJob,
  canAction,
  onEnqueue,
}: {
  task: TaskRecord;
  config: PublicConfig | null;
  busy: boolean;
  hasActiveJob: boolean;
  canAction: (action: string) => boolean;
  onEnqueue: (operation: BatchOperation, label: string) => void;
}) {
  return (
    <ScrollArea className="h-[570px] pr-3">
      <div className="grid gap-4">
        <WorkflowStep
          number="1"
          title="导出最终 Word"
          description="仅导出已完成两次人工检测、链接校验和图片准备的版本。"
          done={task.status === "docx_exported"}
        >
          <Button
            onClick={() => onEnqueue("export_docx", "Word 导出")}
            disabled={busy || hasActiveJob || !canAction("export_docx")}
          >
            <Download />
            导出 Word
          </Button>
          {task.docx_path && (
            <div className="break-all text-sm text-muted-foreground">
              {task.docx_path}
            </div>
          )}
        </WorkflowStep>

        <WorkflowStep
          number="2"
          title="生成英文 SEO TDK"
          description="根据最终正文生成 T、D、K；T 与正文 H1 完全一致，D 最多 150 个字符，K 固定 6 个关键词，并保存为 D.docx。"
          done={Boolean(task.tdk_path)}
        >
          <Button
            onClick={() => onEnqueue("generate_tdk", "TDK 文档生成")}
            disabled={busy || hasActiveJob || !canAction("generate_tdk")}
          >
            <Sparkles />
            生成 TDK 文档
          </Button>
          {task.tdk?.title && (
            <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm">
              <div>
                <span className="font-semibold">T: </span>
                {task.tdk.title}
              </div>
              <div>
                <span className="font-semibold">D: </span>
                {task.tdk.description}
                <span className="ml-2 text-xs text-muted-foreground">
                  {task.tdk.description_character_count}/150
                </span>
              </div>
              <div>
                <span className="font-semibold">K: </span>
                {task.tdk.keywords.join(", ")}
              </div>
              <div className="break-all text-xs text-muted-foreground">
                {task.tdk_path}
              </div>
            </div>
          )}
        </WorkflowStep>

        <WorkflowStep
          number="3"
          title="交付打包"
          description="正文 Word、D.docx、全部文章图片和最后一次 AI 检测截图直接放在成品文件夹根目录；初检截图不打包。"
          done={Boolean(task.delivery_package_path)}
        >
          <Button
            onClick={() => onEnqueue("package_delivery", "交付打包")}
            disabled={
              busy ||
              hasActiveJob ||
              !task.docx_path ||
              !task.tdk_path ||
              !canAction("package_delivery")
            }
          >
            <Package />
            生成交付文件夹
          </Button>
          {task.delivery_package_path && (
            <div className="break-all text-sm text-muted-foreground">
              {task.delivery_package_path}
            </div>
          )}
        </WorkflowStep>

        <div className="grid gap-3 rounded-lg border bg-muted/20 p-4 text-sm">
          <div className="font-medium">文件与项目位置</div>
          <FileRow label="任务目录" value={task.task_dir} />
          <FileRow label="Word 文件" value={task.docx_path || "未导出"} />
          <FileRow label="TDK 文档" value={task.tdk_path || "未生成"} />
          <FileRow label="交付成品" value={task.delivery_package_path || "未打包"} />
          <FileRow label="话题库" value={config?.topic_library ?? ""} />
          <FileRow label="知识库" value={config?.knowledge_base ?? ""} />
          <Separator />
          <div className="grid gap-1">
            <span className="font-medium">竞品关键词 / 网站</span>
            <span className="text-muted-foreground">
              {task.competitor_keyword || "空"}
            </span>
          </div>
          <div className="grid gap-1">
            <span className="font-medium">竞品 Blog</span>
            <span className="break-all text-muted-foreground">
              {task.competitor_blog || "空"}
            </span>
          </div>
        </div>
      </div>
    </ScrollArea>
  );
}
