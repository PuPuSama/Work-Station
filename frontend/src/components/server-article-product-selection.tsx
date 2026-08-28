"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Loader2,
  PackageCheck,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Undo2,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import { Input } from "@/components/ui/input";
import { apiGet, apiPut } from "@/lib/api";
import { sameProjectId } from "@/lib/project-id";
import type {
  AccessibleProject,
  ServerCatalogProduct,
  ServerProjectCatalog,
  TaskRecord,
} from "@/types";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败，请重试。";
}

function taskProductIds(task: TaskRecord) {
  return task.products.flatMap((product) =>
    product.product_id ? [product.product_id] : [],
  );
}

function selectionKey(productIds: string[]) {
  return JSON.stringify([...productIds].sort());
}

function canEdit(
  role: AccessibleProject["effective_role"] | null,
  isProjectOwner: boolean,
) {
  return (
    role === "org_admin" ||
    role === "editor" ||
    (role === "team_lead" && isProjectOwner)
  );
}

export function ServerArticleProductSelection({
  customer,
  taskId,
}: {
  customer: string;
  taskId: string;
}) {
  const router = useRouter();
  const [task, setTask] = useState<TaskRecord | null>(null);
  const [catalog, setCatalog] = useState<ServerProjectCatalog | null>(null);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [isProjectOwner, setIsProjectOwner] = useState(false);
  const [selectedProductIds, setSelectedProductIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const requestIdRef = useRef(0);

  const encodedCustomer = encodeURIComponent(customer);
  const encodedTaskId = encodeURIComponent(taskId);
  const projectApi = `/api/projects/${encodedCustomer}`;
  const taskApi = `${projectApi}/tasks/${encodedTaskId}`;
  const workbenchHref = `/projects/${encodedCustomer}/articles/${encodedTaskId}?step=setup`;
  const outlineHref = `/projects/${encodedCustomer}/articles/${encodedTaskId}?step=outline`;

  const load = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setError("");
    try {
      const [nextTask, projects, nextCatalog] = await Promise.all([
        apiGet<TaskRecord>(taskApi),
        apiGet<AccessibleProject[]>("/api/projects"),
        apiGet<ServerProjectCatalog>(
          `${projectApi}/catalog?product_limit=200&image_limit=1`,
        ),
      ]);
      if (requestIdRef.current !== requestId) return;
      const project = projects.find((item) =>
        sameProjectId(item.project_id, customer),
      );
      const available = new Set(
        nextCatalog.products.map((product) => product.product_id),
      );
      const recommended = (nextTask.product_candidate_ids || []).filter(
        (productId) => available.has(productId),
      );
      const confirmed = taskProductIds(nextTask).filter((productId) =>
        available.has(productId),
      );
      setTask(nextTask);
      setCatalog(nextCatalog);
      setRole(project?.effective_role ?? null);
      setIsProjectOwner(project?.is_project_owner === true);
      setSelectedProductIds(recommended.length ? recommended : confirmed);
    } catch (reason) {
      if (requestIdRef.current === requestId) {
        setError(errorMessage(reason));
      }
    } finally {
      if (requestIdRef.current === requestId) setLoading(false);
    }
  }, [customer, projectApi, taskApi]);

  useEffect(() => {
    void load();
    return () => {
      requestIdRef.current += 1;
    };
  }, [load]);

  const products = useMemo(() => catalog?.products || [], [catalog]);
  const recommendedProductIds = useMemo(
    () => new Set(task?.product_candidate_ids || []),
    [task?.product_candidate_ids],
  );
  const recommendationReasons = task?.product_candidate_reasons || {};
  const recommendationDetails = useMemo(
    () =>
      Object.fromEntries(
        (task?.product_candidate_details || []).map((detail) => [
          detail.product_id,
          detail,
        ]),
      ),
    [task?.product_candidate_details],
  );
  const savedReasons = useMemo(
    () =>
      Object.fromEntries(
        (task?.products || []).flatMap((product) => {
          if (
            !product.product_id ||
            !product.selection_reason ||
            product.selection_reason.startsWith("Operator selected")
          ) {
            return [];
          }
          return [[product.product_id, product.selection_reason] as const];
        }),
      ),
    [task?.products],
  );
  const filteredProducts = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const visible = normalizedQuery
      ? products.filter((product) =>
          `${product.name}\n${product.product_id}`
            .toLocaleLowerCase()
            .includes(normalizedQuery),
        )
      : products;
    return [...visible].sort((left, right) => {
      const recommendationRank =
        Number(recommendedProductIds.has(right.product_id)) -
        Number(recommendedProductIds.has(left.product_id));
      return recommendationRank || left.name.localeCompare(right.name);
    });
  }, [products, query, recommendedProductIds]);

  const confirmedProductIds = task ? taskProductIds(task) : [];
  const selectionDirty =
    task !== null &&
    selectionKey(selectedProductIds) !== selectionKey(confirmedProductIds);
  const hasPendingRecommendation = recommendedProductIds.size > 0;
  const editAllowed = canEdit(role, isProjectOwner);
  const allowed = new Set(task?.allowed_actions || []);

  function toggleProduct(product: ServerCatalogProduct) {
    setSelectedProductIds((current) => {
      const checked = current.includes(product.product_id);
      return checked
        ? current.filter((value) => value !== product.product_id)
        : [...current, product.product_id];
    });
    setMessage("");
  }

  async function saveSelection() {
    if (!task) return;
    setPending(true);
    setError("");
    setMessage("");
    try {
      const updated = await apiPut<TaskRecord>(`${taskApi}/products`, {
        revision: task.revision ?? 0,
        product_ids: selectedProductIds,
      });
      setTask(updated);
      setSelectedProductIds(taskProductIds(updated));
      setMessage(`已确认 ${updated.products.length} 个产品，正在打开大纲页面。`);
      router.push(outlineHref);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPending(false);
    }
  }

  if (loading && !task) {
    return (
      <main className="mx-auto max-w-5xl px-5 py-10">
        <div className="flex min-h-56 items-center justify-center rounded-xl border bg-card text-sm text-muted-foreground">
          <Loader2 className="mr-2 animate-spin" />
          正在读取产品目录…
        </div>
      </main>
    );
  }

  if (!task) {
    return (
      <main className="mx-auto max-w-5xl px-5 py-10">
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>无法打开产品选择页</AlertTitle>
          <AlertDescription>{error || "Task 不存在或无权访问。"}</AlertDescription>
        </Alert>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 px-5 py-5 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <Button
              nativeButton={false}
              variant="ghost"
              size="sm"
              className="-ml-2 mb-2"
              render={<Link href={workbenchHref} />}
            >
              <ArrowLeft />
              返回文章工作台
            </Button>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">选择本篇产品</h1>
              <Badge variant="outline">最多 3 个</Badge>
            </div>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
              {task.selected_title || task.topic}
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            disabled={loading || pending}
            onClick={() => void load()}
          >
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            刷新
          </Button>
        </div>
      </header>

      <div className="mx-auto grid max-w-6xl gap-4 px-5 py-5">
        {error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>操作失败</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {message && (
          <Alert>
            <CheckCircle2 />
            <AlertTitle>产品选择已保存</AlertTitle>
            <AlertDescription>{message}</AlertDescription>
          </Alert>
        )}
        {!editAllowed && (
          <Alert>
            <AlertCircle />
            <AlertTitle>当前账号没有文章编辑权限</AlertTitle>
            <AlertDescription>
              可以查看产品与推荐理由，但不能修改或保存选择。
            </AlertDescription>
          </Alert>
        )}
        {hasPendingRecommendation && (
          <Alert>
            <Sparkles />
            <AlertTitle>
              已自动勾选 {recommendedProductIds.size} 个推荐产品
            </AlertTitle>
            <AlertDescription>
              请结合下方推荐理由检查；可以取消勾选或换成其他产品，确认后再保存。
            </AlertDescription>
          </Alert>
        )}

        <div className="sticky top-4 z-10 flex flex-col gap-3 rounded-xl border bg-card/95 p-4 shadow-lg backdrop-blur sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">
            {selectionDirty || hasPendingRecommendation
              ? "当前勾选尚未确认保存。"
              : "当前勾选与服务器已保存选择一致。"}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              disabled={
                pending ||
                !editAllowed ||
                !allowed.has("update_products") ||
                selectedProductIds.length < 1 ||
                (!selectionDirty && !hasPendingRecommendation)
              }
              onClick={() => void saveSelection()}
            >
              {pending ? <Loader2 className="animate-spin" /> : <Save />}
              确认并保存 {selectedProductIds.length} 个产品
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={pending || !selectionDirty}
              onClick={() => setSelectedProductIds(confirmedProductIds)}
            >
              <Undo2 />
              恢复已保存选择
            </Button>
            {!selectionDirty &&
              !hasPendingRecommendation &&
              selectedProductIds.length > 0 && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={pending}
                  onClick={() => router.push(outlineHref)}
                >
                  进入大纲
                </Button>
              )}
          </div>
        </div>

        <Card>
          <CardHeader className="border-b">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <CardTitle>Server 已确认产品</CardTitle>
                <CardDescription className="mt-1">
                  当前已选 {selectedProductIds.length}/3，共 {products.length} 个可选产品。
                </CardDescription>
              </div>
              <div className="relative w-full sm:max-w-sm">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="pl-9"
                  placeholder="搜索产品名称或 Product ID"
                  aria-label="搜索产品"
                />
              </div>
            </div>
          </CardHeader>
          <CardContent className="grid gap-3">
            {filteredProducts.length ? (
              filteredProducts.map((product) => {
                const checked = selectedProductIds.includes(product.product_id);
                const recommended = recommendedProductIds.has(product.product_id);
                const reason =
                  recommendationReasons[product.product_id] ||
                  savedReasons[product.product_id] ||
                  "";
                const detail = recommendationDetails[product.product_id];
                return (
                  <label
                    key={product.product_id}
                    className="flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-4 transition-colors hover:bg-accent/40"
                  >
                    <input
                      type="checkbox"
                      className="mt-1 size-4"
                      checked={checked}
                      disabled={
                        pending ||
                        !editAllowed ||
                        !allowed.has("update_products") ||
                        (!checked && selectedProductIds.length >= 3)
                      }
                      onChange={() => toggleProduct(product)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-2 font-medium">
                        <PackageCheck className="size-4 text-muted-foreground" />
                        {product.name}
                        {recommended && <Badge variant="secondary">AI 推荐</Badge>}
                      </span>
                      <span className="mt-1 block break-all font-mono text-xs text-muted-foreground">
                        {product.product_id}
                      </span>
                      {reason && (
                        <span className="mt-3 block rounded-lg bg-accent/55 px-3 py-2 text-sm leading-6">
                          <span className="font-medium">推荐理由：</span>
                          {reason}
                        </span>
                      )}
                      {detail && (
                        <span className="mt-2 flex flex-wrap gap-2 text-xs text-muted-foreground">
                          <Badge variant="outline">
                            {detail.article_role || "article role"}
                          </Badge>
                          <Badge variant="outline">
                            证据：{detail.evidence_status || "unknown"}
                          </Badge>
                          {detail.suggested_section && (
                            <span className="rounded-md bg-muted px-2 py-1">
                              建议章节：{detail.suggested_section}
                            </span>
                          )}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })
            ) : (
              <p className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">
                {products.length
                  ? "没有匹配当前搜索的产品。"
                  : "当前知识库没有可供选择的已确认产品。"}
              </p>
            )}
          </CardContent>
        </Card>

      </div>
    </main>
  );
}
