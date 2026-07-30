"use client";

import {
  AlertTriangle,
  ArrowRight,
  FileText,
  PackageSearch,
  Sparkles,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import type { TaskRecord } from "@/types";

type ArticleContextPanelProps = {
  task: TaskRecord;
  statusLabel: string;
  suggestedTabLabel: string;
  unsavedSections: string[];
  busyLabel?: string;
  articleWordCount: number;
  onSuggestedStep: () => void;
};

function compactDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function ArticleContextPanel({
  task,
  statusLabel,
  suggestedTabLabel,
  unsavedSections,
  busyLabel,
  articleWordCount,
  onSuggestedStep,
}: ArticleContextPanelProps) {
  return (
    <div className="grid content-start">
      <div className="p-4">
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
          Context
        </p>
        <h2 className="mt-1 text-sm font-semibold">当前文章上下文</h2>
      </div>

      <Separator />

      <div className="grid gap-3 p-4">
        <div>
          <div className="text-xs text-muted-foreground">当前状态</div>
          <Badge className="mt-1.5" variant={task.workflow_error ? "destructive" : "secondary"}>
            {task.workflow_error ? "处理失败" : statusLabel}
          </Badge>
        </div>

        <Button
          type="button"
          className="h-auto justify-between px-3 py-2.5"
          onClick={onSuggestedStep}
        >
          <span className="flex items-center gap-2">
            <Sparkles className="size-4" />
            建议进入：{suggestedTabLabel}
          </span>
          <ArrowRight className="size-4" />
        </Button>

        {busyLabel && (
          <div className="rounded-lg border border-primary/20 bg-accent/40 p-3 text-xs leading-5">
            <div className="font-medium text-accent-foreground">后台处理中</div>
            <div className="mt-0.5 text-muted-foreground">{busyLabel}</div>
          </div>
        )}

        {unsavedSections.length > 0 && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs leading-5 text-amber-950">
            <div className="flex items-center gap-1.5 font-medium">
              <AlertTriangle className="size-3.5" />
              有未保存修改
            </div>
            <div className="mt-1">{unsavedSections.join("、")}</div>
          </div>
        )}
      </div>

      <Separator />

      <div className="grid gap-4 p-4 text-sm">
        <div>
          <div className="text-xs text-muted-foreground">原始话题</div>
          <p className="mt-1 line-clamp-4 leading-5">{task.topic}</p>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">选定标题</div>
          <p className="mt-1 line-clamp-4 leading-5">
            {task.selected_title || "尚未选择标题"}
          </p>
        </div>
      </div>

      <Separator />

      <div className="grid grid-cols-2 gap-px bg-border">
        <div className="bg-card p-4">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <PackageSearch className="size-3.5" />
            产品
          </div>
          <div className="mt-1 text-xl font-semibold tabular-nums">
            {task.products.length}
          </div>
        </div>
        <div className="bg-card p-4">
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <FileText className="size-3.5" />
            正文词数
          </div>
          <div className="mt-1 text-xl font-semibold tabular-nums">
            {articleWordCount}
          </div>
        </div>
      </div>

      <div className="border-t p-4 text-xs text-muted-foreground">
        最近更新：{compactDate(task.updated_at)}
      </div>
    </div>
  );
}
