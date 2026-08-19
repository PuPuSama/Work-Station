"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  Loader2,
  Save,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useMemo, useState } from "react";

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
import { Textarea } from "@/components/ui/textarea";
import { apiPost, apiPut } from "@/lib/api";
import type {
  SeoReviewChange,
  SeoReviewPreview,
  SeoReviewRun,
  TaskRecord,
} from "@/types";

type ReviewDecision = SeoReviewChange["decision"];

type ReviewDraft = {
  decision: ReviewDecision;
  reviewedText: string;
  confirmRisks: boolean;
};

type ServerSeoReviewPanelProps = {
  task: TaskRecord;
  taskApi: string;
  pending: string;
  editAllowed: boolean;
  reviewAllowed: boolean;
  runAction: (
    label: string,
    action: () => Promise<unknown>,
    successMessage?: string,
  ) => Promise<unknown>;
  runJob: (label: string, endpoint: string) => Promise<unknown>;
};

function messageFor(error: unknown) {
  return error instanceof Error ? error.message : "SEO Review 操作失败，请重试。";
}

function latestSelectableReview(reviews: SeoReviewRun[]) {
  return (
    [...reviews].reverse().find((review) => review.status === "open") ??
    reviews.at(-1) ??
    null
  );
}

function decisionLabel(decision: ReviewDecision) {
  if (decision === "accepted") return "接受";
  if (decision === "rejected") return "拒绝";
  return "待裁决";
}

function operationLabel(operation: SeoReviewChange["operation"]) {
  if (operation === "insert_after") return "在后方插入";
  if (operation === "delete") return "删除";
  if (operation === "structure") return "结构调整";
  return "替换";
}

function reviewStatusLabel(status: SeoReviewRun["status"]) {
  if (status === "applied") return "已应用";
  if (status === "completed") return "已完成（未改正文）";
  return "审阅中";
}

function draftFor(
  change: SeoReviewChange,
  drafts: Record<string, ReviewDraft>,
): ReviewDraft {
  return (
    drafts[change.id] ?? {
      decision: change.decision,
      reviewedText: change.reviewed_text || change.model_proposed_text,
      confirmRisks: change.risk_confirmed,
    }
  );
}

function DiffLine({ prefix, text }: { prefix: " " | "-" | "+"; text: string }) {
  const tone =
    prefix === "-"
      ? "bg-red-50 text-red-900 dark:bg-red-950/40 dark:text-red-200"
      : prefix === "+"
        ? "bg-emerald-50 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200"
        : "text-muted-foreground";
  return (
    <div className={`grid grid-cols-[2rem_minmax(0,1fr)] px-2 ${tone}`}>
      <span aria-hidden="true" className="select-none text-center opacity-70">
        {prefix}
      </span>
      <span className="whitespace-pre-wrap break-words py-0.5">{text || " "}</span>
    </div>
  );
}

function GitDiff({
  source,
  change,
  reviewedText,
}: {
  source: string;
  change: SeoReviewChange;
  reviewedText: string;
}) {
  const validRange =
    change.source_start >= 0 && change.source_end >= change.source_start;
  const contextBefore = validRange
    ? source.slice(0, change.source_start).split("\n").slice(-2)
    : [];
  const contextAfter = validRange
    ? source.slice(change.source_end).split("\n").slice(0, 2)
    : [];
  const removed =
    change.operation === "insert_after" ? [] : change.target_text.split("\n");
  const added =
    change.operation === "delete" ? [] : reviewedText.split("\n");

  return (
    <div
      className="overflow-hidden rounded-md border bg-background font-mono text-xs leading-5"
      role="region"
      aria-label={`${change.title} 修改对比`}
    >
      <div className="border-b bg-muted/50 px-3 py-2 text-muted-foreground">
        @@ {validRange ? `${change.source_start},${change.source_end}` : "定位不可用"} @@
      </div>
      <div className="max-h-96 overflow-auto py-1">
        {contextBefore.map((line, index) => (
          <DiffLine key={`before-${index}`} prefix=" " text={line} />
        ))}
        {removed.map((line, index) => (
          <DiffLine key={`removed-${index}`} prefix="-" text={line} />
        ))}
        {added.map((line, index) => (
          <DiffLine key={`added-${index}`} prefix="+" text={line} />
        ))}
        {contextAfter.map((line, index) => (
          <DiffLine key={`after-${index}`} prefix=" " text={line} />
        ))}
      </div>
    </div>
  );
}

export function ServerSeoReviewPanel({
  task,
  taskApi,
  pending,
  editAllowed,
  reviewAllowed,
  runAction,
  runJob,
}: ServerSeoReviewPanelProps) {
  const reviews = task.seo_reviews ?? [];
  const preferredReview = latestSelectableReview(reviews);
  const [requestedReviewId, setRequestedReviewId] = useState("");
  const [drafts, setDrafts] = useState<Record<string, ReviewDraft>>({});
  const [preview, setPreview] = useState<SeoReviewPreview | null>(null);
  const [previewPending, setPreviewPending] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [confirmPending, setConfirmPending] = useState(false);
  const [primaryKeyword, setPrimaryKeyword] = useState(
    task.seo_primary_keyword ?? "",
  );
  const [longTailKeywords, setLongTailKeywords] = useState(
    (task.seo_long_tail_keywords ?? []).join("\n"),
  );

  const review =
    reviews.find((item) => item.id === requestedReviewId) ??
    preferredReview;
  const summary = useMemo(() => {
    const changes = review?.changes ?? [];
    return {
      accepted: changes.filter((change) => change.decision === "accepted")
        .length,
      rejected: changes.filter((change) => change.decision === "rejected")
        .length,
      pending: changes.filter((change) => change.decision === "pending").length,
      invalid: changes.filter((change) => !change.applicable).length,
    };
  }, [review]);
  const busy = Boolean(pending) || previewPending;
  const open = review?.status === "open";

  function updateDraft(change: SeoReviewChange, patch: Partial<ReviewDraft>) {
    setPreview(null);
    setPreviewError("");
    setDrafts((current) => ({
      ...current,
      [change.id]: {
        ...draftFor(change, current),
        ...patch,
      },
    }));
  }

  async function buildPreview() {
    if (!review) return;
    setPreviewPending(true);
    setPreviewError("");
    try {
      const nextPreview = await apiPost<SeoReviewPreview>(
        `${taskApi}/seo-reviews/${encodeURIComponent(review.id)}/preview`,
        { revision: task.revision ?? 0 },
      );
      setPreview(nextPreview);
    } catch (error) {
      setPreview(null);
      setPreviewError(messageFor(error));
    } finally {
      setPreviewPending(false);
    }
  }

  return (
    <Card className="xl:col-span-2">
      <CardHeader className="border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="grid gap-1">
            <CardTitle>SEO Review 裁决台</CardTitle>
            <CardDescription>
              先保存关键词并生成 Review，再逐条裁决、生成精确预览，最后应用或不改正文直接完成。
            </CardDescription>
          </div>
          {review && (
            <Badge variant={review.status === "open" ? "default" : "secondary"}>
              {reviewStatusLabel(review.status)}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="grid gap-6">
        <section className="grid gap-4" aria-labelledby="seo-inputs-title">
          <div>
            <h3 id="seo-inputs-title" className="text-sm font-semibold">
              1. Review 输入
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              Prompt 内容由 Project 配置解析；浏览器只提交关键词和
              project_default 选择。
            </p>
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="server-seo-primary-keyword">
                Primary Keyword
              </Label>
              <Input
                id="server-seo-primary-keyword"
                value={primaryKeyword}
                disabled={busy || !editAllowed}
                onChange={(event) => setPrimaryKeyword(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="server-seo-long-tail-keywords">
                Long-tail Keywords（每行一个）
              </Label>
              <Textarea
                id="server-seo-long-tail-keywords"
                value={longTailKeywords}
                disabled={busy || !editAllowed}
                className="min-h-24 resize-y"
                onChange={(event) => setLongTailKeywords(event.target.value)}
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              disabled={busy || !editAllowed}
              onClick={() =>
                void runAction("保存 SEO 设置", () =>
                  apiPut<TaskRecord>(`${taskApi}/seo-review-settings`, {
                    revision: task.revision ?? 0,
                    primary_keyword: primaryKeyword,
                    long_tail_keywords: longTailKeywords
                      .split("\n")
                      .map((value) => value.trim())
                      .filter(Boolean),
                    prompt_selection: "project_default",
                  }),
                )
              }
            >
              <Save />
              保存设置
            </Button>
            <Button
              type="button"
              className="min-h-11"
              disabled={busy || !reviewAllowed}
              onClick={() => void runJob("生成 SEO Review", "seo-reviews")}
            >
              {pending === "生成 SEO Review" ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Sparkles />
              )}
              生成 Review
            </Button>
          </div>
        </section>

        {reviews.length === 0 ? (
          <Alert>
            <ShieldCheck />
            <AlertTitle>还没有 Review Run</AlertTitle>
            <AlertDescription>
              保存关键词后生成第一份 Review。生成任务在服务端运行，完成后会读取最新
              Task Revision。
            </AlertDescription>
          </Alert>
        ) : (
          <>
            <section className="grid gap-4" aria-labelledby="seo-run-title">
              <h3 id="seo-run-title" className="text-sm font-semibold">
                2. 选择 Review Run
              </h3>
              <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-end">
                <div className="grid gap-2">
                  <Label htmlFor="server-seo-review-run">
                    Review Run
                  </Label>
                  <select
                    id="server-seo-review-run"
                    className="h-11 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    value={review?.id ?? ""}
                    disabled={busy}
                    onChange={(event) => {
                      setRequestedReviewId(event.target.value);
                      setPreview(null);
                      setPreviewError("");
                      setConfirmPending(false);
                    }}
                  >
                    {reviews.map((item, index) => (
                      <option key={item.id} value={item.id}>
                        #{index + 1} · {reviewStatusLabel(item.status)} ·{" "}
                        {item.score.toFixed(1)} 分
                      </option>
                    ))}
                  </select>
                </div>
                {review && (
                  <div className="grid grid-cols-2 gap-2 text-center text-xs sm:grid-cols-4">
                    <div className="rounded-md border px-3 py-2">
                      <strong className="block text-base text-foreground">
                        {summary.accepted}
                      </strong>
                      接受
                    </div>
                    <div className="rounded-md border px-3 py-2">
                      <strong className="block text-base text-foreground">
                        {summary.rejected}
                      </strong>
                      拒绝
                    </div>
                    <div className="rounded-md border px-3 py-2">
                      <strong className="block text-base text-foreground">
                        {summary.pending}
                      </strong>
                      待裁决
                    </div>
                    <div className="rounded-md border px-3 py-2">
                      <strong className="block text-base text-foreground">
                        {summary.invalid}
                      </strong>
                      不可应用
                    </div>
                  </div>
                )}
              </div>

              {review && (
                <div className="grid gap-4 rounded-lg border bg-muted/20 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="text-2xl font-semibold tabular-nums">
                        {review.score.toFixed(1)}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {review.publish_ready ? "达到发布门槛" : "建议修订后发布"}
                      </p>
                    </div>
                    <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
                      {review.publish_recommendation}
                    </p>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {review.dimensions.map((dimension) => (
                      <div
                        key={dimension.key}
                        className="rounded-md border bg-background p-3"
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-sm font-medium">
                            {dimension.name}
                          </span>
                          <span className="text-sm tabular-nums">
                            {dimension.score}/{dimension.target_score}
                          </span>
                        </div>
                        <p className="mt-2 text-xs leading-5 text-muted-foreground">
                          {dimension.main_issue}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </section>

            {review && (
              <section
                className="grid gap-4"
                aria-labelledby="seo-decisions-title"
              >
                <div>
                  <h3 id="seo-decisions-title" className="text-sm font-semibold">
                    3. 逐条裁决
                  </h3>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    每次保存只更新一个 Change。保存会推进 Task Revision，并使旧预览失效。
                  </p>
                </div>
                {review.changes.length === 0 ? (
                  <Alert>
                    <CheckCircle2 />
                    <AlertTitle>没有建议变更</AlertTitle>
                    <AlertDescription>
                      可直接完成 Review，不会改写当前文章。
                    </AlertDescription>
                  </Alert>
                ) : (
                  <div className="grid gap-4">
                    {review.changes.map((change, index) => {
                      const draft = draftFor(change, drafts);
                      const needsRiskConfirmation =
                        draft.decision === "accepted" &&
                        change.risks.length > 0;
                      return (
                        <article
                          key={change.id}
                          className="grid gap-4 rounded-lg border p-4"
                        >
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                              <p className="text-xs font-medium text-muted-foreground">
                                Change {index + 1} ·{" "}
                                {operationLabel(change.operation)}
                              </p>
                              <h4 className="mt-1 font-semibold">
                                {change.title}
                              </h4>
                            </div>
                            <div className="flex flex-wrap gap-2">
                              {change.hard_problem && (
                                <Badge variant="destructive">硬问题</Badge>
                              )}
                              {!change.applicable && (
                                <Badge variant="outline">不可应用</Badge>
                              )}
                              <Badge variant="secondary">
                                {decisionLabel(change.decision)}
                              </Badge>
                            </div>
                          </div>

                          <p className="text-sm leading-6 text-muted-foreground">
                            {change.rationale}
                          </p>

                          <div className="grid gap-2">
                            <Label>修改对比</Label>
                            <GitDiff
                              source={review.source_article}
                              change={change}
                              reviewedText={draft.reviewedText}
                            />
                          </div>

                          <div className="grid gap-2">
                            <Label htmlFor={`seo-reviewed-${change.id}`}>
                              人工确认后的内容
                            </Label>
                            <Textarea
                              id={`seo-reviewed-${change.id}`}
                              value={draft.reviewedText}
                              disabled={busy || !reviewAllowed || !open}
                              className="min-h-32 resize-y font-mono text-sm leading-6"
                              onChange={(event) =>
                                updateDraft(change, {
                                  reviewedText: event.target.value,
                                })
                              }
                            />
                          </div>

                          {change.validation_errors.length > 0 && (
                            <Alert variant="destructive">
                              <AlertTriangle />
                              <AlertTitle>这条变更当前不可应用</AlertTitle>
                              <AlertDescription>
                                <ul className="list-disc space-y-1 pl-4">
                                  {change.validation_errors.map((item) => (
                                    <li key={item}>{item}</li>
                                  ))}
                                </ul>
                              </AlertDescription>
                            </Alert>
                          )}

                          {change.risks.length > 0 && (
                            <div className="grid gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-950">
                              <div className="flex items-center gap-2 text-sm font-semibold">
                                <AlertTriangle className="size-4" />
                                受保护事实风险（{change.risks.length}）
                              </div>
                              <ul className="grid gap-2 text-xs leading-5">
                                {change.risks.map((risk, riskIndex) => (
                                  <li key={`${risk.kind}-${riskIndex}`}>
                                    <strong>{risk.label}：</strong>
                                    {risk.message}
                                  </li>
                                ))}
                              </ul>
                              <label className="flex min-h-11 cursor-pointer items-center gap-3 rounded-md border border-amber-400 bg-white px-3 text-sm">
                                <input
                                  type="checkbox"
                                  checked={draft.confirmRisks}
                                  disabled={busy || !reviewAllowed || !open}
                                  onChange={(event) =>
                                    updateDraft(change, {
                                      confirmRisks: event.target.checked,
                                    })
                                  }
                                />
                                我已核对数字、URL、品牌和产品事实，确认接受风险
                              </label>
                            </div>
                          )}

                          <div className="flex flex-wrap items-end gap-3">
                            <div className="grid min-w-44 gap-2">
                              <Label htmlFor={`seo-decision-${change.id}`}>
                                裁决
                              </Label>
                              <select
                                id={`seo-decision-${change.id}`}
                                className="h-11 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                value={draft.decision}
                                disabled={busy || !reviewAllowed || !open}
                                onChange={(event) =>
                                  updateDraft(change, {
                                    decision: event.target
                                      .value as ReviewDecision,
                                  })
                                }
                              >
                                <option value="pending">待裁决</option>
                                <option
                                  value="accepted"
                                  disabled={!change.applicable}
                                >
                                  接受
                                </option>
                                <option value="rejected">拒绝</option>
                              </select>
                            </div>
                            <Button
                              type="button"
                              variant="outline"
                              className="min-h-11"
                              disabled={
                                busy ||
                                !reviewAllowed ||
                                !open ||
                                (needsRiskConfirmation &&
                                  !draft.confirmRisks)
                              }
                              onClick={() => {
                                setPreview(null);
                                void runAction(
                                  `保存 Change ${index + 1}`,
                                  () =>
                                    apiPut<TaskRecord>(
                                      `${taskApi}/seo-reviews/${encodeURIComponent(review.id)}/changes/${encodeURIComponent(change.id)}`,
                                      {
                                        revision: task.revision ?? 0,
                                        decision: draft.decision,
                                        reviewed_text: draft.reviewedText,
                                        confirm_risks: draft.confirmRisks,
                                      },
                                    ),
                                  `Change ${index + 1} 已保存；旧预览（如有）已作废。`,
                                );
                              }}
                            >
                              {pending === `保存 Change ${index + 1}` ? (
                                <Loader2 className="animate-spin" />
                              ) : (
                                <Save />
                              )}
                              保存此条
                            </Button>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                )}
              </section>
            )}

            {review && (
              <section
                className="grid gap-4"
                aria-labelledby="seo-finalize-title"
              >
                <div>
                  <h3 id="seo-finalize-title" className="text-sm font-semibold">
                    4. 预览与完成
                  </h3>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    “应用”只接受这次精确预览的 Hash；任一裁决或正文变化都会阻止旧预览落地。
                  </p>
                </div>

                {summary.pending > 0 && (
                  <label className="flex min-h-11 cursor-pointer items-center gap-3 rounded-md border px-3 text-sm">
                    <input
                      type="checkbox"
                      checked={confirmPending}
                      disabled={busy || !reviewAllowed || !open}
                      onChange={(event) =>
                        setConfirmPending(event.target.checked)
                      }
                    />
                    我确认仍有 {summary.pending} 条待裁决，并选择跳过它们
                  </label>
                )}

                {previewError && (
                  <Alert variant="destructive" role="alert">
                    <AlertTriangle />
                    <AlertTitle>无法生成预览</AlertTitle>
                    <AlertDescription>{previewError}</AlertDescription>
                  </Alert>
                )}

                {preview && preview.review_id === review.id && (
                  <div className="grid gap-3 rounded-lg border bg-muted/20 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="text-sm font-semibold">精确应用预览</p>
                        <p className="text-xs text-muted-foreground">
                          接受 {preview.accepted_change_ids.length} 条 · 待裁决{" "}
                          {preview.pending_count} 条 · 无效 {preview.invalid_count} 条
                        </p>
                      </div>
                      <Badge
                        variant={
                          preview.structure_valid ? "default" : "destructive"
                        }
                      >
                        {preview.structure_valid ? "结构校验通过" : "结构校验失败"}
                      </Badge>
                    </div>
                    <Textarea
                      aria-label="SEO Review 应用预览正文"
                      value={preview.article}
                      readOnly
                      className="min-h-72 resize-y bg-background font-mono text-xs leading-5"
                    />
                  </div>
                )}

                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11"
                    disabled={busy || !reviewAllowed || !open}
                    onClick={() => void buildPreview()}
                  >
                    {previewPending ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Eye />
                    )}
                    生成精确预览
                  </Button>
                  <Button
                    type="button"
                    className="min-h-11"
                    disabled={
                      busy ||
                      !editAllowed ||
                      !open ||
                      !preview ||
                      summary.accepted === 0 ||
                      preview.review_id !== review.id ||
                      (summary.pending > 0 && !confirmPending)
                    }
                    onClick={() => {
                      if (!preview) return;
                      const previewHash = preview.article_hash;
                      setPreview(null);
                      void runAction(
                        "应用 SEO Review",
                        () =>
                          apiPost<TaskRecord>(
                            `${taskApi}/seo-reviews/${encodeURIComponent(review.id)}/apply`,
                            {
                              revision: task.revision ?? 0,
                              preview_hash: previewHash,
                              confirm_pending: confirmPending,
                            },
                          ),
                        "已按精确预览应用接受项；Task Revision 已更新。",
                      );
                    }}
                  >
                    <CheckCircle2 />
                    应用接受项
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    className="min-h-11"
                    disabled={
                      busy ||
                      !reviewAllowed ||
                      !open ||
                      summary.accepted > 0 ||
                      (summary.pending > 0 && !confirmPending)
                    }
                    onClick={() => {
                      setPreview(null);
                      void runAction(
                        "完成 SEO Review",
                        () =>
                          apiPost<TaskRecord>(
                            `${taskApi}/seo-reviews/${encodeURIComponent(review.id)}/complete`,
                            {
                              revision: task.revision ?? 0,
                              confirm_pending: confirmPending,
                            },
                          ),
                        "Review 已完成，当前正文保持不变。",
                      );
                    }}
                  >
                    <ShieldCheck />
                    不改正文并完成
                  </Button>
                </div>

                {summary.accepted > 0 && !preview && (
                  <p className="text-xs leading-5 text-muted-foreground">
                    已接受变更时不能直接完成。请先生成精确预览，再应用接受项。
                  </p>
                )}
              </section>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
