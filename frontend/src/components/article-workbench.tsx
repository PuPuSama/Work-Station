"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  ArrowLeft,
  CheckCircle2,
  Clipboard,
  Download,
  ExternalLink,
  FileText,
  FolderOpen,
  ImageIcon,
  Link2,
  Loader2,
  Package,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Sparkles,
  Upload,
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
import { apiGet, apiPost, apiPut, apiUpload } from "@/lib/api";
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
  outline_confirmed: "大纲已确认",
  draft_ready: "待 ZeroGPT 初检",
  initial_ai_checked: "初检已完成",
  humanized_ready: "待 ZeroGPT 复检",
  final_ai_checked: "待恢复链接",
  links_verified: "待准备图片",
  images_ready: "可导出 Word",
  docx_exported: "已导出 Word",
};

const STATUS_FILTERS: Array<"all" | WorkflowStatus> = [
  "all",
  "new",
  "titles_ready",
  "title_selected",
  "outline_ready",
  "outline_confirmed",
  "draft_ready",
  "initial_ai_checked",
  "humanized_ready",
  "final_ai_checked",
  "links_verified",
  "images_ready",
  "docx_exported",
];

const PRODUCT_ASSET_TIMEOUT_MS = 15 * 60 * 1000;

function emptyProduct(): Product {
  return { name: "", url: "", image_path: "", description: "" };
}

type EditableProductField = "name" | "url" | "image_path" | "description";

function statusLabel(status: string) {
  return STATUS_LABELS[status as WorkflowStatus] ?? status;
}

function statusVariant(status: WorkflowStatus) {
  if (status === "docx_exported") return "default";
  if (status === "images_ready" || status === "links_verified") return "default";
  if (
    status === "draft_ready" ||
    status === "outline_ready" ||
    status === "outline_confirmed" ||
    status === "humanized_ready" ||
    status === "final_ai_checked"
  ) {
    return "secondary";
  }
  if (status === "titles_ready" || status === "title_selected") return "outline";
  return "ghost";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown error";
}

function englishWordCount(value: string) {
  const visible = value
    .replace(/^\s*img\.[^\r\n]+\.webp\s*$/gim, "")
    .replace(/!\[[^\]]*\]\([^)]+\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  return visible.match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*/g)?.length ?? 0;
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
  const [humanizedText, setHumanizedText] = useState("");
  const [initialAiScore, setInitialAiScore] = useState("");
  const [initialAiReport, setInitialAiReport] = useState("");
  const [finalAiScore, setFinalAiScore] = useState("");
  const [finalAiReport, setFinalAiReport] = useState("");
  const [heroImage, setHeroImage] = useState("");
  const [heroUpload, setHeroUpload] = useState<File | null>(null);
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
    setArticleText(
      selectedTask?.initial_article ||
        selectedTask?.raw_draft_article ||
        selectedTask?.article ||
        "",
    );
    setHumanizedText(
      selectedTask?.linked_article ||
        selectedTask?.humanized_article ||
        selectedTask?.final_article ||
        "",
    );
    setInitialAiScore(
      selectedTask?.initial_ai_check?.score == null
        ? ""
        : String(selectedTask.initial_ai_check.score),
    );
    setInitialAiReport(
      selectedTask?.initial_ai_check?.report || selectedTask?.zero_gpt_report || "",
    );
    setFinalAiScore(
      selectedTask?.final_ai_check?.score == null
        ? ""
        : String(selectedTask.final_ai_check.score),
    );
    setFinalAiReport(selectedTask?.final_ai_check?.report || "");
    setHeroImage(selectedTask?.hero_image || "");
    setHeroUpload(null);
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
      await loadData(selectedTask?.id).catch(() => undefined);
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

  async function copyArticle(value: string, label: string) {
    if (!value.trim()) {
      setError("没有可复制的正文");
      return;
    }
    await navigator.clipboard.writeText(value);
    setError("");
    setMessage(label);
  }

  function optionalScore(value: string) {
    if (!value.trim()) return null;
    const score = Number(value);
    return Number.isFinite(score) ? score : null;
  }

  function uploadAiScreenshot(stage: "initial" | "final", file: File) {
    if (!selectedId) return;
    const body = new FormData();
    body.append("file", file);
    void runAction(
      stage === "initial" ? "初检 AI 率截图已保存" : "复检 AI 率截图已保存",
      () =>
        apiUpload<TaskRecord>(
          `/api/tasks/${selectedId}/checks/${stage}-ai/screenshot`,
          body,
        ),
    );
  }

  function persistedProducts() {
    return products.filter((product) =>
      [product.name, product.url, product.image_path, product.description].some(
        (value) => value.trim(),
      ),
    );
  }

  function updateProduct(index: number, key: EditableProductField, value: string) {
    setProducts((current) =>
      current.map((product, productIndex) =>
        productIndex === index
          ? key === "url" && product.url.trim() !== value.trim()
            ? {
                ...product,
                url: value,
                product_id: "",
                canonical_url: "",
                image_path: "",
                reference_summary: "",
                reference_facts: [],
                specifications: {},
                reference_path: "",
                asset_manifest_path: "",
                asset_count: 0,
                selected_asset_id: "",
                selection_confidence: null,
                selection_reason: "",
                discovery_source: "",
                detail_page_verified: false,
                asset_status: "",
                asset_error: "",
              }
            : { ...product, [key]: value }
          : product,
      ),
    );
  }

  const selectedId = selectedTask?.id;
  const isBusy = Boolean(busy);
  const canAction = (action: string) =>
    selectedTask?.allowed_actions == null ||
    selectedTask.allowed_actions.includes(action);
  const articleTarget = config?.article.default_word_count ?? 1200;
  const articleCharacterTarget = Math.round((articleTarget * 6.67) / 100) * 100;
  const articleWords = englishWordCount(articleText);
  const humanizedWords = englishWordCount(humanizedText);
  const humanizedEditRollsBack = selectedTask
    ? ["final_ai_checked", "links_verified", "images_ready", "docx_exported"].includes(
        selectedTask.status,
      )
    : false;

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="border-b bg-[color-mix(in_oklch,var(--background),var(--accent)_22%)]">
        <div className="mx-auto flex max-w-[1500px] flex-col gap-4 px-5 py-5 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {projectName && (
                <Button
                  variant="outline"
                  size="icon-sm"
                  nativeButton={false}
                  render={<Link href="/" />}
                >
                  <ArrowLeft />
                </Button>
              )}
              <h1 className="text-xl font-semibold tracking-normal">
                {projectName || "Article Workflow Agent"}
              </h1>
              <Badge variant={dashboard?.llm_ready ? "default" : "outline"}>
                {dashboard?.llm_ready ? "LLM Ready" : "Mock LLM"}
              </Badge>
              <Badge variant={config?.integrations?.tavily_ready ? "default" : "outline"}>
                {config?.integrations?.tavily_ready
                  ? "官网搜索已连接"
                  : "官网搜索未配置"}
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
                <CardAction className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      selectedId &&
                      runAction("当前项目目录已打开", () =>
                        apiPost<ApiMessage>(`/api/tasks/${selectedId}/open-folder`),
                      )
                    }
                    disabled={isBusy}
                  >
                    <FolderOpen />
                    打开项目目录
                  </Button>
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
                  <TabsList className="grid h-auto w-full grid-cols-4 gap-1 sm:grid-cols-7">
                    <TabsTrigger value="titles">标题</TabsTrigger>
                    <TabsTrigger value="products">产品</TabsTrigger>
                    <TabsTrigger value="outline">大纲</TabsTrigger>
                    <TabsTrigger value="article">第一版</TabsTrigger>
                    <TabsTrigger value="review">人工处理</TabsTrigger>
                    <TabsTrigger value="media">图片导出</TabsTrigger>
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
                          disabled={isBusy || !canAction("generate_titles")}
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
                          disabled={isBusy || !titleChoice || !canAction("select_title")}
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
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="outline"
                          onClick={() => setProducts((current) => [...current, emptyProduct()])}
                          disabled={isBusy}
                        >
                          <Plus />
                          添加产品
                        </Button>
                        <Button
                          variant="secondary"
                          onClick={() =>
                            selectedId &&
                            runAction("官网产品与资产已自动抓取", () =>
                              apiPost<TaskRecord>(
                                `/api/tasks/${selectedId}/products/auto?limit=3`,
                                undefined,
                                PRODUCT_ASSET_TIMEOUT_MS,
                              ),
                            )
                          }
                          disabled={isBusy || !selectedId || !canAction("update_products")}
                        >
                          {isBusy ? <Loader2 className="animate-spin" /> : <WandSparkles />}
                          自动抓取产品与官网资产
                        </Button>
                        <Button
                          variant="outline"
                          onClick={() =>
                            selectedId &&
                            runAction("官网资产已重新抓取并选图", async () => {
                              await apiPut<TaskRecord>(
                                `/api/tasks/${selectedId}/products`,
                                { products: persistedProducts() },
                              );
                              return apiPost<TaskRecord>(
                                `/api/tasks/${selectedId}/products/assets`,
                                undefined,
                                PRODUCT_ASSET_TIMEOUT_MS,
                              );
                            })
                          }
                          disabled={
                            isBusy ||
                            !selectedId ||
                            !persistedProducts().some((product) => product.url.trim()) ||
                            !canAction("update_products")
                          }
                        >
                          {isBusy ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                          重新核验官网资产并 AI 选图
                        </Button>
                        <Button
                          className="ml-auto"
                          onClick={() =>
                            selectedId &&
                            runAction("产品已保存", () =>
                              apiPut<TaskRecord>(`/api/tasks/${selectedId}/products`, {
                                products: persistedProducts(),
                              }),
                            )
                          }
                          disabled={isBusy || !canAction("update_products")}
                        >
                          <Save />
                          保存产品
                        </Button>
                      </div>
                      <div className="text-xs text-muted-foreground">
                        Tavily 只负责发现官网详情页；系统会从每个详情页归档对应产品资产，再由视觉模型按资产编号选图。每篇最多使用 3 张不同图片，证据不足的产品会跳过，不要求操作人员猜图。
                      </div>
                      <ScrollArea className="h-[520px] pr-3">
                        <div className="grid gap-3">
                          {products.map((product, index) => (
                            <div key={index} className="rounded-lg border p-3">
                              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                                <div className="flex flex-wrap items-center gap-2">
                                  <div className="font-medium">产品 {index + 1}</div>
                                  {product.detail_page_verified && (
                                    <Badge variant="outline">官网详情页已核验</Badge>
                                  )}
                                  {product.discovery_source === "tavily" && (
                                    <Badge variant="outline">Tavily 发现</Badge>
                                  )}
                                  {(product.asset_count ?? 0) > 0 && (
                                    <Badge variant="secondary">
                                      官网资产 {product.asset_count}
                                    </Badge>
                                  )}
                                  {product.selected_asset_id && (
                                    <Badge>
                                      已选图
                                      {product.selection_confidence != null
                                        ? ` ${Math.round(product.selection_confidence * 100)}%`
                                        : ""}
                                    </Badge>
                                  )}
                                  {product.url && (
                                    <a
                                      href={product.canonical_url || product.url}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                                    >
                                      <ExternalLink className="size-3.5" />
                                      打开官网详情页
                                    </a>
                                  )}
                                </div>
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
                                {(product.reference_summary || product.selection_reason) && (
                                  <div className="grid gap-2 rounded-md bg-muted/45 p-3 text-xs md:col-span-2">
                                    {product.reference_summary && (
                                      <div>
                                        <span className="font-medium">官网资料摘要：</span>
                                        <span className="text-muted-foreground">
                                          {product.reference_summary}
                                        </span>
                                      </div>
                                    )}
                                    {product.selection_reason && (
                                      <div>
                                        <span className="font-medium">选图依据：</span>
                                        <span className="text-muted-foreground">
                                          {product.selection_reason}
                                        </span>
                                      </div>
                                    )}
                                  </div>
                                )}
                                {product.asset_error && (
                                  <div className="text-xs text-destructive md:col-span-2">
                                    官网资产未采用：{product.asset_error}
                                  </div>
                                )}
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
                            disabled={isBusy || !canAction("generate_outline")}
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
                            disabled={isBusy || !outlineText.trim() || !canAction("update_outline")}
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
                      meta={
                        <div className="text-xs text-muted-foreground">
                          生成范围：1000–{articleTarget} 词（约 {articleCharacterTarget.toLocaleString()}
                          字符，含空格）/ 当前：{articleWords} 词；不机械截断或自动压缩。FAQ
                          固定为最后一个 H2，3 个 Q 均需整行加粗
                        </div>
                      }
                      actions={
                        <>
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("正文已生成", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/article`, {
                                  word_count: config?.article.default_word_count ?? 1200,
                                }),
                              )
                            }
                            disabled={isBusy || !canAction("generate_article")}
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
                            disabled={isBusy || !articleText.trim() || !canAction("update_article")}
                          >
                            <Save />
                            保存第一版
                          </Button>
                        </>
                      }
                    />
                  </TabsContent>

                  <TabsContent value="review" className="min-w-0 pt-4">
                    <ScrollArea className="h-[570px] pr-3">
                      <div className="grid gap-4">
                        <WorkflowStep
                          number="1"
                          title="ZeroGPT 初检（人工）"
                          description="复制第一版正文到 ZeroGPT，检测后把分数或备注保存回来。系统不会自动访问 ZeroGPT。"
                          done={Boolean(selectedTask.initial_ai_check?.confirmed)}
                        >
                          <div className="flex flex-wrap gap-2">
                            <Button
                              variant="outline"
                              onClick={() =>
                                copyArticle(
                                  selectedTask.initial_article || articleText,
                                  "第一版正文已复制",
                                ).catch((err) => setError(errorMessage(err)))
                              }
                            >
                              <Clipboard />
                              复制第一版正文
                            </Button>
                          </div>
                          {selectedTask.initial_article_issues?.length ? (
                            <Alert className="border-amber-500/40 bg-amber-50">
                              <AlertTitle>第一版尚未满足检测条件</AlertTitle>
                              <AlertDescription>
                                {selectedTask.initial_article_issues.join(" ")}
                              </AlertDescription>
                            </Alert>
                          ) : null}
                          <div className="grid gap-3 md:grid-cols-[150px_1fr]">
                            <Field
                              label="AI 率（可选）"
                              value={initialAiScore}
                              onChange={setInitialAiScore}
                              inputType="number"
                            />
                            <div className="grid gap-2">
                              <Label>初检报告或备注</Label>
                              <Textarea
                                value={initialAiReport}
                                onChange={(event) => setInitialAiReport(event.target.value)}
                                placeholder="粘贴 ZeroGPT 结果，或记录已在公司工作台完成初检"
                                className="h-[92px] resize-none"
                              />
                            </div>
                          </div>
                          <AiScreenshotInput
                            label="初检 AI 率截图"
                            path={selectedTask.initial_ai_check?.screenshot_path || ""}
                            disabled={isBusy}
                            onImage={(file) => uploadAiScreenshot("initial", file)}
                          />
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("ZeroGPT 初检已确认", () =>
                                apiPut<TaskRecord>(
                                  `/api/tasks/${selectedId}/checks/initial-ai`,
                                  {
                                    score: optionalScore(initialAiScore),
                                    report: initialAiReport,
                                  },
                                ),
                              )
                            }
                            disabled={
                              isBusy ||
                              !canAction("confirm_initial_ai_check") ||
                              selectedTask.initial_article_ready === false ||
                              (!initialAiScore &&
                                !initialAiReport.trim() &&
                                !selectedTask.initial_ai_check?.screenshot_path)
                            }
                          >
                            <ShieldCheck />
                            确认初检完成
                          </Button>
                        </WorkflowStep>

                        <WorkflowStep
                          number="2"
                          title="生成或粘贴降 AI 稿"
                          description={`可在完成初检后调用本地提示词，也可以直接粘贴外部已经降 AI 的正文并保存。提示词：${config?.prompts?.humanize ?? "D:\\article\\降ai提示词-未测试效果版.txt"}`}
                          done={Boolean(selectedTask.humanized_article)}
                        >
                          <div className="text-xs text-muted-foreground">
                            当前候选稿：{humanizedWords} 词；不设最大词数，不会自动压缩。
                          </div>
                          {humanizedEditRollsBack && (
                            <Alert className="border-amber-500/40 bg-amber-50">
                              <AlertTitle>保存修改会回退后续步骤</AlertTitle>
                              <AlertDescription>
                                将回退到“待 ZeroGPT 复检”，并清除旧正文对应的复检确认、链接恢复、图片准备、Word、TDK 和交付包记录。
                              </AlertDescription>
                            </Alert>
                          )}
                          <Textarea
                            value={humanizedText}
                            onChange={(event) => setHumanizedText(event.target.value)}
                            placeholder="可直接粘贴外部已经降 AI 的完整正文，或完成初检后点击下方模型改写"
                            className="h-[220px] resize-none overflow-y-auto font-mono text-sm leading-6"
                          />
                          <div className="grid gap-2 md:grid-cols-2">
                            <Button
                              onClick={() =>
                                selectedId &&
                                runAction("降 AI 候选稿已生成", () =>
                                  apiPost<TaskRecord>(`/api/tasks/${selectedId}/humanize`),
                                )
                              }
                              disabled={isBusy || !canAction("humanize_article")}
                            >
                              <WandSparkles />
                              执行内置 AI 改写
                            </Button>
                            <Button
                              variant="outline"
                              onClick={() =>
                                selectedId &&
                                runAction(
                                  humanizedEditRollsBack
                                    ? "正文修改已保存，后续步骤已回退"
                                    : "外部降 AI 稿已保存",
                                  () =>
                                    apiPut<TaskRecord>(
                                      `/api/tasks/${selectedId}/humanized-article`,
                                      { article: humanizedText },
                                    ),
                                )
                              }
                              disabled={
                                isBusy ||
                                !humanizedText.trim() ||
                                !canAction("update_humanized_article")
                              }
                            >
                              <Save />
                              {humanizedEditRollsBack
                                ? "保存修改并回退后续步骤"
                                : "保存粘贴的降 AI 稿"}
                            </Button>
                          </div>
                        </WorkflowStep>

                        <WorkflowStep
                          number="3"
                          title="ZeroGPT 复检（人工）"
                          description={`复制降 AI 版本再次检测。默认参考线 < ${config?.article.ai_pass_threshold ?? 30}%，但最终由你确认。`}
                          done={Boolean(selectedTask.final_ai_check?.confirmed)}
                        >
                          <Button
                            variant="outline"
                            onClick={() =>
                              copyArticle(
                                selectedTask.humanized_article || humanizedText,
                                "降 AI 正文已复制",
                              ).catch((err) => setError(errorMessage(err)))
                            }
                            disabled={!humanizedText.trim()}
                          >
                            <Clipboard />
                            复制降 AI 正文
                          </Button>
                          <div className="grid gap-3 md:grid-cols-[150px_1fr]">
                            <Field
                              label="复检 AI 率（可选）"
                              value={finalAiScore}
                              onChange={setFinalAiScore}
                              inputType="number"
                            />
                            <div className="grid gap-2">
                              <Label>复检报告或备注</Label>
                              <Textarea
                                value={finalAiReport}
                                onChange={(event) => setFinalAiReport(event.target.value)}
                                placeholder="粘贴第二次 ZeroGPT 结果"
                                className="h-[92px] resize-none"
                              />
                            </div>
                          </div>
                          <AiScreenshotInput
                            label="复检 AI 率截图"
                            path={selectedTask.final_ai_check?.screenshot_path || ""}
                            disabled={isBusy}
                            onImage={(file) => uploadAiScreenshot("final", file)}
                          />
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("ZeroGPT 复检已确认", () =>
                                apiPut<TaskRecord>(
                                  `/api/tasks/${selectedId}/checks/final-ai`,
                                  {
                                    score: optionalScore(finalAiScore),
                                    report: finalAiReport,
                                  },
                                ),
                              )
                            }
                            disabled={
                              isBusy ||
                              !canAction("confirm_final_ai_check") ||
                              !humanizedText.trim() ||
                              (!finalAiScore &&
                                !finalAiReport.trim() &&
                                !selectedTask.final_ai_check?.screenshot_path)
                            }
                          >
                            <ShieldCheck />
                            确认复检完成
                          </Button>
                        </WorkflowStep>

                        <WorkflowStep
                          number="4"
                          title="恢复并校验超链接"
                          description="以第一版链接清单为基准；URL 缺失或锚文本被降 AI 改名时，由模型恢复第一版的原始链接名称和 URL。"
                          done={selectedTask.status === "links_verified" || selectedTask.status === "images_ready" || selectedTask.status === "docx_exported"}
                        >
                          <div className="flex flex-wrap items-center gap-2 text-sm">
                            <Badge variant="outline">
                              第一版链接 {selectedTask.source_links?.length ?? 0}
                            </Badge>
                            {selectedTask.workflow_error && (
                              <span className="text-destructive">
                                {selectedTask.workflow_error.message}
                              </span>
                            )}
                          </div>
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("超链接已校验并恢复", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/restore-links`),
                              )
                            }
                            disabled={isBusy || !canAction("restore_links")}
                          >
                            <Link2 />
                            校验并回填链接
                          </Button>
                        </WorkflowStep>
                      </div>
                    </ScrollArea>
                  </TabsContent>

                  <TabsContent value="media" className="min-w-0 pt-4">
                    <ScrollArea className="h-[570px] pr-3">
                      <div className="grid gap-4">
                        <WorkflowStep
                          number="5"
                          title="选择并准备首图"
                          description="每篇文章最多 3 张不同图片（包含首图）。首图会转换为 WebP，以安全化文章标题命名，并固定放在第一个 H2 前；重复产品图会自动跳过。"
                          done={selectedTask.status === "images_ready" || selectedTask.status === "docx_exported"}
                        >
                          <Field label="首图路径" value={heroImage} onChange={setHeroImage} />
                          <div className="flex flex-wrap gap-2">
                            {products
                              .filter((product) => product.image_path)
                              .map((product, index) => (
                                <Button
                                  key={`${product.image_path}-${index}`}
                                  size="sm"
                                  variant="outline"
                                  onClick={() => setHeroImage(product.image_path)}
                                >
                                  <ImageIcon />
                                  使用{product.name || `产品 ${index + 1}`}图片
                                </Button>
                              ))}
                          </div>
                          <div className="grid gap-2">
                            <Label htmlFor="hero-upload">或上传首图</Label>
                            <Input
                              id="hero-upload"
                              type="file"
                              accept="image/*"
                              onChange={(event) => setHeroUpload(event.target.files?.[0] ?? null)}
                            />
                          </div>
                          <div className="flex flex-wrap gap-2">
                            <Button
                              variant="outline"
                              onClick={() =>
                                selectedId &&
                                runAction("首图设置已保存", () =>
                                  apiPut<TaskRecord>(`/api/tasks/${selectedId}/images`, {
                                    hero_image: heroImage,
                                  }),
                                )
                              }
                              disabled={
                                isBusy ||
                                !heroImage.trim() ||
                                !canAction("update_images")
                              }
                            >
                              <Save />
                              保存首图路径
                            </Button>
                            <Button
                              variant="outline"
                              onClick={() => {
                                if (!selectedId || !heroUpload) return;
                                const body = new FormData();
                                body.append("file", heroUpload);
                                void runAction("首图已上传", () =>
                                  apiUpload<TaskRecord>(
                                    `/api/tasks/${selectedId}/images/upload?role=hero`,
                                    body,
                                  ),
                                );
                              }}
                              disabled={isBusy || !heroUpload || !canAction("update_images")}
                            >
                              <Upload />
                              上传并设为首图
                            </Button>
                          </div>
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("图片已转换并排版", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/prepare-images`),
                              )
                            }
                            disabled={
                              isBusy ||
                              !heroImage.trim() ||
                              !canAction("prepare_images")
                            }
                          >
                            <ImageIcon />
                            转换 WebP 并准备图片
                          </Button>
                          <div className="grid gap-2">
                            {(selectedTask.images ?? []).map((item) => (
                              <div key={item.id} className="rounded-lg border p-3 text-sm">
                                <div className="flex items-center justify-between gap-2">
                                  <span className="font-medium">
                                    {item.role === "hero" ? "首图" : "正文图"}
                                  </span>
                                  <Badge variant="outline">{item.marker}</Badge>
                                </div>
                                <div className="mt-1 break-all text-xs text-muted-foreground">
                                  {item.prepared_path}
                                </div>
                                {item.status === "needs_anchor" &&
                                item.anchor_candidates?.length ? (
                                  <div className="mt-3 grid gap-2">
                                    <div className="text-xs text-amber-700">
                                      请选择标题；图片会放在该标题下第一段完整正文的末尾：
                                    </div>
                                    <div className="flex flex-wrap gap-2">
                                      {item.anchor_candidates.map((candidate) => (
                                        <Button
                                          key={candidate.id}
                                          size="sm"
                                          variant="outline"
                                          onClick={() => {
                                            if (!selectedId) return;
                                            const nextImages = (selectedTask.images ?? []).map(
                                              (image) =>
                                                image.id === item.id
                                                  ? {
                                                      ...image,
                                                      anchor_heading:
                                                        candidate.anchor_heading,
                                                      status: "pending",
                                                      error: "",
                                                    }
                                                  : image,
                                            );
                                            void runAction("图片锚点已保存", () =>
                                              apiPut<TaskRecord>(
                                                `/api/tasks/${selectedId}/images`,
                                                {
                                                  hero_image: heroImage,
                                                  images: nextImages,
                                                },
                                              ),
                                            );
                                          }}
                                        >
                                          H{candidate.level} {candidate.heading}
                                        </Button>
                                      ))}
                                    </div>
                                  </div>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </WorkflowStep>

                        <WorkflowStep
                          number="6"
                          title="导出最终 Word"
                          description="仅导出已完成两次人工检测、链接校验和图片准备的版本。"
                          done={selectedTask.status === "docx_exported"}
                        >
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("Word 已导出", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/export-docx`),
                              )
                            }
                            disabled={isBusy || !canAction("export_docx")}
                          >
                            <Download />
                            导出 Word
                          </Button>
                          {selectedTask.docx_path && (
                            <div className="break-all text-sm text-muted-foreground">
                              {selectedTask.docx_path}
                            </div>
                          )}
                        </WorkflowStep>

                        <WorkflowStep
                          number="7"
                          title="生成英文 SEO TDK"
                          description="根据最终正文生成 T、D、K；T 与正文 H1 完全一致，D 最多 150 个字符，K 固定 6 个关键词，并保存为 D.docx。"
                          done={Boolean(selectedTask.tdk_path)}
                        >
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("TDK 已生成并保存为 D.docx", () =>
                                apiPost<TaskRecord>(`/api/tasks/${selectedId}/generate-tdk`),
                              )
                            }
                            disabled={isBusy || !canAction("generate_tdk")}
                          >
                            <Sparkles />
                            生成 TDK 文档
                          </Button>
                          {selectedTask.tdk?.title && (
                            <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm">
                              <div>
                                <span className="font-semibold">T: </span>
                                {selectedTask.tdk.title}
                              </div>
                              <div>
                                <span className="font-semibold">D: </span>
                                {selectedTask.tdk.description}
                                <span className="ml-2 text-xs text-muted-foreground">
                                  {selectedTask.tdk.description_character_count}/150
                                </span>
                              </div>
                              <div>
                                <span className="font-semibold">K: </span>
                                {selectedTask.tdk.keywords.join(", ")}
                              </div>
                              <div className="break-all text-xs text-muted-foreground">
                                {selectedTask.tdk_path}
                              </div>
                            </div>
                          )}
                        </WorkflowStep>

                        <WorkflowStep
                          number="8"
                          title="交付打包"
                          description="正文 Word、D.docx、全部文章图片和最后一次 AI 检测截图直接放在成品文件夹根目录；初检截图不打包。"
                          done={Boolean(selectedTask.delivery_package_path)}
                        >
                          <Button
                            onClick={() =>
                              selectedId &&
                              runAction("交付成品已打包", () =>
                                apiPost<TaskRecord>(
                                  `/api/tasks/${selectedId}/package-delivery`,
                                ),
                              )
                            }
                            disabled={
                              isBusy ||
                              !selectedTask.docx_path ||
                              !selectedTask.tdk_path ||
                              !canAction("package_delivery")
                            }
                          >
                            <Package />
                            生成交付文件夹
                          </Button>
                          {selectedTask.delivery_package_path && (
                            <div className="break-all text-sm text-muted-foreground">
                              {selectedTask.delivery_package_path}
                            </div>
                          )}
                        </WorkflowStep>
                      </div>
                    </ScrollArea>
                  </TabsContent>

                  <TabsContent value="files" className="min-w-0 pt-4">
                    <div className="grid gap-3 text-sm">
                      <FileRow label="任务目录" value={selectedTask.task_dir} />
                      <FileRow label="Word 文件" value={selectedTask.docx_path || "未导出"} />
                      <FileRow label="TDK 文档" value={selectedTask.tdk_path || "未生成"} />
                      <FileRow
                        label="交付成品"
                        value={selectedTask.delivery_package_path || "未打包"}
                      />
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
  inputType = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  inputType?: "text" | "number";
}) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <Input
        type={inputType}
        min={inputType === "number" ? 0 : undefined}
        max={inputType === "number" ? 100 : undefined}
        step={inputType === "number" ? "0.1" : undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

function WorkflowStep({
  number,
  title,
  description,
  done,
  children,
}: {
  number: string;
  title: string;
  description: string;
  done: boolean;
  children: ReactNode;
}) {
  return (
    <div className="grid gap-3 rounded-lg border p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold">
            {number}
          </div>
          <div className="min-w-0">
            <div className="font-medium">{title}</div>
            <div className="mt-1 text-sm text-muted-foreground">{description}</div>
          </div>
        </div>
        <Badge variant={done ? "default" : "outline"}>{done ? "已完成" : "待处理"}</Badge>
      </div>
      <div className="grid gap-3 pl-0 md:pl-10">{children}</div>
    </div>
  );
}

function AiScreenshotInput({
  label,
  path,
  disabled,
  onImage,
}: {
  label: string;
  path: string;
  disabled: boolean;
  onImage: (file: File) => void;
}) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <div
        tabIndex={disabled ? -1 : 0}
        onPaste={(event) => {
          if (disabled) return;
          const item = Array.from(event.clipboardData.items).find((candidate) =>
            candidate.type.startsWith("image/"),
          );
          const blob = item?.getAsFile();
          if (!blob) return;
          event.preventDefault();
          onImage(
            new File([blob], `${label}-${Date.now()}.png`, {
              type: blob.type || "image/png",
            }),
          );
        }}
        className={cn(
          "rounded-lg border border-dashed bg-muted/20 p-3 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20",
          disabled && "cursor-not-allowed opacity-60",
        )}
      >
        <div className="flex items-center gap-2 text-muted-foreground">
          <Clipboard className="size-4" />
          点击此区域后按 Ctrl+V 粘贴截图，或在下方选择图片文件。
        </div>
      </div>
      <Input
        type="file"
        accept="image/*"
        disabled={disabled}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onImage(file);
          event.currentTarget.value = "";
        }}
      />
      {path && <div className="break-all text-xs text-muted-foreground">{path}</div>}
    </div>
  );
}

function EditorPanel({
  value,
  onChange,
  placeholder,
  height,
  meta,
  actions,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  height: string;
  meta?: ReactNode;
  actions: ReactNode;
}) {
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">{actions}</div>
      {meta}
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
