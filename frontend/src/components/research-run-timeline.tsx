"use client";

import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Search,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type {
  ResearchGapFillAttempt,
  ResearchRunEvent,
} from "@/types";

type ResearchRunTimelineProps = {
  events: ResearchRunEvent[];
  attempts: ResearchGapFillAttempt[];
};

const eventLabels: Record<string, string> = {
  queued: "已进入研究队列",
  node_completed: "节点完成",
  interrupted: "等待人工复核",
  resumed: "人工复核后继续",
  failed: "执行失败",
  completed: "研究完成",
  tool_call: "工具调用",
};

function formatTime(value: string | null) {
  if (!value) return "时间待记录";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function detailSummary(details: Record<string, unknown>) {
  const duration =
    typeof details.duration_ms === "number"
      ? `${Math.round(details.duration_ms)} ms`
      : null;
  const outcome =
    typeof details.outcome === "string" ? details.outcome : null;
  return [outcome, duration].filter(Boolean).join(" · ");
}

function costSummary(cost: Record<string, unknown>) {
  return Object.entries(cost)
    .filter(
      (entry): entry is [string, string | number] =>
        typeof entry[1] === "string" || typeof entry[1] === "number",
    )
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");
}

export function ResearchRunTimeline({
  events,
  attempts,
}: ResearchRunTimelineProps) {
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <h3 className="text-sm font-semibold">执行时间线</h3>
        {events.length ? (
          <ol className="grid gap-1" aria-label="研究执行事件">
            {events.map((event) => {
              const failed =
                event.event_type === "failed" ||
                event.details.outcome === "failed";
              return (
                <li
                  key={event.sequence}
                  className="grid min-h-12 grid-cols-[24px_minmax(0,1fr)_auto] items-start gap-2 rounded-lg px-2 py-2 hover:bg-muted/40"
                >
                  <span
                    className="mt-0.5 flex size-6 items-center justify-center rounded-full border bg-card"
                    aria-hidden="true"
                  >
                    {failed ? (
                      <AlertTriangle className="size-3.5 text-destructive" />
                    ) : (
                      <CheckCircle2 className="size-3.5 text-primary" />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm font-medium">
                      {eventLabels[event.event_type] ?? event.event_type}
                    </span>
                    <span className="mt-0.5 block font-mono text-[11px] text-muted-foreground">
                      {event.node_name}
                      {event.scope_id ? ` · ${event.scope_id}` : ""}
                      {event.attempt > 1 ? ` · attempt ${event.attempt}` : ""}
                    </span>
                    {detailSummary(event.details) ? (
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        {detailSummary(event.details)}
                      </span>
                    ) : null}
                  </span>
                  <span className="whitespace-nowrap text-[11px] text-muted-foreground">
                    {formatTime(event.created_at)}
                  </span>
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            还没有结构化事件。
          </p>
        )}
      </div>

      {attempts.length ? (
        <Collapsible>
          <CollapsibleTrigger className="flex min-h-11 w-full items-center gap-2 rounded-lg border px-3 text-left text-sm font-medium hover:bg-muted/40">
            <Search className="size-4" />
            补证记录
            <Badge variant="outline">{attempts.length}</Badge>
            <ChevronDown className="ml-auto size-4" />
          </CollapsibleTrigger>
          <CollapsibleContent className="grid gap-2 pt-2">
            {attempts.map((attempt) => (
              <article
                key={attempt.attempt_id}
                className="rounded-lg border bg-muted/15 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="secondary">第 {attempt.round_number} 轮</Badge>
                  <span className="font-mono text-xs">{attempt.scope_id}</span>
                  <Badge variant="outline">{attempt.result}</Badge>
                  <span className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <Clock3 className="size-3" />
                    {formatTime(attempt.updated_at)}
                  </span>
                </div>
                <p className="mt-2 text-sm">{attempt.reason}</p>
                <p className="mt-1 break-words font-mono text-xs text-muted-foreground">
                  {attempt.query}
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  发现 {attempt.discovered_urls.length} 个 URL · 发布{" "}
                  {attempt.published_source_ids.length} 个来源
                </p>
                {costSummary(attempt.cost_usage) ? (
                  <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                    {costSummary(attempt.cost_usage)}
                  </p>
                ) : null}
              </article>
            ))}
          </CollapsibleContent>
        </Collapsible>
      ) : null}
    </div>
  );
}
