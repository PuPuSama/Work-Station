"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { Eye, Loader2, Save } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import { apiGet, apiPost } from "@/lib/api";
import type { ProjectPromptLibrary, PromptKind, PromptPreview, TaskRecord } from "@/types";

type Props = {
  task: TaskRecord;
  topicNotes: string;
  includeProjectIntroduction: boolean;
  includeProjectNotes: boolean;
  includeTopicNotes: boolean;
  useOutlineCustomPrompt: boolean;
  outlineCustomPrompt: string;
  useArticleCustomPrompt: boolean;
  articleCustomPrompt: string;
  outlinePromptSelection: string;
  articlePromptSelection: string;
  dirty: boolean;
  busy: boolean;
  canSave: boolean;
  onTopicNotesChange: (value: string) => void;
  onIncludeProjectIntroductionChange: (value: boolean) => void;
  onIncludeProjectNotesChange: (value: boolean) => void;
  onIncludeTopicNotesChange: (value: boolean) => void;
  onUseOutlineCustomPromptChange: (value: boolean) => void;
  onOutlineCustomPromptChange: (value: string) => void;
  onUseArticleCustomPromptChange: (value: boolean) => void;
  onArticleCustomPromptChange: (value: string) => void;
  onOutlinePromptSelectionChange: (value: string) => void;
  onArticlePromptSelectionChange: (value: string) => void;
  onSave: () => void;
};

export function ArticleWritingRequirementsStep(props: Props) {
  const {
    task,
    topicNotes,
    includeProjectIntroduction,
    includeProjectNotes,
    includeTopicNotes,
    useOutlineCustomPrompt,
    outlineCustomPrompt,
    useArticleCustomPrompt,
    articleCustomPrompt,
    outlinePromptSelection,
    articlePromptSelection,
    dirty,
    busy,
    canSave,
    onTopicNotesChange,
    onIncludeProjectIntroductionChange,
    onIncludeProjectNotesChange,
    onIncludeTopicNotesChange,
    onUseOutlineCustomPromptChange,
    onOutlineCustomPromptChange,
    onUseArticleCustomPromptChange,
    onArticleCustomPromptChange,
    onOutlinePromptSelectionChange,
    onArticlePromptSelectionChange,
    onSave,
  } = props;
  const [library, setLibrary] = useState<ProjectPromptLibrary | null>(null);
  const [preview, setPreview] = useState<PromptPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState<PromptKind | "">("");
  const [previewError, setPreviewError] = useState("");

  const loadLibrary = useCallback(async () => {
    setLibrary(await apiGet<ProjectPromptLibrary>(
      `/api/projects/${encodeURIComponent(task.customer)}/prompts`,
    ));
  }, [task.customer]);

  useEffect(() => {
    void loadLibrary().catch((error) =>
      setPreviewError(error instanceof Error ? error.message : "提示词库加载失败"),
    );
  }, [loadLibrary]);

  const activePrompts = useMemo(
    () => library?.prompts.filter((prompt) => prompt.active) ?? [],
    [library],
  );

  function defaultName(kind: PromptKind) {
    if (!library) return "读取中";
    const id = kind === "outline"
      ? library.defaults.default_outline_prompt_id
      : library.defaults.default_article_prompt_id;
    return library.prompts.find((prompt) => prompt.id === id)?.name || "系统默认";
  }

  async function loadPreview(kind: PromptKind) {
    setPreviewBusy(kind);
    setPreviewError("");
    try {
      setPreview(await apiPost<PromptPreview>(
        `/api/tasks/${task.id}/prompt-preview`,
        {
          kind,
          selection: kind === "outline" ? outlinePromptSelection : articlePromptSelection,
          supplemental_prompt: kind === "outline"
            ? (useOutlineCustomPrompt ? outlineCustomPrompt : "")
            : (useArticleCustomPrompt ? articleCustomPrompt : ""),
          include_project_introduction: includeProjectIntroduction,
          include_project_notes: includeProjectNotes,
          include_topic_notes: includeTopicNotes,
        },
      ));
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : "预览加载失败");
    } finally {
      setPreviewBusy("");
    }
  }

  function promptSelector(
    kind: PromptKind,
    selection: string,
    onChange: (value: string) => void,
  ) {
    const selectedInactive = library?.prompts.find(
      (prompt) => prompt.id === selection && !prompt.active,
    );
    return (
      <div className="grid gap-2">
        <Label htmlFor={`${kind}-base-prompt`}>
          {kind === "outline" ? "完整大纲提示词" : "完整正文提示词"}
        </Label>
        <select
          id={`${kind}-base-prompt`}
          className="h-9 rounded-md border bg-background px-3 text-sm"
          value={selection}
          onChange={(event) => {
            onChange(event.target.value);
            setPreview(null);
          }}
        >
          <option value="project_default">项目默认（{defaultName(kind)}）</option>
          <option value="system">系统默认</option>
          {selectedInactive && (
            <option value={selectedInactive.id} disabled>
              {selectedInactive.name}（已停用，下次生成将回退系统默认）
            </option>
          )}
          {activePrompts
            .filter((prompt) => prompt.kind === kind)
            .map((prompt) => (
              <option key={prompt.id} value={prompt.id}>{prompt.name}（v{prompt.version}）</option>
            ))}
        </select>
      </div>
    );
  }

  return (
    <ScrollArea className="h-[650px] pr-3">
      <div className="grid gap-4">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-lg border p-4">
            <div className="mb-2 font-medium">项目介绍（事实知识库）</div>
            <div className="max-h-40 overflow-auto whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
              {task.project_introduction || "尚未填写，请在项目设置补充公司业务、产品范围等背景。"}
            </div>
          </div>
          <div className="rounded-lg border p-4">
            <div className="mb-2 font-medium">项目注意事项</div>
            <div className="max-h-40 overflow-auto whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
              {task.project_notes || "尚未填写项目级反馈或注意事项。"}
            </div>
          </div>
        </div>

        <div className="grid gap-2 rounded-lg border p-4">
          <Label htmlFor="topic-notes">本话题专属注意事项</Label>
          <Textarea id="topic-notes" value={topicNotes} onChange={(event) => onTopicNotesChange(event.target.value)} className="min-h-28 resize-y" maxLength={30000} placeholder="仅这篇文章需要遵守的客户反馈、修改意见、内容侧重点或禁用表达" />
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div className="font-medium">生成时读取哪些资料</div>
          <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4" checked={includeProjectIntroduction} onChange={(event) => onIncludeProjectIntroductionChange(event.target.checked)} /><span>读取项目介绍（公司业务等事实背景）</span></label>
          <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4" checked={includeProjectNotes} onChange={(event) => onIncludeProjectNotesChange(event.target.checked)} /><span>读取项目注意事项（整批文章通用要求）</span></label>
          <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4" checked={includeTopicNotes} onChange={(event) => onIncludeTopicNotesChange(event.target.checked)} /><span>读取本话题专属注意事项</span></label>
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium">完整提示词选择</div>
            <Link href={`/projects/${encodeURIComponent(task.customer)}/settings`} className="text-xs text-primary underline-offset-4 hover:underline">管理项目提示词库</Link>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            {promptSelector("outline", outlinePromptSelection, onOutlinePromptSelectionChange)}
            {promptSelector("article", articlePromptSelection, onArticlePromptSelectionChange)}
          </div>
          <p className="text-xs text-muted-foreground">两个环节可自由选择不同提示词；选择“项目默认”会在生成时读取对应的项目最新默认值。</p>
        </div>

        <div className="grid gap-2 rounded-lg border p-4">
          <label className="flex items-center gap-3 font-medium"><input type="checkbox" className="size-4" checked={useOutlineCustomPrompt} onChange={(event) => onUseOutlineCustomPromptChange(event.target.checked)} />生成大纲时使用单篇补充提示词</label>
          <Textarea value={outlineCustomPrompt} onChange={(event) => onOutlineCustomPromptChange(event.target.value)} className="min-h-32 resize-y font-mono text-sm" maxLength={40000} placeholder="只针对本篇大纲的临时要求；优先级高于所选完整提示词。" />
        </div>

        <div className="grid gap-2 rounded-lg border p-4">
          <label className="flex items-center gap-3 font-medium"><input type="checkbox" className="size-4" checked={useArticleCustomPrompt} onChange={(event) => onUseArticleCustomPromptChange(event.target.checked)} />生成或重写正文时使用单篇补充提示词</label>
          <Textarea value={articleCustomPrompt} onChange={(event) => onArticleCustomPromptChange(event.target.value)} className="min-h-36 resize-y font-mono text-sm" maxLength={40000} placeholder="只针对本篇正文的语气、角度或修改要求；不会覆盖系统硬约束。" />
        </div>

        <details className="rounded-lg border p-4">
          <summary className="cursor-pointer font-medium">查看本次实际生效提示词</summary>
          <div className="mt-4 grid gap-3">
            <p className="text-sm text-muted-foreground">预览会组合系统硬约束、完整提示词、自动注入资料和单篇补充要求，不会触发生成。</p>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" onClick={() => void loadPreview("outline")} disabled={Boolean(previewBusy)}>{previewBusy === "outline" ? <Loader2 className="animate-spin" /> : <Eye />}预览大纲提示词</Button>
              <Button type="button" variant="outline" size="sm" onClick={() => void loadPreview("article")} disabled={Boolean(previewBusy)}>{previewBusy === "article" ? <Loader2 className="animate-spin" /> : <Eye />}预览正文提示词</Button>
            </div>
            {previewError && <p className="text-sm text-destructive">{previewError}</p>}
            {preview && (
              <div className="grid gap-2">
                <div className="text-sm"><span className="font-medium">当前来源：{preview.snapshot.name}</span><span className="ml-2 text-muted-foreground">v{preview.snapshot.version}</span></div>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs leading-5">{preview.effective_prompt}</pre>
              </div>
            )}
          </div>
        </details>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">完整提示词在项目设置维护；这里保存本话题的选择和补充要求。</p>
          <Button onClick={onSave} disabled={busy || !canSave || !dirty}><Save />保存写作要求</Button>
        </div>
      </div>
    </ScrollArea>
  );
}
