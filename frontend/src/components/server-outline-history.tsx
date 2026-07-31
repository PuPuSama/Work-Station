"use client";

import { History, Loader2, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { apiPost } from "@/lib/api";
import type { TaskRecord } from "@/types";

type ServerOutlineHistoryProps = {
  task: TaskRecord;
  taskApi: string;
  pending: string;
  editAllowed: boolean;
  updateAllowed: boolean;
  runAction: (
    label: string,
    action: () => Promise<unknown>,
    successMessage?: string,
  ) => Promise<unknown>;
};

function sourceLabel(source: string) {
  if (source === "generated") return "模型草稿";
  if (source === "manual_confirmed") return "人工确认";
  if (source === "manual_draft") return "人工草稿";
  if (source === "restored") return "版本恢复";
  return source || "未知来源";
}

export function ServerOutlineHistory({
  task,
  taskApi,
  pending,
  editAllowed,
  updateAllowed,
  runAction,
}: ServerOutlineHistoryProps) {
  const versions = (task.article_versions ?? [])
    .map((version, versionIndex) => ({ version, versionIndex }))
    .filter(({ version }) =>
      ["outline", "outline_draft"].includes(version.kind),
    )
    .reverse();

  return (
    <section className="grid gap-3 rounded-lg border bg-muted/20 p-4">
      <div className="flex items-start gap-3">
        <History className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div>
          <h3 className="text-sm font-semibold">大纲版本记录</h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            恢复命令只提交服务端版本索引，不回传历史正文。恢复结果进入可编辑草稿，不会自动确认大纲。
          </p>
        </div>
      </div>
      {versions.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
          还没有可恢复的大纲版本。
        </p>
      ) : (
        <div className="grid gap-2">
          {versions.map(({ version, versionIndex }) => {
            const label = `恢复大纲版本 ${versionIndex}`;
            return (
              <div
                key={`${versionIndex}-${version.content_hash}`}
                className="grid gap-3 rounded-md border bg-background p-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="secondary">
                      {version.kind === "outline" ? "已确认" : "草稿"}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      #{versionIndex} · {sourceLabel(version.source_kind)} ·{" "}
                      {version.word_count} 词
                    </span>
                  </div>
                  <p className="mt-2 line-clamp-2 whitespace-pre-wrap text-sm leading-6">
                    {version.content}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  disabled={
                    Boolean(pending) || !editAllowed || !updateAllowed
                  }
                  onClick={() =>
                    void runAction(
                      label,
                      () =>
                        apiPost<TaskRecord>(
                          `${taskApi}/outline/restore-version`,
                          {
                            revision: task.revision ?? 0,
                            version_index: versionIndex,
                          },
                        ),
                      "已从服务端版本恢复到大纲草稿；请审阅后再确认。",
                    )
                  }
                >
                  {pending === label ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <RotateCcw />
                  )}
                  恢复为草稿
                </Button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
