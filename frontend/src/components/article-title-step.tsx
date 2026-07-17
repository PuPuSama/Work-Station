"use client";

import { CheckCircle2, WandSparkles } from "lucide-react";

import { TaskBrief } from "@/components/article-workbench-ui";
import { Button } from "@/components/ui/button";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { TaskRecord } from "@/types";

type ArticleTitleStepProps = {
  task: TaskRecord;
  titleChoice: string;
  titleDirty: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  canGenerate: boolean;
  canSelect: boolean;
  onGenerate: () => void;
  onSelect: () => void;
  onTitleChoiceChange: (value: string) => void;
};

export function ArticleTitleStep({
  task,
  titleChoice,
  titleDirty,
  busy,
  hasActiveJob,
  canGenerate,
  canSelect,
  onGenerate,
  onSelect,
  onTitleChoiceChange,
}: ArticleTitleStepProps) {
  return (
    <div className="grid gap-4">
      <TaskBrief task={task} />
      <div className="flex flex-wrap gap-2">
        <Button onClick={onGenerate} disabled={busy || hasActiveJob || !canGenerate}>
          <WandSparkles />
          生成 10 个标题
        </Button>
        <Button
          variant="outline"
          onClick={onSelect}
          disabled={busy || !titleChoice || !titleDirty || !canSelect}
        >
          <CheckCircle2 />
          选用标题
        </Button>
      </div>
      <ScrollArea className="h-[470px] pr-3">
        <RadioGroup
          value={titleChoice}
          onValueChange={onTitleChoiceChange}
          className="gap-2"
        >
          {task.title_candidates.length ? (
            task.title_candidates.map((title) => (
              <label
                key={title}
                className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 hover:bg-accent/50"
              >
                <RadioGroupItem value={title} className="mt-1" />
                <span className="text-sm leading-6">{title}</span>
              </label>
            ))
          ) : (
            <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
              暂无候选标题
            </div>
          )}
        </RadioGroup>
      </ScrollArea>
    </div>
  );
}
