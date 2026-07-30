"use client";

import { Check, Circle, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type Stage = {
  value: string;
  step: number;
  label: string;
  tabs: string[];
};

type ArticleWorkflowNavigationProps = {
  stages: Stage[];
  activeStage: string;
  progressStageIndex: number;
  busyTabs: ReadonlySet<string>;
  dirtyTabs: ReadonlySet<string>;
  onSelect: (tab: string) => void;
};

const stageDescriptions: Record<string, string> = {
  prepare: "确定标题、产品和写作边界",
  writing: "完成文章骨架与正文版本",
  review: "检测、复检并审核修改建议",
  media: "选择图片并确认正文落位",
  delivery: "生成文件、检查并归档交付",
};

function StageStateIcon({
  active,
  completed,
  busy,
}: {
  active: boolean;
  completed: boolean;
  busy: boolean;
}) {
  if (busy) return <Loader2 className="size-3.5 animate-spin" />;
  if (completed) return <Check className="size-3.5" />;
  if (active) return <Circle className="size-2.5 fill-current" />;
  return null;
}

export function ArticleWorkflowNavigation({
  stages,
  activeStage,
  progressStageIndex,
  busyTabs,
  dirtyTabs,
  onSelect,
}: ArticleWorkflowNavigationProps) {
  return (
    <>
      <div className="rounded-xl border bg-card p-2 lg:hidden">
        <div className="grid grid-cols-2 gap-1 sm:grid-cols-5">
          {stages.map((stage, index) => {
            const active = activeStage === stage.value;
            const completed = index < progressStageIndex;
            const busy = stage.tabs.some((tab) => busyTabs.has(tab));
            const dirty = stage.tabs.some((tab) => dirtyTabs.has(tab));
            return (
              <Button
                key={stage.value}
                type="button"
                variant="ghost"
                onClick={() => onSelect(stage.tabs[0])}
                aria-current={active ? "step" : undefined}
                className={cn(
                  "h-11 justify-start gap-2 px-2.5",
                  active &&
                    "bg-primary text-primary-foreground hover:bg-primary/92 hover:text-primary-foreground",
                  !active && completed && "bg-accent/60 text-accent-foreground",
                )}
              >
                <span
                  className={cn(
                    "flex size-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold",
                    active && "border-primary-foreground/30",
                    completed && !active && "border-primary/20 bg-primary text-primary-foreground",
                  )}
                >
                  <StageStateIcon active={active} completed={completed} busy={busy} />
                  {!active && !completed && !busy ? stage.step : null}
                </span>
                <span className="truncate text-xs font-semibold">{stage.label}</span>
                {dirty && <span className="ml-auto size-1.5 rounded-full bg-amber-500" />}
              </Button>
            );
          })}
        </div>
      </div>

      <div className="hidden lg:block">
        <div className="px-4 pb-3 pt-4">
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
            Production flow
          </p>
          <h2 className="mt-1 text-sm font-semibold">文章生产流程</h2>
        </div>
        <div className="px-2 pb-3">
          {stages.map((stage, index) => {
            const active = activeStage === stage.value;
            const completed = index < progressStageIndex;
            const busy = stage.tabs.some((tab) => busyTabs.has(tab));
            const dirty = stage.tabs.some((tab) => dirtyTabs.has(tab));

            return (
              <div key={stage.value} className="relative">
                {index < stages.length - 1 && (
                  <span
                    className={cn(
                      "absolute left-[22px] top-10 h-[calc(100%-24px)] w-px bg-border",
                      completed && "bg-primary/30",
                    )}
                    aria-hidden="true"
                  />
                )}
                <button
                  type="button"
                  onClick={() => onSelect(stage.tabs[0])}
                  aria-current={active ? "step" : undefined}
                  className={cn(
                    "group relative flex w-full items-start gap-3 rounded-lg px-2.5 py-3 text-left transition-colors",
                    active
                      ? "bg-primary text-primary-foreground"
                      : "hover:bg-accent/55",
                  )}
                >
                  <span
                    className={cn(
                      "relative z-10 flex size-7 shrink-0 items-center justify-center rounded-full border bg-card text-xs font-semibold text-muted-foreground",
                      active &&
                        "border-primary-foreground/30 bg-primary-foreground/15 text-primary-foreground",
                      completed &&
                        !active &&
                        "border-primary bg-primary text-primary-foreground",
                    )}
                  >
                    <StageStateIcon active={active} completed={completed} busy={busy} />
                    {!active && !completed && !busy ? stage.step : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2 text-sm font-semibold">
                      {stage.label}
                      {dirty && (
                        <span
                          className="size-1.5 rounded-full bg-amber-500"
                          title="有未保存修改"
                        />
                      )}
                    </span>
                    <span
                      className={cn(
                        "mt-0.5 block text-xs leading-5 text-muted-foreground",
                        active && "text-primary-foreground/72",
                      )}
                    >
                      {stageDescriptions[stage.value]}
                    </span>
                  </span>
                </button>
              </div>
            );
          })}
        </div>
        <div className="border-t px-4 py-3">
          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>系统建议</span>
            <Badge variant="outline">
              节点 {Math.min(progressStageIndex + 1, stages.length)} / {stages.length}
            </Badge>
          </div>
        </div>
      </div>
    </>
  );
}
