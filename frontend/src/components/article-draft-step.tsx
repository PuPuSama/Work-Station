"use client";

import { FileText, RefreshCw, Save } from "lucide-react";

import { ArticleVersionHistory } from "@/components/article-version-history";
import { EditorPanel } from "@/components/article-workbench-ui";
import { Button } from "@/components/ui/button";
import type { TaskRecord } from "@/types";

export function ArticleDraftStep({
  task,
  articleText,
  articleTarget,
  articleCharacterTarget,
  articleWords,
  hasGeneratedFirstVersion,
  articleDirty,
  busy,
  hasActiveJob,
  canAction,
  onArticleChange,
  onGenerate,
  onSave,
  onRestore,
}: {
  task: TaskRecord;
  articleText: string;
  articleTarget: number;
  articleCharacterTarget: number;
  articleWords: number;
  hasGeneratedFirstVersion: boolean;
  articleDirty: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  canAction: (action: string) => boolean;
  onArticleChange: (value: string) => void;
  onGenerate: () => void;
  onSave: () => void;
  onRestore: (versionIndex: number, kind: string) => void;
}) {
  return (
    <>
      <EditorPanel
        value={articleText}
        onChange={onArticleChange}
        placeholder="生成或编辑正文"
        height="h-[500px]"
        meta={
          <div className="text-xs text-muted-foreground">
            生成范围：1000–{articleTarget} 词（约 {articleCharacterTarget.toLocaleString()}
            字符，含空格）/ 当前：{articleWords} 词；不机械截断或自动压缩。FAQ
            固定为最后一个 H2，3 个 Q 均需整行加粗；除最终 FAQ 外，每个 H2
            至少包含 2 个 H3
          </div>
        }
        actions={
          <>
            <Button
              onClick={onGenerate}
              disabled={busy || hasActiveJob || !canAction("generate_article")}
            >
              {hasGeneratedFirstVersion ? <RefreshCw /> : <FileText />}
              {hasGeneratedFirstVersion ? "仅重写正文" : "生成正文"}
            </Button>
            <Button
              variant="outline"
              onClick={onSave}
              disabled={
                busy ||
                !articleText.trim() ||
                !articleDirty ||
                !canAction("update_article")
              }
            >
              <Save />
              保存第一版
            </Button>
          </>
        }
      />
      <ArticleVersionHistory
        title="第一版正文记录"
        versions={task.article_versions || []}
        kinds={["initial"]}
        currentContent={articleText}
        onRestore={onRestore}
      />
    </>
  );
}
