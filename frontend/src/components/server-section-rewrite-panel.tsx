"use client";

import { FilePenLine, Loader2, Save } from "lucide-react";
import { useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { apiPut } from "@/lib/api";
import type { TaskRecord } from "@/types";

type ArticleSection = {
  key: string;
  level: number;
  path: string[];
  body: string;
};

type ServerSectionRewritePanelProps = {
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

const HEADING = /^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const FENCE = /^[ \t]{0,3}(`{3,}|~{3,})/;

function articleSections(markdown: string): ArticleSection[] {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const headings: Array<{
    lineIndex: number;
    level: number;
    path: string[];
  }> = [];
  const stack: Array<{ level: number; title: string }> = [];
  let fenceCharacter = "";
  let fenceLength = 0;

  lines.forEach((line, lineIndex) => {
    const fence = line.match(FENCE)?.[1] ?? "";
    if (fence) {
      if (!fenceCharacter) {
        fenceCharacter = fence[0];
        fenceLength = fence.length;
      } else if (
        fence[0] === fenceCharacter &&
        fence.length >= fenceLength
      ) {
        fenceCharacter = "";
        fenceLength = 0;
      }
      return;
    }
    if (fenceCharacter) return;
    const match = line.match(HEADING);
    if (!match) return;
    const level = match[1].length;
    const title = match[2].trim().replace(/\s+/g, " ");
    if (!title) return;
    if (level === 1) {
      stack.length = 0;
      return;
    }
    while (stack.length && stack.at(-1)!.level >= level) {
      stack.pop();
    }
    stack.push({ level, title });
    headings.push({
      lineIndex,
      level,
      path: stack.map((item) => item.title),
    });
  });

  return headings.map((heading, index) => {
    const nextBoundary = headings
      .slice(index + 1)
      .find((candidate) => candidate.level <= heading.level);
    const bodyEnd = nextBoundary?.lineIndex ?? lines.length;
    return {
      key: heading.path.join("\u001f"),
      level: heading.level,
      path: heading.path,
      body: lines.slice(heading.lineIndex + 1, bodyEnd).join("\n").trim(),
    };
  });
}

export function ServerSectionRewritePanel({
  task,
  taskApi,
  pending,
  editAllowed,
  updateAllowed,
  runAction,
}: ServerSectionRewritePanelProps) {
  const sections = useMemo(
    () => articleSections(task.initial_article ?? ""),
    [task.initial_article],
  );
  const [requestedSectionKey, setRequestedSectionKey] = useState("");
  const [replacementBody, setReplacementBody] = useState("");
  const section =
    sections.find((item) => item.key === requestedSectionKey) ??
    sections[0] ??
    null;
  const label = "保存章节重写";
  const busy = Boolean(pending);

  return (
    <Card className="xl:col-span-2">
      <CardHeader className="border-b">
        <CardTitle>章节级受控重写</CardTitle>
        <CardDescription>
          只替换一个 Markdown 标题下的正文；服务端保留标题、同级章节和全文版本，并重新执行结构与链接校验。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {!task.initial_article?.trim() || sections.length === 0 ? (
          <Alert>
            <FilePenLine />
            <AlertTitle>当前没有可定位章节</AlertTitle>
            <AlertDescription>
              先生成包含 H2/H3 的 Initial Article，再进行章节级编辑。
            </AlertDescription>
          </Alert>
        ) : (
          <>
            <div className="grid gap-2">
              <Label htmlFor="server-section-path">目标标题路径</Label>
              <select
                id="server-section-path"
                className="h-11 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                value={section?.key ?? ""}
                disabled={busy || !editAllowed}
                onChange={(event) => {
                  setRequestedSectionKey(event.target.value);
                  setReplacementBody("");
                }}
              >
                {sections.map((item) => (
                  <option key={item.key} value={item.key}>
                    H{item.level} · {item.path.join(" › ")}
                  </option>
                ))}
              </select>
              <p className="text-xs leading-5 text-muted-foreground">
                提交字段是标题路径数组，例如【产品选择 → 材料差异】；不提交字符偏移或整篇正文。
              </p>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor="server-section-current">当前章节正文</Label>
                <Textarea
                  id="server-section-current"
                  value={section?.body ?? ""}
                  readOnly
                  className="min-h-64 resize-y bg-muted/30 font-mono text-xs leading-5"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="server-section-replacement">
                  人工确认后的替换正文
                </Label>
                <Textarea
                  id="server-section-replacement"
                  value={replacementBody}
                  disabled={busy || !editAllowed}
                  className="min-h-64 resize-y font-mono text-xs leading-5"
                  placeholder="只填写标题下方的正文；不要重复目标标题。"
                  onChange={(event) => setReplacementBody(event.target.value)}
                />
              </div>
            </div>

            <div className="flex flex-wrap justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                disabled={busy || !editAllowed || !section}
                onClick={() => setReplacementBody(section?.body ?? "")}
              >
                复制当前正文到编辑区
              </Button>
              <Button
                type="button"
                className="min-h-11"
                disabled={
                  busy ||
                  !editAllowed ||
                  !updateAllowed ||
                  !section ||
                  !replacementBody.trim() ||
                  replacementBody.trim() === section.body.trim()
                }
                onClick={() => {
                  if (!section) return;
                  void runAction(
                    label,
                    () =>
                      apiPut<TaskRecord>(`${taskApi}/article/sections`, {
                        revision: task.revision ?? 0,
                        heading_path: section.path,
                        replacement_body: replacementBody,
                      }),
                    "章节已替换；服务端已保存前后版本并使下游产物失效。",
                  );
                }}
              >
                {pending === label ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <Save />
                )}
                保存章节重写
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
