"use client";

import {
  AlertCircle,
  ArrowLeft,
  BookOpenCheck,
  ExternalLink,
  Link2,
  Loader2,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import { apiFileUrl, apiGet, apiPut } from "@/lib/api";
import { sameProjectId } from "@/lib/project-id";
import { cn } from "@/lib/utils";
import type {
  AccessibleProject,
  KnowledgeCoverageDetail,
  KnowledgeCoverageEvidenceDetail,
  TaskRecord,
} from "@/types";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "读取知识库支撑详情失败。";
}

function canReview(project: AccessibleProject | undefined) {
  return (
    project?.effective_role === "org_admin" ||
    project?.effective_role === "editor" ||
    project?.effective_role === "reviewer" ||
    (project?.effective_role === "team_lead" &&
      Boolean(project.is_project_owner))
  );
}

function sourceKindLabel(value: string) {
  if (value === "product_detail") return "产品详情页";
  if (value === "product_category") return "产品分类页";
  if (value === "private_file") return "上传资料";
  if (value === "knowledge_page") return "知识页面";
  return value.replaceAll("_", " ");
}

function checkedAtLabel(value: string) {
  if (!value) return "尚未记录检查时间";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : `检查于 ${parsed.toLocaleString("zh-CN")}`;
}

export function ServerKnowledgeCoverageDetail({
  customer,
  taskId,
}: {
  customer: string;
  taskId: string;
}) {
  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;
  const taskApi = `${projectApi}/tasks/${encodeURIComponent(taskId)}`;
  const detailApi = `${taskApi}/checks/knowledge-coverage`;
  const backHref = `/projects/${encodeURIComponent(customer)}/articles/${encodeURIComponent(taskId)}?step=review`;
  const [detail, setDetail] = useState<KnowledgeCoverageDetail | null>(null);
  const [reviewAllowed, setReviewAllowed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    Promise.all([
      apiGet<KnowledgeCoverageDetail>(detailApi),
      apiGet<AccessibleProject[]>("/api/projects"),
    ])
      .then(([nextDetail, projects]) => {
        if (!active) return;
        setDetail(nextDetail);
        setReviewAllowed(
          canReview(
            projects.find((project) =>
              sameProjectId(project.project_id, customer),
            ),
          ),
        );
      })
      .catch((reason: unknown) => {
        if (active) setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [customer, detailApi]);

  const evidence = useMemo(() => {
    const byId = new Map<string, KnowledgeCoverageEvidenceDetail>();
    for (const paragraph of detail?.paragraphs ?? []) {
      for (const sentence of paragraph.sentences) {
        for (const reference of sentence.evidence) {
          byId.set(reference.evidence_link_id, reference);
        }
      }
    }
    return Array.from(byId.values());
  }, [detail]);

  const citationNumbers = useMemo(
    () =>
      new Map(
        evidence.map((reference, index) => [
          reference.evidence_link_id,
          index + 1,
        ]),
      ),
    [evidence],
  );

  async function recheck() {
    if (!detail) return;
    setPending(true);
    setError("");
    setMessage("");
    try {
      await apiPut<TaskRecord>(detailApi, {
        revision: detail.task_revision,
      });
      const refreshed = await apiGet<KnowledgeCoverageDetail>(detailApi);
      setDetail(refreshed);
      setMessage("已按当前正文重新计算知识库支撑率。");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPending(false);
    }
  }

  if (loading) {
    return (
      <main className="mx-auto grid w-full max-w-7xl gap-4 p-4 sm:p-6">
        <div className="h-11 w-36 animate-pulse rounded-lg bg-muted" />
        <div className="h-28 animate-pulse rounded-xl bg-muted" />
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((item) => (
            <div key={item} className="h-28 animate-pulse rounded-xl bg-muted" />
          ))}
        </div>
        <div className="h-[52dvh] animate-pulse rounded-xl bg-muted" />
      </main>
    );
  }

  return (
    <main className="mx-auto grid w-full max-w-7xl gap-4 p-4 sm:p-6">
      <a
        href="#coverage-content"
        className="sr-only rounded-md bg-background px-3 py-2 focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:ring-2 focus:ring-ring"
      >
        跳到正文证据映射
      </a>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button
          nativeButton={false}
          variant="outline"
          className="min-h-11"
          render={<Link href={backHref} />}
        >
          <ArrowLeft />
          返回文章复检
        </Button>
        {detail && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Badge variant={detail.status === "available" ? "default" : "outline"}>
              {detail.status === "available"
                ? "结果有效"
                : detail.status === "stale"
                  ? "等待重检"
                  : "未完成"}
            </Badge>
            <span>{checkedAtLabel(detail.checked_at)}</span>
          </div>
        )}
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>无法继续</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {message && (
        <Alert>
          <ShieldCheck />
          <AlertTitle>检查已更新</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      )}

      {detail && (
        <>
          <Card>
            <CardHeader className="gap-3 border-b sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <div className="mb-2 flex items-center gap-2 text-sm font-medium text-sky-700 dark:text-sky-300">
                  <BookOpenCheck className="size-4" />
                  KNOWLEDGE COVERAGE
                </div>
                <h1 className="text-xl font-medium leading-snug sm:text-2xl">
                  {detail.title}
                </h1>
                <CardDescription className="mt-2 max-w-3xl leading-6">
                  按读者可见正文句核验项目知识支撑。绿色句子已绑定证据，带编号的链接可跳到对应证据卡。
                </CardDescription>
              </div>
              <Button
                type="button"
                variant="outline"
                className="min-h-11 shrink-0"
                disabled={pending || !reviewAllowed}
                onClick={() => void recheck()}
              >
                {pending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                重新检查
              </Button>
            </CardHeader>
          </Card>

          <section className="grid grid-cols-2 gap-3 xl:grid-cols-4" aria-label="支撑率摘要">
            <Card>
              <CardContent className="grid gap-1 p-5">
                <p className="text-sm text-muted-foreground">正文支撑率</p>
                <p className="text-3xl font-semibold tabular-nums">
                  {Math.round(detail.sentence_coverage * 100)}%
                </p>
                <p className="text-sm text-muted-foreground">
                  {detail.supported_sentences}/{detail.eligible_sentences} 个合格句
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="grid gap-1 p-5">
                <p className="text-sm text-muted-foreground">硬事实证据</p>
                <p className="text-3xl font-semibold tabular-nums">
                  {detail.hard_fact_sentences
                    ? Math.round(detail.hard_fact_coverage * 100)
                    : "—"}
                  {detail.hard_fact_sentences ? "%" : ""}
                </p>
                <p className="text-sm text-muted-foreground">
                  {detail.supported_hard_fact_sentences}/{detail.hard_fact_sentences} 个硬事实句
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="grid gap-1 p-5">
                <p className="text-sm text-muted-foreground">证据链接</p>
                <p className="text-3xl font-semibold tabular-nums">{evidence.length}</p>
                <p className="text-sm text-muted-foreground">当前正文有效引用</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="grid gap-2 p-5">
                <p className="text-sm text-muted-foreground">标记说明</p>
                <div className="flex flex-wrap gap-2 text-sm">
                  <span className="rounded-md border border-emerald-300 bg-emerald-100 px-2 py-1 text-emerald-950 dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-100">
                    已有知识支撑
                  </span>
                  <span className="rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-amber-950 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
                    尚无支撑
                  </span>
                </div>
              </CardContent>
            </Card>
          </section>

          {detail.status !== "available" && (
            <Alert>
              <AlertCircle />
              <AlertTitle>
                {detail.status === "stale" ? "正文已经变化" : "支撑率尚不可用"}
              </AlertTitle>
              <AlertDescription>
                {detail.message || "请重新检查当前正文后再查看句子证据。"}
              </AlertDescription>
            </Alert>
          )}

          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(320px,0.42fr)]">
            <Card id="coverage-content">
              <CardHeader className="border-b">
                <h2 className="text-base font-medium leading-snug">正文证据映射</h2>
                <CardDescription>
                  颜色之外同时提供引用编号，键盘用户也可逐个打开证据。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-5 p-5 sm:p-6">
                {detail.paragraphs.length ? (
                  detail.paragraphs.map((paragraph) => (
                    <p
                      key={paragraph.paragraph_id}
                      className="text-base leading-8 text-foreground"
                    >
                      {paragraph.sentences.map((sentence) => {
                        if (sentence.supported && sentence.evidence.length) {
                          return (
                            <span key={sentence.sentence_id}>
                              <span
                                className={cn(
                                  "box-decoration-clone rounded-sm border-b border-emerald-500 bg-emerald-100 px-0.5 text-emerald-950",
                                  "dark:border-emerald-700 dark:bg-emerald-950/55 dark:text-emerald-100",
                                )}
                              >
                                {sentence.text}
                                {sentence.evidence.map((reference) => {
                                  const number = citationNumbers.get(
                                    reference.evidence_link_id,
                                  );
                                  if (!number) return null;
                                  return (
                                    <a
                                      key={reference.evidence_link_id}
                                      href={`#evidence-${reference.evidence_link_id}`}
                                      aria-label={`查看引用 ${number}：${reference.source_name}`}
                                      className={cn(
                                        "ml-0.5 rounded-sm font-semibold underline decoration-emerald-700 underline-offset-2 transition-colors",
                                        "hover:bg-emerald-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600 focus-visible:ring-offset-2",
                                        "dark:decoration-emerald-300 dark:hover:bg-emerald-900/70",
                                      )}
                                    >
                                      <sup>[{number}]</sup>
                                    </a>
                                  );
                                })}
                              </span>{" "}
                            </span>
                          );
                        }
                        return (
                          <span
                            key={sentence.sentence_id}
                            className={cn(
                              sentence.eligible &&
                                detail.status === "available" &&
                                "rounded-sm border-b border-dashed border-amber-500 bg-amber-50 px-0.5 text-amber-950 dark:border-amber-700 dark:bg-amber-950/35 dark:text-amber-100",
                            )}
                          >
                            {sentence.text}{" "}
                          </span>
                        );
                      })}
                    </p>
                  ))
                ) : (
                  <p className="text-sm text-muted-foreground">当前没有可显示的正文句子。</p>
                )}
              </CardContent>
            </Card>

            <aside className="grid gap-3 xl:sticky xl:top-6" aria-label="引用证据">
              <div className="flex items-center justify-between gap-3 px-1">
                <div>
                  <h2 className="font-semibold">引用证据</h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    官网链接在新窗口打开。
                  </p>
                </div>
                <Badge variant="outline">{evidence.length} 条</Badge>
              </div>
              {evidence.length ? (
                evidence.map((reference) => (
                  <Card
                    key={reference.evidence_link_id}
                    id={`evidence-${reference.evidence_link_id}`}
                    className="scroll-mt-6"
                  >
                    <CardHeader className="gap-2 border-b p-4">
                      <div className="flex items-start justify-between gap-3">
                        <h3 className="text-base font-medium leading-6">
                          [{citationNumbers.get(reference.evidence_link_id)}] {reference.source_name}
                        </h3>
                        <Badge variant="outline" className="shrink-0">
                          {sourceKindLabel(reference.source_kind)}
                        </Badge>
                      </div>
                      <CardDescription className="leading-5">
                        {reference.heading_path.join(" › ") || "来源正文"}
                      </CardDescription>
                    </CardHeader>
                    <CardContent className="grid gap-3 p-4">
                      <p className="text-sm leading-6 text-muted-foreground">
                        {reference.excerpt}
                      </p>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="secondary">
                          {reference.claim_type === "hard_fact" ? "硬事实" : "参考支撑"}
                        </Badge>
                        <Badge variant="secondary">{reference.support_type}</Badge>
                      </div>
                      {reference.canonical_url ? (
                        <Button
                          nativeButton={false}
                          variant="outline"
                          className="min-h-11 w-full"
                          render={
                            <a
                              href={reference.canonical_url}
                              target="_blank"
                              rel="noreferrer"
                            />
                          }
                        >
                          <ExternalLink />
                          打开来源页面
                        </Button>
                      ) : (
                        <div className="grid gap-3 rounded-lg border bg-muted/35 p-3">
                          <div className="flex items-start gap-2 text-sm text-muted-foreground">
                            <Link2 className="mt-0.5 size-4 shrink-0" />
                            <span>这是内部知识库资料，没有公开官网链接。</span>
                          </div>
                          <Button
                            nativeButton={false}
                            variant="outline"
                            className="min-h-11 w-full bg-background"
                            render={
                              <a
                                href={apiFileUrl(
                                  `/api/knowledge/${encodeURIComponent(customer)}/sources/${encodeURIComponent(reference.source_id)}/snapshots/${encodeURIComponent(reference.snapshot_id)}/evidence/preview`,
                                )}
                                target="_blank"
                                rel="noreferrer"
                              />
                            }
                          >
                            <ExternalLink />
                            查看知识库证据
                          </Button>
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))
              ) : (
                <Card>
                  <CardContent className="p-5 text-sm leading-6 text-muted-foreground">
                    当前正文还没有可展示的有效证据链接。
                  </CardContent>
                </Card>
              )}
            </aside>
          </div>
        </>
      )}
    </main>
  );
}
