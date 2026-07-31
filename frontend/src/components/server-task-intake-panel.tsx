"use client";

import {
  AlertCircle,
  CheckCircle2,
  FileInput,
  Loader2,
  Plus,
  ShieldCheck,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { apiGet, apiPost } from "@/lib/api";
import type {
  AccessibleProject,
  ServerTaskIntakeResponse,
} from "@/types";

type IntakeRow = {
  topic: string;
  competitor_keyword: string;
  competitor_blog: string;
};

const EDIT_ROLES = new Set<AccessibleProject["effective_role"]>([
  "org_admin",
  "team_lead",
  "editor",
]);

function messageFrom(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Task 创建或导入失败，请稍后重试。";
}

function newIntakeId(prefix: string) {
  if (typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `${prefix}-${Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("")}`;
}

function parseRows(value: string): {
  rows: IntakeRow[];
  error: string;
} {
  const rows: IntakeRow[] = [];
  for (const [index, rawLine] of value.split(/\r?\n/).entries()) {
    if (!rawLine.trim()) continue;
    const columns = rawLine.split("\t");
    if (columns.length > 3) {
      return {
        rows: [],
        error: `第 ${index + 1} 行超过 3 列，请使用 Tab 分隔。`,
      };
    }
    const [topic = "", competitorKeyword = "", competitorBlog = ""] =
      columns.map((column) => column.trim());
    if (!topic) {
      return {
        rows: [],
        error: `第 ${index + 1} 行缺少话题。`,
      };
    }
    rows.push({
      topic,
      competitor_keyword: competitorKeyword,
      competitor_blog: competitorBlog,
    });
  }
  if (rows.length > 200) {
    return {
      rows: [],
      error: "一次最多导入 200 行。",
    };
  }
  return { rows, error: "" };
}

function IntakeSuccess({
  result,
}: {
  result: ServerTaskIntakeResponse;
}) {
  return (
    <Alert>
      <CheckCircle2 />
      <AlertTitle>
        {result.created ? "Task 已提交" : "已识别为安全重试"}
      </AlertTitle>
      <AlertDescription className="grid gap-1">
        <span>
          {result.tasks.length} 条 Task，编号{" "}
          {result.tasks
            .map((task) => `topic_${String(task.topic_index).padStart(3, "0")}`)
            .join("、")}
        </span>
        <span className="font-mono text-xs">
          Source digest: {result.source_digest.slice(0, 12)}…
        </span>
      </AlertDescription>
    </Alert>
  );
}

export function ServerTaskIntakePanel({
  customer,
  onCompleted,
}: {
  customer: string;
  onCompleted: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [role, setRole] =
    useState<AccessibleProject["effective_role"] | null>(null);
  const [roleError, setRoleError] = useState("");
  const [pending, setPending] = useState<"manual" | "import" | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ServerTaskIntakeResponse | null>(null);

  const [topic, setTopic] = useState("");
  const [keyword, setKeyword] = useState("");
  const [competitorUrl, setCompetitorUrl] = useState("");
  const [manualIntakeId, setManualIntakeId] = useState("");

  const [sourceName, setSourceName] = useState("");
  const [rowText, setRowText] = useState("");
  const [importIntakeId, setImportIntakeId] = useState("");

  useEffect(() => {
    let active = true;
    void apiGet<AccessibleProject[]>("/api/projects")
      .then((projects) => {
        if (!active) return;
        const current = projects.find(
          (project) => project.project_id === customer,
        );
        setRole(current?.effective_role ?? null);
        setRoleError(
          current ? "" : "当前 Server Directory 中没有这个 Project。",
        );
      })
      .catch((reason) => {
        if (!active) return;
        setRole(null);
        setRoleError(messageFrom(reason));
      });
    return () => {
      active = false;
    };
  }, [customer]);

  const parsedImport = useMemo(() => parseRows(rowText), [rowText]);
  const canEdit = role !== null && EDIT_ROLES.has(role);

  function resetFeedback() {
    setError("");
    setResult(null);
  }

  function changeManual(
    setter: (value: string) => void,
    value: string,
  ) {
    setter(value);
    setManualIntakeId("");
    resetFeedback();
  }

  function changeImport(
    setter: (value: string) => void,
    value: string,
  ) {
    setter(value);
    setImportIntakeId("");
    resetFeedback();
  }

  async function submitManual(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit || !topic.trim() || pending) return;
    const intakeId = manualIntakeId || newIntakeId("manual");
    if (!manualIntakeId) setManualIntakeId(intakeId);
    setPending("manual");
    resetFeedback();
    try {
      const response = await apiPost<ServerTaskIntakeResponse>(
        `/api/projects/${encodeURIComponent(customer)}/tasks`,
        {
          intake_id: intakeId,
          topic: topic.trim(),
          competitor_keyword: keyword.trim(),
          competitor_blog: competitorUrl.trim(),
        },
      );
      setResult(response);
      await onCompleted();
      if (response.created) {
        setTopic("");
        setKeyword("");
        setCompetitorUrl("");
        setManualIntakeId("");
      }
    } catch (reason) {
      setError(messageFrom(reason));
    } finally {
      setPending(null);
    }
  }

  async function submitImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !canEdit ||
      !sourceName.trim() ||
      !parsedImport.rows.length ||
      parsedImport.error ||
      pending
    ) {
      return;
    }
    const intakeId = importIntakeId || newIntakeId("import");
    if (!importIntakeId) setImportIntakeId(intakeId);
    setPending("import");
    resetFeedback();
    try {
      const response = await apiPost<ServerTaskIntakeResponse>(
        `/api/projects/${encodeURIComponent(customer)}/task-imports`,
        {
          intake_id: intakeId,
          source_name: sourceName.trim(),
          rows: parsedImport.rows,
        },
      );
      setResult(response);
      await onCompleted();
      if (response.created) {
        setSourceName("");
        setRowText("");
        setImportIntakeId("");
      }
    } catch (reason) {
      setError(messageFrom(reason));
    } finally {
      setPending(null);
    }
  }

  return (
    <Card className="gap-0 py-0">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CardHeader className="border-b px-4 py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <CardTitle className="flex items-center gap-2">
                <Plus className="size-4 text-primary" />
                Task Intake
                <Badge variant="outline">
                  <ShieldCheck />
                  Project-scoped
                </Badge>
              </CardTitle>
              <CardDescription className="mt-1 max-w-3xl">
                Server 分配 Task ID 和 topic 序号；浏览器只提交话题行与幂等键。
                不读取 Local XLSX、任务目录或 SQLite。
              </CardDescription>
            </div>
            <CollapsibleTrigger
              render={
                <Button type="button" variant={open ? "secondary" : "default"} />
              }
            >
              {open ? "收起" : "新建 / 导入"}
            </CollapsibleTrigger>
          </div>
        </CardHeader>
        <CollapsibleContent>
          <CardContent className="grid gap-4 px-4 py-4">
            {roleError && (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertTitle>无法确认编辑权限</AlertTitle>
                <AlertDescription>{roleError}</AlertDescription>
              </Alert>
            )}
            {role && !canEdit && (
              <Alert>
                <ShieldCheck />
                <AlertTitle>当前角色只读</AlertTitle>
                <AlertDescription>
                  Reviewer/Viewer 可以查看文章，但 Task 创建和导入需要
                  Editor、Team Lead 或 Org Admin。最终授权仍由后端实时判断。
                </AlertDescription>
              </Alert>
            )}
            {error && (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertTitle>Task Intake 失败</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            {result && <IntakeSuccess result={result} />}

            <Tabs defaultValue="manual">
              <TabsList className="h-auto min-h-11 flex-wrap justify-start">
                <TabsTrigger value="manual" className="min-h-10">
                  单条创建
                </TabsTrigger>
                <TabsTrigger value="import" className="min-h-10">
                  行数据导入
                </TabsTrigger>
              </TabsList>

              <TabsContent value="manual" className="pt-4">
                <form className="grid gap-4" onSubmit={submitManual}>
                  <div className="grid gap-2">
                    <Label htmlFor="server-task-topic">文章话题</Label>
                    <Input
                      id="server-task-topic"
                      value={topic}
                      maxLength={500}
                      required
                      disabled={!canEdit || pending !== null}
                      onChange={(event) =>
                        changeManual(setTopic, event.target.value)
                      }
                      placeholder="例如：How to choose stainless steel bolt grades"
                    />
                  </div>
                  <div className="grid gap-4 lg:grid-cols-2">
                    <div className="grid gap-2">
                      <Label htmlFor="server-task-keyword">
                        竞对关键词（可选）
                      </Label>
                      <Input
                        id="server-task-keyword"
                        value={keyword}
                        maxLength={500}
                        disabled={!canEdit || pending !== null}
                        onChange={(event) =>
                          changeManual(setKeyword, event.target.value)
                        }
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="server-task-url">
                        竞对文章 URL（可选）
                      </Label>
                      <Input
                        id="server-task-url"
                        value={competitorUrl}
                        type="url"
                        maxLength={2048}
                        disabled={!canEdit || pending !== null}
                        onChange={(event) =>
                          changeManual(setCompetitorUrl, event.target.value)
                        }
                        placeholder="https://..."
                      />
                    </div>
                  </div>
                  <div className="flex justify-end">
                    <Button
                      type="submit"
                      className="min-h-11"
                      disabled={!canEdit || !topic.trim() || pending !== null}
                    >
                      {pending === "manual" ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <Plus />
                      )}
                      创建 Task
                    </Button>
                  </div>
                </form>
              </TabsContent>

              <TabsContent value="import" className="pt-4">
                <form className="grid gap-4" onSubmit={submitImport}>
                  <div className="grid gap-2">
                    <Label htmlFor="server-task-source-name">来源名称</Label>
                    <Input
                      id="server-task-source-name"
                      value={sourceName}
                      maxLength={255}
                      required
                      disabled={!canEdit || pending !== null}
                      onChange={(event) =>
                        changeImport(setSourceName, event.target.value)
                      }
                      placeholder="例如：2026-Q3-topic-rows.tsv"
                    />
                    <p className="text-xs text-muted-foreground">
                      只记录来源标签、摘要和 Task ID，不上传或保存原始文件。
                    </p>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="server-task-import-rows">
                      话题行（最多 200 行）
                    </Label>
                    <Textarea
                      id="server-task-import-rows"
                      value={rowText}
                      rows={9}
                      disabled={!canEdit || pending !== null}
                      onChange={(event) =>
                        changeImport(setRowText, event.target.value)
                      }
                      placeholder={
                        "话题<Tab>竞对关键词<Tab>https://竞对文章\n第二个话题"
                      }
                    />
                    <p className="text-xs text-muted-foreground">
                      每行 1 个 Task；可用 Tab 依次分隔“话题、竞对关键词、HTTP(S)
                      URL”。不支持逗号 CSV，避免含逗号话题被错误拆分。
                    </p>
                    <p
                      className={
                        parsedImport.error
                          ? "text-xs text-destructive"
                          : "text-xs text-muted-foreground"
                      }
                      aria-live="polite"
                    >
                      {parsedImport.error ||
                        `已识别 ${parsedImport.rows.length} 行。`}
                    </p>
                  </div>
                  <div className="flex justify-end">
                    <Button
                      type="submit"
                      className="min-h-11"
                      disabled={
                        !canEdit ||
                        !sourceName.trim() ||
                        !parsedImport.rows.length ||
                        Boolean(parsedImport.error) ||
                        pending !== null
                      }
                    >
                      {pending === "import" ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <FileInput />
                      )}
                      {parsedImport.rows.length
                        ? `导入 ${parsedImport.rows.length} 条 Task`
                        : "导入 Task"}
                    </Button>
                  </div>
                </form>
              </TabsContent>
            </Tabs>
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}
