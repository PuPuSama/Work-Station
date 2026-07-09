"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  ArrowLeft,
  CheckCircle2,
  Download,
  ExternalLink,
  FileText,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  WandSparkles,
  X,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ApiMessage,
  DashboardSummary,
  Product,
  PublicConfig,
  TaskRecord,
  WorkflowStatus,
} from "@/types";

const STATUS_LABELS: Record<WorkflowStatus, string> = {
  new: "待生成标题",
  titles_ready: "待选标题",
  title_selected: "已选标题",
  outline_ready: "大纲待确认",
  draft_ready: "正文待处理",
  docx_exported: "已导出 Word",
};

const STATUS_FILTERS: Array<"all" | WorkflowStatus> = [
  "all",
  "new",
  "titles_ready",
  "outline_ready",
  "draft_ready",
  "docx_exported",
];

function emptyProduct(): Product {
  return { name: "", url: "", image_path: "", description: "" };
}

function statusLabel(status: string) {
  return STATUS_LABELS[status as WorkflowStatus] ?? status;
}

function statusVariant(status: WorkflowStatus) {
  if (status === "docx_exported") return "default";
  if (status === "draft_ready" || status === "outline_ready") return "secondary";
  if (status === "titles_ready" || status === "title_selected") return "outline";
  return "ghost";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown error";
}

export function ArticleWorkbench({ customer }: { customer?: string }) {
  const [dashboard, setDashboard] = useState<DashboardSummary | null>(null);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [selectedTask, setSelectedTask] = useState<TaskRecord | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | WorkflowStatus>("all");
  const [query, setQuery] = useState("");
  const [titleChoice, setTitleChoice] = useState("");
  const [outlineText, setOutlineText] = useState("");
  const [articleText, setArticleText] = useState("");
  const [zeroGptReport, setZeroGptReport] = useState("");
  const [products, setProducts] = useState<Product[]>([emptyProduct()]);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const projectName = customer ? decodeURIComponent(customer) : "";

  const loadData = useCallback(async (preferredTaskId?: string) => {
    const taskPath = projectName
      ? `/api/tasks?customer=${encodeURIComponent(projectName)}`
      : "/api/tasks";
    const [nextDashboard, nextConfig, nextTasks] = await Promise.all([
      apiGet<DashboardSummary>("/api/dashboard"),
      apiGet<PublicConfig>("/api/config"),
      apiGet<TaskRecord[]>(taskPath),
    ]);
    setDashboard(nextDashboard);
    setConfig(nextConfig);
    setTasks(nextTasks);
    setSelectedTask((current) => {
      const preferred = preferredTaskId ?? current?.id;
      return (
        nextTasks.find((task) => task.id === preferred) ??
        nextTasks[0] ??
        null
      );
    });
  }, [projectName]);

  useEffect(() => {
    loadData().catch((err) => setError(errorMessage(err)));
  }, [loadData]);

  useEffect(() => {
    setTitleChoice(selectedTask?.selected_title || "");
    setOutlineText(selectedTask?.outline || "");
    setArticleText(selectedTask?.article || "");
    setZeroGptReport(selectedTask?.zero_gpt_report || "");
    setProducts(
      selectedTask?.products?.length ? selectedTask.products : [emptyProduct()],
    );
  }, [selectedTask]);

  const filteredTasks = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return tasks.filter((task) => {
      const matchesStatus =
        statusFilter === "all" || task.status === statusFilter;
      const haystack = [
        task.customer,
        task.topic,
        task.competitor_keyword,
        task.competitor_blog,
        task.selected_title,
      ]
        .join(" ")
        .toLowerCase();
      return matchesStatus && (!normalizedQuery || haystack.includes(normalizedQuery));
    });
  }, [query, statusFilter, tasks]);

  const completedCount = useMemo(
    () => tasks.filter((task) => task.status === "docx_exported").length,
    [tasks],
  );
  const pendingCount = tasks.length - completedCount;
  const completion = tasks.length
    ? Math.round((completedCount / tasks.length) * 100)
    : 0;

  async function runAction<T>(
    label: string,
    action: () => Promise<T>,
    after?: (result: T) => void,
  ) {
    setBusy(label);
    setError("");
    setMessage("");
    try {
      const result = await action();
      after?.(result);
      const maybeTask = result as TaskRecord;
      await loadData(maybeTask?.id ?? selectedTask?.id);
      setMessage(label);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function initializeWeek() {
    await runAction<ApiMessage>(
      "本周任务已初始化",
      () => apiPost<ApiMessage>("/api/init-week"),
      (result) => setMessage(result.message),
    );
  }

  function persistedProducts() {
    return products.filter((product) =>
      [product.name, product.url, product.image_path, product.description].some(
        (value) => value.trim(),
      ),
    );
  }

  function updateProduct(index: number, key: keyof Product, value: string) {
    setProducts((current) =>
      current.map((product, productIndex) =>
        productIndex === index ? { ...product, [key]: value } : product,
      ),
    );
  }

  const selectedId = selectedTask?.id;
  const isBusy = Boolean(busy);

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-[color-mix(in_oklch,var(--background),var(--accent)_22%)]">
        <div className="mx-auto flex max-w-[1500px] flex-col gap-4 px-5 py-5 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {projectName && (
                <Button variant="outline" size="icon-sm" render={<Link href="/" />}>
                  <ArrowLeft />
                </Button>
              )}
              <h1 className="text-xl font-semibold tracking-normal">
                {projectName || "Article Workflow Agent"}
              </h1>
              <Badge variant={dashboard?.llm_ready ? "default" : "outline"}>
                {dashboard?.llm_ready ? "LLM Ready" : "Mock LLM"}
              </Badge>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
              <span>{dashboard?.week_folder ?? "未初始化"}</span>
              <span>{dashboard?.week_path ?? config?.current_week_path}</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              onClick={() => runAction("数据已刷新", () => loadData())}
              disabled={isBusy}
            >
              <RefreshCw />
              刷新
            </Button>
            <Button onClick={initializeWeek} disabled={isBusy}>
              {busy ? <Loader2 className="animate-spin" /> : <Sparkles />}
              初始化本周任务
            </Button>
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-[1500px] gap-4 px-5 py-5">
        {(error || message) && (
          <Alert
            className={cn(
              "rounded-lg",
              error
                ? "border-destructive/40 bg-destructive/5"
                : "border-emerald-600/30 bg-emerald-50",
            )}
          >
            <AlertTitle>{error ? "操作失败" : "状态"}</AlertTitle>
            <AlertDescription>{error || message}</AlertDescription>
          </Alert>
        )}

        <section className="grid gap-3 md:grid-cols-4">
          <SummaryCard
            title={projectName ? "项目任务" : "客户"}
            value={projectName ? tasks.length : dashboard?.customer_count ?? 0}
          />
          <SummaryCard
            title={projectName ? "待处理" : "任务"}
            value={projectName ? pendingCount : dashboard?.task_count ?? 0}
          />
          <SummaryCard title="已导出" value={completedCount} />
          <Card className="rounded-lg">
            <CardHeader>
              <CardTitle>完成率</CardTitle>
              <CardDescription>{completion}%</CardDescription>
            </CardHeader>
            <CardContent>
              <Progress value={completion} />
            </CardContent>
          </Card>
        </section>

        <section className="grid min-h-[720px] gap-4 xl:grid-cols-[minmax(460px,0.95fr)_minmax(0,1.05fr)]">
          <Card className="min-w-0 rounded-lg">
            <CardHeader className="border-b">
              <CardTitle>任务队列</CardTitle>
              <CardDescription>
                {filteredTasks.length} / {tasks.length}
              </CardDescription>
              <CardAction>
                <Input
                  className="h-8 w-[220px]"
                  placeholder={projectName ? "搜索话题、标题、竞品" : "搜索客户、话题、标题"}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </CardAction>
            </CardHeader>
            <CardContent className="grid gap-3">
              <div className="flex flex-wrap gap-2">
                {STATUS_FILTERS.map((status) => (
                  <Button
                    key={status}
                    size="sm"
                    variant={statusFilter === status ? "default" : "outline"}
                    onClick={() => setStatusFilter(status)}
                  >
                    {status === "all" ? "全部" : statusLabel(status)}
                  </Button>
                ))}
              </div>
              <ScrollArea className="h-[585px] rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      {projectName && <TableHead className="w-[100px]">编号</TableHead>}
                      {!projectName && <TableHead className="w-[170px]">客户</TableHead>}
                      <TableHead>话题</TableHead>
                      <TableHead className="w-[120px]">状态</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredTasks.map((task) => (
                      <TableRow
                        key={task.id}
                        className={cn(
                          "cursor-pointer",
                          selectedTask?.id === task.id && "bg-accent/60",
                        )}
                        onClick={() => setSelectedTask(task)}
                      >
                        {projectName ? (
                          <TableCell className="text-xs text-muted-foreground">
                            topic_{String(task.topic_index).padStart(3, "0")}
                          </TableCell>
                        ) : (
                          <TableCell>
                            <div className="font-medium">{task.customer}</div>
                            <div className="text-xs text-muted-foreground">
                              topic_{String(task.topic_index).padStart(3, "0")}
                            </div>
                          </TableCell>
                        )}
                        <TableCell>
                          <div className="line-clamp-2 max-w-[360px]">
                            {task.topic}
                          </div>
                          {task.selected_title && (
                            <div className="mt-1 line-clamp-1 text-xs text-muted-foreground">
                              {task.selected_title}
                            </div>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge variant={statusVariant(task.status)}>
                            {statusLabel(task.status)}
                          </Badge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ScrollArea>
            </CardContent>
          </Card>

          <Card className="min-w-0 rounded-lg">
            <CardHeader className="border-b">
              <CardTitle>任务处理</CardTitle>
              <CardDescription>
                {selectedTask
                  ? `${selectedTask.customer} / topic_${String(
                      selectedTask.topic_index,
                    ).padStart(3, "0")}`
                  : "未选择任务"}
              </CardDescription>
              {selectedTask && (
                <CardAction>
                  <Badge variant={statusVariant(selectedTask.status)}>
                    {statusLabel(selectedTask.status)}
                  </Badge>
                </CardAction>
              )}
            </CardHeader>
            <CardContent>
              {!selectedTask ? (
                <div className="flex h-[610px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
                  初始化本周任务后选择一行
                </div>
              ) : (
                <Tabs defaultValue="titles" className="h-full min-w-0">
                  <TabsList className="grid h-auto w-full grid-cols-3 gap-1 sm:grid-cols-6">
                    <TabsTrigger value="titles">标题</TabsTrigger>
                    <TabsTrigger value="products">产品</TabsTrigger>
                    <TabsTrigger value="outline">大纲</TabsTrigger>
                    <TabsTrigger value="article">正文</TabsTrigger>
                    <TabsTrigger value="zerogpt">ZeroGPT</TabsTrigger>
                    <TabsTrigger value="files">文件</TabsTrigger>
                  </TabsList>

                  <TabsContent value="titles" className="min-w-0 pt-4">
                    <div className="grid gap-4">
                      <TaskBrief task={selectedTask} />
                      <div className="flex flex-wrap gap-2">
                        <Button
                          onClick={() =>
                            selectedId &&
                            runAction("标题已生成", () =>
                              apiPost<TaskRecord>(`/api/tasks/${selectedId}/titles`),
                            )
                          }
                          disabled={isBusy}
                        >
                          <WandSparkles />
                          生成 10 个标题
                        </Button>
                        <Button
                          variant="outline"
                          onClick={() =>
                            selectedId &&
                            runAction("标题已保存", () =>
                              apiPost<TaskRecord>(
                                `/api/tasks/${selectedId}/select-title`,
                                { title: titleChoice },
                              ),
                            )
                          }
                          disabled={isBusy || !titleChoice}
                        >
                          <CheckCircle2 />
                          选用标题
                        </Button>
                      </div>
                      <ScrollArea className="h-[470px] pr-3">
                      <RadioGroup
                        value={titleChoice}
                        onValueChange={setTitleChoice}
                        className="gap-2"
                      >
                        {selectedTask.title_candidates.length ? (
                          selectedTask.title_candidates.map((title) => (
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
                  </TabsContent>

                  <TabsContent value="products" className="min-w-0 pt-4">
                    <div className="grid gap-3">
                      <div className="flex flex-wrap justify-between gap-2">
                        <Button
                          variant="outline"
                          onClick={() => setProducts((current) => [...current, emptyProduct()])}
                          disabled={isBusy}
                        >
                          <Plus />
                          添加产品
                        </Button>
                        <Button
                          onClick={() =>
                            selectedId &&
                            runAction("产品已保存", () =>
                              apiPut<TaskRecord>(`/api/tasks/${selectedId}/products`, {
                                products: persistedProducts(),
                              }),
                            )
                          }
                          disabled={isBusy}
                        >
                          <Save />
                          保存产品
                        </Button>
                      </div>
                      <ScrollArea className="h-[520px] pr-3">
                        <div className="grid gap-3">
                          {products.map((product, index) => (
                            <div key={index} className="rounded-lg border p-3">
                              <div className="mb-3 flex items-center justify-between">
                                <div className="font-medium">产品 {index + 1}</div>
                                <Button
                                  size="icon-sm"
                                  variant="ghost"
                                  onClick={() =>
                                    setProducts((current) =>
                                      current.filter((_, productIndex) => productIndex !== index),
                                    )
                                  }
                                  disabled={products.length === 1}
                                >
                                  <X />
                                </Button>
                              </div>
                              <div className="grid gap-3 md:grid-cols-2">
                                <Field
                                  label="产品名"
                                  value={product.name}
                                  onChange={(value) => updateProduct(index, "name", value)}
                                />
                                <Field
                                  label="产品 URL"
                                  value={product.url}
                                  onChange={(value) => updateProduct(index, "url", value)}
                                />
                                <Field
                                  label="图片路径"
                                  value={product.image_path}
                                  onChange={(value) => updateProduct(index, "image_path", value)}
                                />
                                <div className="grid gap-2 md:col-span-2">
                                  <Label>描述</Label>
                                  <Textarea
                                    value={product.description}
                                    onChange={(event) =>
                                      updateProduct(index, "description", event.target.value)
                                    }
                                    className="h-[86px] resize-none overflow-y-auto"
                                  />
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                      </ScrollArea>
                    </div>
                  </TabsContent>

                  <TabsContent value="outline" className="min-w-0 pt-4">
                    <EditorPanel
                      value={outlineText}
                      onChange={setOutlineText}
                      placeholder="生成或编辑大纲"
                      height="h-[480px]"
                      actions={
                        <>
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("大纲已生成", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/outline`),
                              )
                            }
                            disabled={isBusy}
                          >
                            <WandSparkles />
                            生成大纲
                          </Button>
                          <Button
                            variant="outline"
                            onClick={() =>
                              selectedId &&
                              runAction("大纲已保存", () =>
                                apiPut<TaskRecord>(`/api/tasks/${selectedId}/outline`, {
                                  outline: outlineText,
                                }),
                              )
                            }
                            disabled={isBusy || !outlineText.trim()}
                          >
                            <Save />
                            保存大纲
                          </Button>
                        </>
                      }
                    />
                  </TabsContent>

                  <TabsContent value="article" className="min-w-0 pt-4">
                    <EditorPanel
                      value={articleText}
                      onChange={setArticleText}
                      placeholder="生成或编辑正文"
                      height="h-[500px]"
                      actions={
                        <>
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("正文已生成", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/article`, {
                                  word_count: config?.article.default_word_count ?? 1500,
                                }),
                              )
                            }
                            disabled={isBusy}
                          >
                            <FileText />
                            生成正文
                          </Button>
                          <Button
                            variant="outline"
                            onClick={() =>
                              selectedId &&
                              runAction("正文已保存", () =>
                                apiPut<TaskRecord>(`/api/tasks/${selectedId}/article`, {
                                  article: articleText,
                                }),
                              )
                            }
                            disabled={isBusy || !articleText.trim()}
                          >
                            <Save />
                            保存正文
                          </Button>
                          <Button
                            variant="secondary"
                            onClick={() =>
                              selectedId &&
                              runAction("Word 已导出", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/export-docx`),
                              )
                            }
                            disabled={isBusy || !articleText.trim()}
                          >
                            <Download />
                            导出 Word
                          </Button>
                        </>
                      }
                    />
                  </TabsContent>

                  <TabsContent value="zerogpt" className="min-w-0 pt-4">
                    <EditorPanel
                      value={zeroGptReport}
                      onChange={setZeroGptReport}
                      placeholder="粘贴 ZeroGPT 分数或报告"
                      height="h-[430px]"
                      actions={
                        <>
                          <Button
                            variant="outline"
                            onClick={() =>
                              selectedId &&
                              runAction("ZeroGPT 报告已保存", () =>
                                apiPut<TaskRecord>(`/api/tasks/${selectedId}/zerogpt`, {
                                  report: zeroGptReport,
                                }),
                              )
                            }
                            disabled={isBusy}
                          >
                            <Save />
                            保存报告
                          </Button>
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("正文已优化", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/humanize`),
                              )
                            }
                            disabled={isBusy || !selectedTask.article}
                          >
                            <WandSparkles />
                            优化正文
                          </Button>
                        </>
                      }
                    />
                  </TabsContent>

                  <TabsContent value="files" className="min-w-0 pt-4">
                    <div className="grid gap-3 text-sm">
                      <FileRow label="任务目录" value={selectedTask.task_dir} />
                      <FileRow label="Word 文件" value={selectedTask.docx_path || "未导出"} />
                      <FileRow label="话题库" value={config?.topic_library ?? ""} />
                      <FileRow label="知识库" value={config?.knowledge_base ?? ""} />
                      <Separator />
                      <div className="grid gap-1">
                        <span className="font-medium">竞品关键词 / 网站</span>
                        <span className="text-muted-foreground">
                          {selectedTask.competitor_keyword || "空"}
                        </span>
                      </div>
                      <div className="grid gap-1">
                        <span className="font-medium">竞品 Blog</span>
                        <span className="break-all text-muted-foreground">
                          {selectedTask.competitor_blog || "空"}
                        </span>
                      </div>
                    </div>
                  </TabsContent>
                </Tabs>
              )}
            </CardContent>
          </Card>
        </section>
      </div>
    </main>
  );
}

function SummaryCard({ title, value }: { title: string; value: number }) {
  return (
    <Card className="rounded-lg">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription className="text-2xl font-semibold text-foreground">
          {value}
        </CardDescription>
      </CardHeader>
    </Card>
  );
}

function TaskBrief({ task }: { task: TaskRecord }) {
  return (
    <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm">
      <div>
        <span className="font-medium">话题：</span>
        <span>{task.topic}</span>
      </div>
      <div>
        <span className="font-medium">竞品：</span>
        <span className="text-muted-foreground">
          {task.competitor_keyword || "空"}
        </span>
      </div>
      {task.competitor_blog && (
        <div className="flex items-center gap-1 break-all text-muted-foreground">
          <ExternalLink className="size-3.5" />
          {task.competitor_blog}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <Input value={value} onChange={(event) => onChange(event.target.value)} />
    </div>
  );
}

function EditorPanel({
  value,
  onChange,
  placeholder,
  height,
  actions,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  height: string;
  actions: ReactNode;
}) {
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">{actions}</div>
      <Textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        wrap="soft"
        className={cn(
          "resize-none overflow-y-auto break-words font-mono text-sm leading-6",
          height,
        )}
      />
    </div>
  );
}

function FileRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 rounded-lg border p-3">
      <span className="font-medium">{label}</span>
      <span className="break-all text-muted-foreground">{value}</span>
    </div>
  );
}
