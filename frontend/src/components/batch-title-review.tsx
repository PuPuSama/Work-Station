"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { CheckCircle2, ExternalLink, Loader2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import { apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { TaskRecord } from "@/types";

type BatchTitleReviewProps = {
  customer: string;
  tasks: TaskRecord[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTaskSaved: (task: TaskRecord) => void;
};

function topicLabel(task: TaskRecord) {
  return `topic_${String(task.topic_index).padStart(3, "0")}`;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "标题保存失败";
}

export function BatchTitleReview({
  customer,
  tasks,
  open,
  onOpenChange,
  onTaskSaved,
}: BatchTitleReviewProps) {
  const pendingTasks = useMemo(
    () =>
      tasks
        .filter((task) => task.status === "titles_ready" && task.title_candidates.length > 0)
        .sort((left, right) => left.topic_index - right.topic_index),
    [tasks],
  );
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [savingTaskId, setSavingTaskId] = useState("");
  const [error, setError] = useState("");
  const [savedCount, setSavedCount] = useState(0);

  useEffect(() => {
    if (!open) return;
    if (!pendingTasks.some((task) => task.id === selectedTaskId)) {
      setSelectedTaskId(pendingTasks[0]?.id ?? "");
    }
  }, [open, pendingTasks, selectedTaskId]);

  useEffect(() => {
    if (!open) {
      setError("");
      setSavedCount(0);
    }
  }, [open]);

  const activeTask =
    pendingTasks.find((task) => task.id === selectedTaskId) ?? pendingTasks[0] ?? null;
  const activeIndex = activeTask
    ? pendingTasks.findIndex((task) => task.id === activeTask.id)
    : -1;
  const activeChoice = activeTask ? choices[activeTask.id] ?? "" : "";

  async function saveAndContinue() {
    if (!activeTask || !activeChoice) return;
    const nextTaskId =
      pendingTasks[activeIndex + 1]?.id ?? pendingTasks[activeIndex - 1]?.id ?? "";
    setSavingTaskId(activeTask.id);
    setError("");
    try {
      const updated = await apiPost<TaskRecord>(
        `/api/tasks/${encodeURIComponent(activeTask.id)}/select-title`,
        {
          revision: activeTask.revision,
          title: activeChoice,
        },
      );
      onTaskSaved(updated);
      setSavedCount((count) => count + 1);
      setChoices((current) => {
        const next = { ...current };
        delete next[activeTask.id];
        return next;
      });
      setSelectedTaskId(nextTaskId);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setSavingTaskId("");
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!activeTask || savingTaskId) return;
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && activeChoice) {
      event.preventDefault();
      void saveAndContinue();
    }
  }

  const completed = !pendingTasks.length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="h-[min(820px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden p-0 sm:max-w-[1180px]"
        onKeyDown={handleKeyDown}
      >
        <DialogHeader className="border-b px-5 py-4 pr-12">
          <div className="flex flex-wrap items-center gap-2">
            <DialogTitle>集中审核标题</DialogTitle>
            <Badge variant="secondary">剩余 {pendingTasks.length} 篇</Badge>
            {savedCount > 0 && <Badge variant="outline">本次已保存 {savedCount} 篇</Badge>}
          </div>
          <DialogDescription>
            逐篇点击候选标题并保存。Ctrl+Enter 可保存并进入下一篇。
          </DialogDescription>
        </DialogHeader>

        {completed ? (
          <div className="flex min-h-0 flex-col items-center justify-center gap-3 p-8 text-center">
            <CheckCircle2 className="size-10 text-emerald-700" />
            <div className="text-lg font-semibold">待选标题已经处理完</div>
            <p className="text-sm text-muted-foreground">可以关闭窗口，继续批量找产品或生成大纲。</p>
          </div>
        ) : (
          <div className="grid min-h-0 md:grid-cols-[300px_minmax(0,1fr)]">
            <div className="min-h-0 border-r bg-muted/20">
              <div className="border-b px-4 py-3 text-sm font-medium">
                待审核文章
              </div>
              <ScrollArea className="h-[calc(100%-45px)]">
                <div className="grid gap-1 p-2">
                  {pendingTasks.map((task, index) => (
                    <button
                      key={task.id}
                      type="button"
                      onClick={() => {
                        setSelectedTaskId(task.id);
                        setError("");
                      }}
                      className={cn(
                        "grid gap-1 rounded-lg border border-transparent px-3 py-2 text-left hover:bg-accent/60",
                        activeTask?.id === task.id && "border-primary bg-background shadow-sm",
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="text-xs font-medium text-muted-foreground">
                          {topicLabel(task)}
                        </span>
                        <span className="text-xs text-muted-foreground">{index + 1}/{pendingTasks.length}</span>
                      </span>
                      <span className="line-clamp-2 text-sm leading-5">{task.topic}</span>
                      {choices[task.id] && (
                        <span className="line-clamp-1 text-xs text-emerald-700">已暂选标题</span>
                      )}
                    </button>
                  ))}
                </div>
              </ScrollArea>
            </div>

            <div className="grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 p-4">
              <div className="grid gap-2 rounded-lg border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{topicLabel(activeTask)}</span>
                  <Button
                    size="sm"
                    variant="ghost"
                    nativeButton={false}
                    render={
                      <Link
                        href={`/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(activeTask.id)}?step=titles`}
                        target="_blank"
                      />
                    }
                  >
                    <ExternalLink />
                    打开完整任务
                  </Button>
                </div>
                <p className="text-sm leading-6">{activeTask.topic}</p>
              </div>
              <Progress
                value={pendingTasks.length ? ((activeIndex + 1) / pendingTasks.length) * 100 : 100}
                className="h-1.5"
              />
              <ScrollArea className="min-h-0 pr-3">
                <RadioGroup
                  value={activeChoice}
                  onValueChange={(value) =>
                    setChoices((current) => ({ ...current, [activeTask.id]: value }))
                  }
                  className="gap-2 pb-2"
                >
                  {activeTask.title_candidates.map((title, index) => (
                    <label
                      key={title}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-accent/50",
                        activeChoice === title && "border-primary bg-accent/40 ring-1 ring-primary/20",
                      )}
                    >
                      <span className="flex size-6 shrink-0 items-center justify-center rounded bg-muted text-xs font-semibold">
                        {index + 1}
                      </span>
                      <RadioGroupItem value={title} className="mt-1" />
                      <span className="text-sm leading-6">{title}</span>
                    </label>
                  ))}
                </RadioGroup>
              </ScrollArea>
            </div>
          </div>
        )}

        <DialogFooter className="mx-0 mb-0 px-5">
          {error && <p className="mr-auto self-center text-sm text-destructive">{error}</p>}
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {completed ? "完成并关闭" : "稍后继续"}
          </Button>
          {!completed && (
            <Button
              onClick={() => void saveAndContinue()}
              disabled={!activeChoice || Boolean(savingTaskId)}
            >
              {savingTaskId ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
              {pendingTasks.length === 1 ? "保存标题" : "保存并下一篇"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
