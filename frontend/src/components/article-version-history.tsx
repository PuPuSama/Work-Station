"use client";

import { History, RotateCcw } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { formatProjectDate } from "@/lib/project-date";
import type { ContentVersion } from "@/types";

export function ArticleVersionHistory({
  title,
  versions,
  kinds,
  currentContent,
  onRestore,
}: {
  title: string;
  versions: ContentVersion[];
  kinds: string[];
  currentContent: string;
  onRestore: (versionIndex: number, kind: string) => void;
}) {
  const [previewIndex, setPreviewIndex] = useState<number | null>(null);
  const candidates = versions
    .map((version, index) => ({ version, index }))
    .filter(({ version }) => kinds.includes(version.kind))
    .reverse();
  const selected = candidates.find(({ index }) => index === previewIndex);
  const sourceLabels: Record<string, string> = {
    generated: "模型生成",
    manual_draft: "人工草稿",
    manual_confirmed: "人工确认",
    manual_edit: "人工保存",
    raw_draft: "首次生成",
    regenerated_raw_draft: "重新生成",
    restored: "历史恢复",
  };

  return (
    <details className="mt-3 rounded-lg border bg-muted/15 p-3">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium">
        <History className="size-4" />
        {title}
        <Badge variant="outline" className="ml-1">{candidates.length}</Badge>
      </summary>
      <div className="mt-3 grid gap-3 lg:grid-cols-[280px_minmax(0,1fr)]">
        <div className="grid content-start gap-2">
          {candidates.map(({ version, index }) => (
            <button
              key={`${index}-${version.content_hash}`}
              type="button"
              onClick={() => setPreviewIndex(index)}
              className={cn(
                "rounded-lg border bg-background p-2.5 text-left text-sm hover:bg-accent/40",
                previewIndex === index && "border-primary ring-1 ring-primary/20",
              )}
            >
              <span className="block font-medium">
                {sourceLabels[version.source_kind] || version.source_kind || "历史版本"}
              </span>
              <span className="mt-1 block text-xs text-muted-foreground">
                {version.created_at
                  ? formatProjectDate(version.created_at, {
                      dateStyle: "medium",
                      timeStyle: "short",
                    })
                  : "旧版本"}
                {version.word_count ? ` · ${version.word_count} 词` : ""}
              </span>
            </button>
          ))}
          {!candidates.length && (
            <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
              暂无可恢复版本。下一次生成或保存后会自动记录。
            </p>
          )}
        </div>
        <div className="min-w-0">
          {selected ? (
            <div className="grid gap-2">
              <div className="grid gap-2 xl:grid-cols-2">
                <div className="grid gap-1.5">
                  <Label>历史版本</Label>
                  <Textarea
                    readOnly
                    value={selected.version.content}
                    className="min-h-72 resize-y font-mono text-sm"
                    aria-label={`${title}历史版本预览`}
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label>当前编辑内容</Label>
                  <Textarea
                    readOnly
                    value={currentContent}
                    className="min-h-72 resize-y font-mono text-sm"
                    aria-label={`${title}当前内容`}
                  />
                </div>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="truncate text-xs text-muted-foreground">
                  内容摘要：{selected.version.content_hash?.slice(0, 12) || "无"}
                </span>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => onRestore(selected.index, selected.version.kind)}
                >
                  <RotateCcw />
                  恢复这个版本
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex min-h-72 items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
              在左侧选择一个版本进行预览
            </div>
          )}
        </div>
      </div>
    </details>
  );
}
