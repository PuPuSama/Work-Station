"use client";

import { Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import type { TaskRecord } from "@/types";

type ArticleWritingRequirementsStepProps = {
  task: TaskRecord;
  topicNotes: string;
  includeProjectIntroduction: boolean;
  includeProjectNotes: boolean;
  includeTopicNotes: boolean;
  useOutlineCustomPrompt: boolean;
  outlineCustomPrompt: string;
  useArticleCustomPrompt: boolean;
  articleCustomPrompt: string;
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
  onSave: () => void;
};

export function ArticleWritingRequirementsStep({
  task,
  topicNotes,
  includeProjectIntroduction,
  includeProjectNotes,
  includeTopicNotes,
  useOutlineCustomPrompt,
  outlineCustomPrompt,
  useArticleCustomPrompt,
  articleCustomPrompt,
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
  onSave,
}: ArticleWritingRequirementsStepProps) {
  return (
    <ScrollArea className="h-[570px] pr-3">
      <div className="grid gap-4">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-lg border p-4">
            <div className="mb-2 font-medium">项目介绍（事实知识库）</div>
            <div className="max-h-40 overflow-auto whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
              {task.project_introduction || "尚未填写，请在项目首页补充公司业务、产品范围等背景。"}
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
          <Textarea
            id="topic-notes"
            value={topicNotes}
            onChange={(event) => onTopicNotesChange(event.target.value)}
            className="min-h-28 resize-y"
            maxLength={30000}
            placeholder="仅这篇文章需要遵守的客户反馈、修改意见、内容侧重点或禁用表达"
          />
        </div>

        <div className="grid gap-3 rounded-lg border p-4">
          <div className="font-medium">生成时读取哪些资料</div>
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1 size-4"
              checked={includeProjectIntroduction}
              onChange={(event) => onIncludeProjectIntroductionChange(event.target.checked)}
            />
            <span>读取项目介绍（公司业务等事实背景）</span>
          </label>
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1 size-4"
              checked={includeProjectNotes}
              onChange={(event) => onIncludeProjectNotesChange(event.target.checked)}
            />
            <span>读取项目注意事项（整批文章通用要求）</span>
          </label>
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1 size-4"
              checked={includeTopicNotes}
              onChange={(event) => onIncludeTopicNotesChange(event.target.checked)}
            />
            <span>读取本话题专属注意事项</span>
          </label>
        </div>

        <div className="grid gap-2 rounded-lg border p-4">
          <label className="flex items-center gap-3 font-medium">
            <input
              type="checkbox"
              className="size-4"
              checked={useOutlineCustomPrompt}
              onChange={(event) => onUseOutlineCustomPromptChange(event.target.checked)}
            />
            生成大纲时使用自定义补充提示词
          </label>
          <Textarea
            value={outlineCustomPrompt}
            onChange={(event) => onOutlineCustomPromptChange(event.target.value)}
            className="min-h-32 resize-y font-mono text-sm"
            maxLength={40000}
            placeholder="这里的内容会作为运营人员的附加要求，与系统默认的大纲结构和事实约束一起发送给模型。"
          />
        </div>

        <div className="grid gap-2 rounded-lg border p-4">
          <label className="flex items-center gap-3 font-medium">
            <input
              type="checkbox"
              className="size-4"
              checked={useArticleCustomPrompt}
              onChange={(event) => onUseArticleCustomPromptChange(event.target.checked)}
            />
            生成或重写正文时使用自定义补充提示词
          </label>
          <Textarea
            value={articleCustomPrompt}
            onChange={(event) => onArticleCustomPromptChange(event.target.value)}
            className="min-h-36 resize-y font-mono text-sm"
            maxLength={40000}
            placeholder="例如客户要求的语气、需要重点改写的内容、文章角度。不会替换系统的事实、字数和 Markdown 硬约束。"
          />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            项目介绍和项目注意事项请在项目首页编辑；这里保存本话题设置。
          </p>
          <Button onClick={onSave} disabled={busy || !canSave || !dirty}>
            <Save />
            保存写作要求
          </Button>
        </div>
      </div>
    </ScrollArea>
  );
}
