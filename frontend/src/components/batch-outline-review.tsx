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
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import { apiPut } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { TaskRecord } from "@/types";

type BatchOutlineReviewProps = {
  customer: string;
  tasks: TaskRecord[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTaskSaved: (task: TaskRecord) => void;
};

function topicLabel(task: TaskRecord) {
  return `topic_${String(task.topic_index).padStart(3, "0")}`;
}

function taskOutline(task: TaskRecord) {
  return (task.outline_draft || task.outline || "").trim();
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "大纲保存失败";
}

export function BatchOutlineReview({
  customer,
  tasks,
  open,
  onOpenChange,
  onTaskSaved,
}: BatchOutlineReviewProps) {
  const pendingTasks = useMemo(
    () =>
      tasks
        .filter((task) => task.status === "outline_ready" && taskOutline(task))
        .sort((left, right) => left.topic_index - right.topic_index),
    [tasks],
  );
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savingTaskId, setSavingTaskId] = useState("");
  const [error, setError] = useState("");
  const [savedCount, setSavedCount] = useState(0);

  useEffect(() => {
    if (!open) return;
    setDrafts((current) => {
      const next = { ...current };
      for (const task of pendingTasks) {
        if (next[task.id] === undefined) next[task.id] = taskOutline(task);
      }
      return next;
    });
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
  const activeDraft = activeTask ? drafts[activeTask.id] ?? taskOutline(activeTask) : "";
  const activeDirty = activeTask ? activeDraft.trim() !== taskOutline(activeTask) : false;

  async function saveAndContinue() {
    if (!activeTask || !activeDraft.trim()) return;
    const nextTaskId =
      pendingTasks[activeIndex + 1]?.id ?? pendingTasks[activeIndex - 1]?.id ?? "";
    setSavingTaskId(activeTask.id);
    setError("");
    try {
      const updated = await apiPut<TaskRecord>(
        `/api/tasks/${encodeURIComponent(activeTask.id)}/outline`,
        {
          revision: activeTask.revision,
          outline: activeDraft,
          confirmed: true,
        },
      );
      onTaskSaved(updated);
      setSavedCount((count) => count + 1);
      setDrafts((current) => {
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

  const completed = !pendingTasks.length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="h-[min(860px,calc(100vh-2rem))] w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden p-0 sm:max-w-[1280px]">
        <DialogHeader className="border-b px-5 py-4 pr-12">
          <div className="flex flex-wrap items-center gap-2">
            <DialogTitle>集中审核大纲</DialogTitle>
            <Badge variant="secondary">剩余 {pendingTasks.length} 篇</Badge>
            {savedCount > 0 && <Badge variant="outline">本次已确认 {savedCount} 篇</Badge>}
          </div>
          <DialogDescription>
            在这里检查或修改大纲，保存并确认后自动进入下一篇。确认后该文章即可生成正文。
          </DialogDescription>
        </DialogHeader>

        {completed ? (
          <div className="flex min-h-0 flex-col items-center justify-center gap-3 p-8 text-center">
            <CheckCircle2 className="size-10 text-emerald-700" />
            <div className="text-lg font-semibold">待确认大纲已经处理完</div>
            <p className="text-sm text-muted-foreground">可以关闭窗口，继续批量生成正文。</p>
          </div>
        ) : (
          <div className="grid min-h-0 md:grid-cols-[300px_minmax(0,1fr)]">
            <div className="min-h-0 border-r bg-muted/20">
              <div className="border-b px-4 py-3 text-sm font-medium">待确认大纲</div>
              <ScrollArea className="h-[calc(100%-45px)]">
                <div className="grid gap-1 p-2">
                  {pendingTasks.map((task, index) => {
                    const dirty = (drafts[task.id] ?? taskOutline(task)).trim() !== taskOutline(task);
                    return (
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
                          <span className="text-xs font-medium text-muted-foreground">{topicLabel(task)}</span>
                          <span className="text-xs text-muted-foreground">{index + 1}/{pendingTasks.length}</span>
                        </span>
                        <span className="line-clamp-2 text-sm leading-5">{task.selected_title || task.topic}</span>
                        {dirty && <span className="text-xs text-amber-700">有未保存修改</span>}
                      </button>
                    );
                  })}
                </div>
              </ScrollArea>
            </div>

            <div className="grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 p-4">
              <div className="grid gap-2 rounded-lg border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{topicLabel(activeTask)}</span>
                    {activeDirty && <Badge variant="outline">已修改</Badge>}
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    nativeButton={false}
                    render={
                      <Link
                        href={`/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(activeTask.id)}?step=outline`}
                        target="_blank"
                      />
                    }
                  >
                    <ExternalLink />
                    打开完整任务
                  </Button>
                </div>
                <p className="text-sm font-medium leading-6">{activeTask.selected_title || activeTask.topic}</p>
              </div>
              <Progress
                value={pendingTasks.length ? ((activeIndex + 1) / pendingTasks.length) * 100 : 100}
                className="h-1.5"
              />
              <Textarea
                aria-label="审核或修改大纲"
                value={activeDraft}
                onChange={(event) =>
                  setDrafts((current) => ({ ...current, [activeTask.id]: event.target.value }))
                }
                className="min-h-0 resize-none overflow-y-auto font-mono text-sm leading-6"
              />
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
              disabled={!activeDraft.trim() || Boolean(savingTaskId)}
            >
              {savingTaskId ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
              {pendingTasks.length === 1 ? "保存并确认" : "保存并确认、下一篇"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
