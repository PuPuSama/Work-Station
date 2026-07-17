"use client";

import { RefreshCw, Save, WandSparkles } from "lucide-react";

import { ArticleVersionHistory } from "@/components/article-version-history";
import { EditorPanel } from "@/components/article-workbench-ui";
import { Button } from "@/components/ui/button";
import type { TaskRecord } from "@/types";

export function ArticleOutlineStep({
  task,
  outlineText,
  outlineDirty,
  outlineNeedsConfirmation,
  outlineHasDownstream,
  canSaveOutline,
  busy,
  hasActiveJob,
  canAction,
  onOutlineChange,
  onGenerate,
  onSaveDraft,
  onSaveAndConfirm,
  onRestore,
}: {
  task: TaskRecord;
  outlineText: string;
  outlineDirty: boolean;
  outlineNeedsConfirmation: boolean;
  outlineHasDownstream: boolean;
  canSaveOutline: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  canAction: (action: string) => boolean;
  onOutlineChange: (value: string) => void;
  onGenerate: () => void;
  onSaveDraft: () => void;
  onSaveAndConfirm: () => void;
  onRestore: (versionIndex: number, kind: string) => void;
}) {
  return (
    <>
      <EditorPanel
        value={outlineText}
        onChange={onOutlineChange}
        placeholder="生成或编辑大纲"
        height="h-[480px]"
        meta={
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>
              {outlineNeedsConfirmation
                ? "生成结果尚未确认；保存后才能生成正文。"
                : outlineDirty
                  ? "大纲有未保存修改。"
                  : "当前大纲已保存。"}
            </span>
            {outlineHasDownstream && (
              <span className="text-amber-700">
                修改或重生成大纲会清空正文及后续结果。
              </span>
            )}
          </div>
        }
        actions={
          <>
            <Button
              onClick={onGenerate}
              disabled={busy || hasActiveJob || !canAction("generate_outline")}
            >
              {task.outline.trim() ? <RefreshCw /> : <WandSparkles />}
              {task.outline.trim() ? "重新生成大纲" : "生成大纲"}
            </Button>
            <Button
              variant="outline"
              onClick={onSaveDraft}
              disabled={
                busy ||
                !outlineDirty ||
                !outlineText.trim() ||
                !canAction("update_outline")
              }
            >
              <Save />
              保存草稿
            </Button>
            <Button
              variant="outline"
              onClick={onSaveAndConfirm}
              disabled={busy || !canSaveOutline || !canAction("update_outline")}
            >
              <Save />
              保存并确认
            </Button>
          </>
        }
      />
      <ArticleVersionHistory
        title="大纲版本记录"
        versions={task.article_versions || []}
        kinds={["outline", "outline_draft"]}
        currentContent={outlineText}
        onRestore={onRestore}
      />
    </>
  );
}
