"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  FileStack,
  ImageIcon,
  Inbox,
  Loader2,
  PackageCheck,
  PackageSearch,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

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
import { Label } from "@/components/ui/label";
import { ServerPrivateDocumentUpload } from "@/components/server-private-document-upload";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  AccessibleProject,
  KnowledgeLibrary,
  KnowledgeProductSummary,
  KnowledgeSourceSummary,
  KnowledgeUploadResult,
} from "@/types";

type ReviewDecision = "approve" | "needs_review" | "reject";

const sourceKindLabels: Record<string, string> = {
  private_file: "私有资料",
  product_detail: "产品详情",
  product_category: "产品分类",
  official_blog: "官网博客",
  knowledge_page: "知识页面",
};

const trustTierLabels: Record<string, string> = {
  hard_fact: "硬事实",
  reference_material: "参考资料",
  writing_instruction: "写作指令",
};

const statusLabels: Record<string, string> = {
  inbox: "待发布",
  needs_review: "需复核",
  rejected: "已拒绝",
  published: "已发布",
  candidate: "待确认",
  confirmed: "已确认",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "知识库操作失败，请重试。";
}

function canEditKnowledge(
  role: AccessibleProject["effective_role"] | null,
) {
  return role === "org_admin" || role === "team_lead" || role === "editor";
}

function formatDate(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}

function SourceReviewCard({
  editable,
  onChanged,
  pending,
  projectPath,
  publishable,
  source,
}: {
  editable: boolean;
  onChanged: (
    key: string,
    action: Promise<unknown>,
    message: string,
  ) => Promise<void>;
  pending: string;
  projectPath: string;
  publishable: boolean;
  source: KnowledgeSourceSummary;
}) {
  const [sourceKind, setSourceKind] = useState(source.source_kind);
  const [trustTier, setTrustTier] = useState(source.trust_tier);
  const [decision, setDecision] = useState<ReviewDecision>(
    source.review_decision || "approve",
  );
  const [reason, setReason] = useState("");
  const sourcePath = `${projectPath}/sources/${encodeURIComponent(source.source_id)}`;
  const locked = source.status === "published";
  const reviewing = pending === `review:${source.source_id}`;
  const publishing = pending === `publish:${source.source_id}`;

  return (
    <Card>
      <CardHeader className="gap-3 border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="break-words text-base">
              {source.display_name}
            </CardTitle>
            <CardDescription className="mt-1 break-all font-mono text-[11px]">
              {source.source_id}
            </CardDescription>
          </div>
          <Badge variant={source.status === "published" ? "default" : "secondary"}>
            {statusLabels[source.status] || source.status}
          </Badge>
        </div>
        {source.classification_reason ? (
          <p className="text-xs leading-5 text-muted-foreground">
            {source.classification_reason}
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid grid-cols-2 gap-3 text-xs text-muted-foreground sm:grid-cols-4">
          <div>
            <span className="block text-foreground">{source.snapshot_count}</span>
            Snapshot
          </div>
          <div>
            <span className="block text-foreground">{source.chunk_count}</span>
            Chunk
          </div>
          <div>
            <span className="block text-foreground">{source.asset_count}</span>
            Asset
          </div>
          <div>
            <span className="block text-foreground">
              {formatDate(source.latest_fetched_at)}
            </span>
            最近入库
          </div>
        </div>

        {!locked ? (
          <div className="grid gap-4 rounded-xl border bg-muted/20 p-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor={`kind-${source.source_id}`}>来源类型</Label>
                <select
                  id={`kind-${source.source_id}`}
                  value={sourceKind}
                  disabled={!editable || Boolean(pending)}
                  className="min-h-11 rounded-lg border bg-background px-3 text-sm"
                  onChange={(event) => setSourceKind(event.target.value)}
                >
                  {Object.entries(sourceKindLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor={`trust-${source.source_id}`}>信任层级</Label>
                <select
                  id={`trust-${source.source_id}`}
                  value={trustTier}
                  disabled={!editable || Boolean(pending)}
                  className="min-h-11 rounded-lg border bg-background px-3 text-sm"
                  onChange={(event) => setTrustTier(event.target.value)}
                >
                  {Object.entries(trustTierLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`decision-${source.source_id}`}>审阅决定</Label>
              <select
                id={`decision-${source.source_id}`}
                value={decision}
                disabled={!editable || Boolean(pending)}
                className="min-h-11 rounded-lg border bg-background px-3 text-sm"
                onChange={(event) =>
                  setDecision(event.target.value as ReviewDecision)
                }
              >
                <option value="approve">批准分类，进入待发布</option>
                <option value="needs_review">保留并标记需复核</option>
                <option value="reject">拒绝当前来源</option>
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`reason-${source.source_id}`}>审阅理由</Label>
              <Input
                id={`reason-${source.source_id}`}
                value={reason}
                disabled={!editable || Boolean(pending)}
                maxLength={500}
                placeholder="记录为什么接受或拒绝当前分类"
                onChange={(event) => setReason(event.target.value)}
              />
            </div>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={
                !editable || Boolean(pending) || !reason.trim()
              }
              onClick={() =>
                void onChanged(
                  `review:${source.source_id}`,
                  apiPut(sourcePath + "/review", {
                    source_kind: sourceKind,
                    trust_tier: trustTier,
                    decision,
                    reason: reason.trim(),
                  }),
                  `${source.display_name} 的审阅决定已保存。`,
                )
              }
            >
              {reviewing ? (
                <Loader2 className="animate-spin" />
              ) : (
                <ShieldCheck />
              )}
              保存审阅决定
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-2 rounded-xl border bg-muted/20 px-4 py-3 text-sm text-muted-foreground">
            <CheckCircle2 className="size-4" />
            已发布来源不可原地改分类；需要变更时应产生新 Snapshot 并重新审阅。
          </div>
        )}

        {source.status === "inbox" && source.review_decision === "approve" ? (
          <Button
            type="button"
            className="min-h-11"
            disabled={
              !publishable ||
              Boolean(pending) ||
              source.chunk_count === 0
            }
            onClick={() =>
              void onChanged(
                `publish:${source.source_id}`,
                apiPost(sourcePath + "/publish"),
                `${source.display_name} 已完成向量并切换为当前发布快照。`,
              )
            }
          >
            {publishing ? (
              <Loader2 className="animate-spin" />
            ) : (
              <PackageCheck />
            )}
            发布当前 Snapshot
          </Button>
        ) : source.status === "inbox" ? (
          <p className="rounded-xl border border-dashed px-4 py-3 text-xs leading-5 text-muted-foreground">
            先保存“批准分类”的审阅决定，服务端确认
            `review_decision=approve` 后才显示发布命令。
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ProductCard({
  onChanged,
  pending,
  product,
  projectPath,
  publishable,
}: {
  onChanged: (
    key: string,
    action: Promise<unknown>,
    message: string,
  ) => Promise<void>;
  pending: string;
  product: KnowledgeProductSummary;
  projectPath: string;
  publishable: boolean;
}) {
  const confirming = pending === `product:${product.product_id}`;
  return (
    <div className="grid gap-3 rounded-xl border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">{product.name}</div>
          <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {product.product_id}
          </div>
        </div>
        <Badge variant={product.status === "confirmed" ? "default" : "secondary"}>
          {statusLabels[product.status] || product.status}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        {product.category_path.length
          ? product.category_path.join(" / ")
          : "尚未确认分类路径"}
      </p>
      {product.status !== "confirmed" ? (
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={!publishable || Boolean(pending)}
          onClick={() =>
            void onChanged(
              `product:${product.product_id}`,
              apiPost(
                `${projectPath}/products/${encodeURIComponent(product.product_id)}/confirm`,
              ),
              `${product.name} 已确认；只有 Published Current Evidence 完整时才会出现在文章选择器。`,
            )
          }
        >
          {confirming ? (
            <Loader2 className="animate-spin" />
          ) : (
            <PackageSearch />
          )}
          确认产品身份
        </Button>
      ) : null}
    </div>
  );
}

export function ServerKnowledgeInbox({ customer }: { customer: string }) {
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const projectPath = `/api/knowledge/${encodeURIComponent(customer)}`;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextLibrary, projects] = await Promise.all([
        apiGet<KnowledgeLibrary>(projectPath),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      setLibrary(nextLibrary);
      setRole(
        projects.find((project) => project.project_id === customer)
          ?.effective_role ?? null,
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [customer, projectPath]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(
    key: string,
    action: Promise<unknown>,
    successMessage: string,
  ) {
    setPending(key);
    setError("");
    setMessage("");
    try {
      await action;
      setMessage(successMessage);
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPending("");
    }
  }

  async function uploaded(result: KnowledgeUploadResult) {
    setError("");
    setMessage(
      result.created
        ? `${result.message} 已生成 ${result.chunk_count} 个知识块、登记 ${result.asset_count} 个内嵌资产。`
        : `${result.message} 本次没有重复创建 Snapshot 或 Audit 记录。`,
    );
    await load();
  }

  const sortedSources = useMemo(
    () =>
      [...(library?.sources || [])].sort((left, right) => {
        const rank = (status: string) =>
          status === "inbox"
            ? 0
            : status === "needs_review"
              ? 1
              : status === "rejected"
                ? 2
                : 3;
        return (
          rank(left.status) - rank(right.status) ||
          left.display_name.localeCompare(right.display_name)
        );
      }),
    [library],
  );
  const editable = canEditKnowledge(role);
  const summaries = [
    { label: "全部来源", value: library?.source_count || 0, icon: FileStack },
    { label: "Research Inbox", value: library?.inbox_count || 0, icon: Inbox },
    {
      label: "已发布来源",
      value: library?.published_count || 0,
      icon: CheckCircle2,
    },
    {
      label: "已确认产品",
      value: library?.confirmed_product_count || 0,
      icon: PackageSearch,
    },
    { label: "去重资产", value: library?.asset_count || 0, icon: ImageIcon },
  ];

  return (
    <main className="mx-auto grid w-full max-w-[1480px] gap-5 px-5 py-6">
      <div className="flex flex-col gap-4 rounded-2xl border bg-card p-5 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
            <BookOpenText className="size-4" />
            Server Research Inbox
          </div>
          <h1 className="text-2xl font-semibold">知识来源审阅</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            私有资料上传与 Rediscovery 都只把来源、产品和图片证据放入
            Inbox。这里负责人工分类、发布 Snapshot 与确认产品身份；WordPress
            Sync 和原始对象打开仍保持 Server 关闭。Research Run 已迁移到独立的
            PostgreSQL Worker，可在“资料研究”页签中运行。
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          className="min-h-11"
          disabled={loading || Boolean(pending)}
          onClick={() => void load()}
        >
          {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          刷新 Inbox
        </Button>
      </div>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>知识库操作失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      {message ? (
        <Alert>
          <CheckCircle2 />
          <AlertTitle>服务端状态已更新</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {summaries.map((item) => (
          <Card key={item.label}>
            <CardContent className="flex items-center justify-between gap-3 py-4">
              <div>
                <div className="text-2xl font-semibold tabular-nums">
                  {loading ? "—" : item.value}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {item.label}
                </div>
              </div>
              <span className="grid size-9 place-items-center rounded-lg bg-muted text-muted-foreground">
                <item.icon className="size-4" />
              </span>
            </CardContent>
          </Card>
        ))}
      </section>

      {!editable ? (
        <Alert>
          <ShieldCheck />
          <AlertTitle>当前角色为只读审阅视图</AlertTitle>
          <AlertDescription>
            Reviewer/Viewer 可以核对来源与产品状态，但只有
            Editor、Team Lead 或 Organization Admin 可以审阅、发布或确认。
          </AlertDescription>
        </Alert>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="rounded-2xl border border-dashed bg-muted/10 p-5">
          <div className="flex items-start gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-background text-muted-foreground shadow-sm">
              <ShieldCheck className="size-5" />
            </span>
            <div>
              <h2 className="font-semibold">私有资料安全边界</h2>
              <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">
                上传先写入项目隔离、内容寻址的私有对象；数据库提交前会重新检查
                knowledge.edit，并把 Source、Snapshot、Chunk、Asset 关系和脱敏
                Audit 放进同一个事务。权限在上传期间被撤销时，不会留下可查询的半成品。
              </p>
            </div>
          </div>
        </div>
        <ServerPrivateDocumentUpload
          editable={editable}
          projectPath={projectPath}
          onUploaded={uploaded}
        />
      </section>

      <section className="grid gap-4">
        <div>
          <h2 className="text-lg font-semibold">来源队列</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            审阅决定与发布分成两个显式动作；Embedding 失败时旧快照继续服务。
          </p>
        </div>
        {loading ? (
          <div className="flex min-h-44 items-center justify-center rounded-xl border text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            正在读取 Project-scoped Knowledge Library…
          </div>
        ) : sortedSources.length ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {sortedSources.map((source) => (
              <SourceReviewCard
                key={source.source_id}
                source={source}
                projectPath={projectPath}
                editable={editable}
                publishable={editable}
                pending={pending}
                onChanged={run}
              />
            ))}
          </div>
        ) : (
          <div className="flex min-h-44 flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center">
            <Inbox className="mb-3 size-8 text-muted-foreground" />
            <div className="font-medium">当前 Project 的 Inbox 为空</div>
            <p className="mt-1 max-w-xl text-sm text-muted-foreground">
              上传私有资料，或从文章 Setup 发起 Product Rediscovery。成功上传和
              Job 都只进入 Inbox，不会自动发布知识或改变 Task 产品。
            </p>
          </div>
        )}
      </section>

      {library?.products.length ? (
        <section className="grid gap-4">
          <div>
            <h2 className="text-lg font-semibold">产品身份</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              确认只改变 Catalog 身份；文章工作台仍要求当前 Published Snapshot
              Evidence 完整，不能把候选产品事实直接写入 Task。
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {library.products.map((product) => (
              <ProductCard
                key={product.product_id}
                product={product}
                projectPath={projectPath}
                publishable={editable}
                pending={pending}
                onChanged={run}
              />
            ))}
          </div>
        </section>
      ) : null}
    </main>
  );
}
