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
import {
  getResearchRun,
  listResearchPlans,
  listResearchRuns,
  startTargetedGapRepair,
} from "@/lib/research-api";
import { newResearchRequestId } from "@/lib/research-view-model";
import { cn } from "@/lib/utils";
import type {
  AccessibleProject,
  KnowledgeCoverageDetail,
  KnowledgeCoverageEvidenceDetail,
  KnowledgeRetrievalPlan,
  ResearchRunDetail,
  TaskRecord,
  TargetedKnowledgeGap,
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

function findRepairPlan(
  task: TaskRecord,
  plans: KnowledgeRetrievalPlan[],
): KnowledgeRetrievalPlan | null {
  const articleId = `topic_${String(task.topic_index).padStart(3, "0")}`;
  const outlineVersion = Math.max(
    1,
    (task.article_versions ?? []).filter(
      (version) =>
        version.kind === "outline" && version.source_kind === "manual_confirmed",
    ).length,
  );
  return (
    plans
      .filter(
        (plan) =>
          plan.article_id === articleId &&
          plan.outline_version === outlineVersion &&
          String(plan.metadata.task_id || "") === task.id,
      )
      .sort((left, right) => right.created_at.localeCompare(left.created_at))[0]
      ?? null
  );
}

function stringList(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function persistedTargetedGaps(
  plan: KnowledgeRetrievalPlan | null,
): TargetedKnowledgeGap[] {
  const raw = plan?.metadata.targeted_gaps;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return [];
    }
    const item = value as Record<string, unknown>;
    const required = [
      "gap_id",
      "sentence_id",
      "sentence_hash",
      "text",
      "claim_type",
      "scope_id",
      "scope_title",
      "reason",
      "query",
    ];
    if (required.some((key) => typeof item[key] !== "string")) return [];
    return [
      {
        gap_id: String(item.gap_id),
        sentence_id: String(item.sentence_id),
        sentence_hash: String(item.sentence_hash),
        text: String(item.text),
        claim_type: String(item.claim_type),
        hard_fact: item.hard_fact === true,
        scope_id: String(item.scope_id),
        scope_title: String(item.scope_title),
        product_id: typeof item.product_id === "string" ? item.product_id : "",
        reason: String(item.reason),
        query: String(item.query),
        requirement_ids: stringList(item.requirement_ids),
        h3_titles: stringList(item.h3_titles),
        query_variants: stringList(item.query_variants),
        article_brief_id:
          typeof item.article_brief_id === "string"
            ? item.article_brief_id
            : "",
        knowledge_snapshot_fingerprint:
          typeof item.knowledge_snapshot_fingerprint === "string"
            ? item.knowledge_snapshot_fingerprint
            : "",
      },
    ];
  });
}

function canPublish(project: AccessibleProject | undefined) {
  return (
    project?.effective_role === "org_admin" ||
    project?.effective_role === "editor" ||
    (project?.effective_role === "team_lead" &&
      Boolean(project.is_project_owner))
  );
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
  const [repairAllowed, setRepairAllowed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [repairPending, setRepairPending] = useState(false);
  const [repairPlan, setRepairPlan] =
    useState<KnowledgeRetrievalPlan | null>(null);
  const [repairPlanId, setRepairPlanId] = useState("");
  const [repairThreadId, setRepairThreadId] = useState("");
  const [repairRun, setRepairRun] = useState<ResearchRunDetail | null>(null);
  const [selectedGapSentenceIds, setSelectedGapSentenceIds] = useState<string[]>([]);
  const [coverageBeforeRepair, setCoverageBeforeRepair] = useState<{
    sentenceCoverage: number;
    supportedSentences: number;
    eligibleSentences: number;
  } | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    Promise.all([
      apiGet<KnowledgeCoverageDetail>(detailApi),
      apiGet<AccessibleProject[]>("/api/projects"),
      apiGet<TaskRecord>(taskApi),
      listResearchPlans(customer),
      listResearchRuns(customer),
    ])
      .then(([nextDetail, projects, task, plans, runs]) => {
        if (!active) return;
        const project = projects.find((item) =>
          sameProjectId(item.project_id, customer),
        );
        const nextRepairPlan = findRepairPlan(task, plans);
        const nextRepairRun = runs
          .filter(
            (run) =>
              run.retrieval_plan_id === nextRepairPlan?.retrieval_plan_id,
          )
          .sort((left, right) =>
            String(right.updated_at || "").localeCompare(
              String(left.updated_at || ""),
            ),
          )[0];
        setDetail(nextDetail);
        setReviewAllowed(canReview(project));
        setRepairAllowed(canPublish(project));
        setRepairPlan(nextRepairPlan);
        setRepairPlanId(nextRepairPlan?.retrieval_plan_id ?? "");
        setRepairThreadId(nextRepairRun?.thread_id ?? "");
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
  }, [customer, detailApi, taskApi]);

  useEffect(() => {
    let active = true;
    if (!repairThreadId) {
      return () => {
        active = false;
      };
    }
    getResearchRun(customer, repairThreadId)
      .then((nextRun) => {
        if (active) setRepairRun(nextRun);
      })
      .catch(() => {
        if (active) setRepairRun(null);
      });
    return () => {
      active = false;
    };
  }, [customer, repairThreadId]);

  const unsupportedSentences = useMemo(
    () =>
      (detail?.paragraphs ?? []).flatMap((paragraph) =>
        paragraph.sentences.filter(
          (sentence) => sentence.eligible && !sentence.supported,
        ),
      ),
    [detail],
  );

  const targetedGaps = useMemo(
    () => persistedTargetedGaps(repairPlan),
    [repairPlan],
  );
  const displayedRepairRun = repairThreadId ? repairRun : null;

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
    setCoverageBeforeRepair({
      sentenceCoverage: detail.sentence_coverage,
      supportedSentences: detail.supported_sentences,
      eligibleSentences: detail.eligible_sentences,
    });
    try {
      await apiPut<TaskRecord>(detailApi, {
        revision: detail.task_revision,
      });
      const refreshed = await apiGet<KnowledgeCoverageDetail>(detailApi);
      setDetail(refreshed);
      const before = Math.round(detail.sentence_coverage * 100);
      const after = Math.round(refreshed.sentence_coverage * 100);
      setMessage(
        `已按当前正文重新计算知识库支撑率：${before}% → ${after}%。`,
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPending(false);
    }
  }

  async function startRepair() {
    if (
      !detail ||
      !repairAllowed ||
      !repairPlanId ||
      !selectedGapSentenceIds.length ||
      selectedGapSentenceIds.length > 12
    ) {
      return;
    }
    setRepairPending(true);
    setError("");
    setMessage("");
    try {
      const queued = await startTargetedGapRepair(customer, taskId, {
        revision: detail.task_revision,
        request_id: newResearchRequestId("start"),
        retrieval_plan_id: repairPlanId,
        sentence_ids: selectedGapSentenceIds,
        max_discovery_queries: 2,
      });
      setMessage(
        `已创建精准补检：${queued.targeted_scope_ids.length} 个 scope，${queued.gaps.length} 个缺口句进入队列。未受影响的证据包已复用。`,
      );
      setRepairPlan(queued.plan);
      setRepairPlanId(queued.plan.retrieval_plan_id);
      setRepairThreadId(queued.run.thread_id);
      setSelectedGapSentenceIds([]);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setRepairPending(false);
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

          {detail.status === "available" &&
            (unsupportedSentences.length > 0 ||
              targetedGaps.length > 0 ||
              Boolean(displayedRepairRun)) && (
            <Card>
              <CardHeader className="gap-2 border-b">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="text-base font-medium leading-snug">精准补检缺口</h2>
                    <CardDescription className="mt-1 max-w-3xl leading-6">
                      只对勾选的无支撑句所在 scope 重新检索；已有且未受影响的证据包会继续复用，不会修改正文。
                    </CardDescription>
                  </div>
                  <Badge variant="outline">
                    已选 {selectedGapSentenceIds.length}/12
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="grid gap-4 p-5 sm:p-6">
                <div className="grid gap-2">
                  {unsupportedSentences.slice(0, 12).map((sentence) => {
                    const checked = selectedGapSentenceIds.includes(
                      sentence.sentence_id,
                    );
                    return (
                      <label
                        key={sentence.sentence_id}
                        className={cn(
                          "flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors",
                          checked
                            ? "border-sky-300 bg-sky-50 dark:border-sky-800 dark:bg-sky-950/30"
                            : "border-border hover:bg-muted/50",
                        )}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={
                            repairPending ||
                            (!checked && selectedGapSentenceIds.length >= 12)
                          }
                          onChange={() => {
                            setSelectedGapSentenceIds((current) =>
                              checked
                                ? current.filter(
                                    (id) => id !== sentence.sentence_id,
                                  )
                                : [...current, sentence.sentence_id],
                            );
                          }}
                          className="mt-1 size-4 shrink-0 accent-sky-600"
                          aria-label={`选择缺口句：${sentence.text}`}
                        />
                        <span className="min-w-0 text-sm leading-6">
                          {sentence.text}
                          {sentence.hard_fact && (
                            <Badge
                              variant="outline"
                              className="ml-2 align-middle text-amber-700 dark:text-amber-300"
                            >
                              硬事实
                            </Badge>
                          )}
                        </span>
                      </label>
                    );
                  })}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p className="text-sm text-muted-foreground">
                    {unsupportedSentences.length > 12
                      ? `当前显示前 12 个缺口，共 ${unsupportedSentences.length} 个。`
                      : `共 ${unsupportedSentences.length} 个缺口句。`}
                    {!repairPlanId && " 当前文章还没有可用的检索计划。"}
                  </p>
                  <Button
                    type="button"
                    className="min-h-11"
                    disabled={
                      repairPending ||
                      !repairAllowed ||
                      !repairPlanId ||
                      selectedGapSentenceIds.length === 0
                    }
                    onClick={() => void startRepair()}
                  >
                    {repairPending ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <RefreshCw />
                    )}
                    开始精准补检
                  </Button>
                </div>

                {coverageBeforeRepair ? (
                  <div className="rounded-lg border bg-muted/25 p-3 text-sm">
                    <p className="font-medium">复检前后差异</p>
                    <p className="mt-1 text-muted-foreground">
                      上一次复检前：
                      {Math.round(coverageBeforeRepair.sentenceCoverage * 100)}%
                      （{coverageBeforeRepair.supportedSentences}/
                      {coverageBeforeRepair.eligibleSentences} 个合格句）；当前：
                      {Math.round(detail.sentence_coverage * 100)}%（
                      {detail.supported_sentences}/{detail.eligible_sentences} 个合格句）。
                    </p>
                  </div>
                ) : null}

                {targetedGaps.length ? (
                  <div className="grid gap-3 rounded-xl border bg-muted/15 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <h3 className="font-medium">最近一次精准补检</h3>
                        <p className="mt-1 text-sm text-muted-foreground">
                          查询、H3 要求和产品归属均绑定在 repair Plan 中；不会自动修改正文。
                        </p>
                      </div>
                      {displayedRepairRun ? (
                        <Badge variant="outline">{displayedRepairRun.status}</Badge>
                      ) : null}
                    </div>
                    {targetedGaps.map((gap) => (
                      <div
                        key={gap.gap_id}
                        className="grid gap-2 rounded-lg border bg-background p-3 text-sm"
                      >
                        <p className="leading-6">{gap.text}</p>
                        <div className="flex flex-wrap gap-2 text-xs">
                          <Badge variant="outline">{gap.claim_type}</Badge>
                          <Badge variant="outline">{gap.scope_title}</Badge>
                          {gap.product_id ? (
                            <Badge variant="secondary">
                              产品 {gap.product_id}
                            </Badge>
                          ) : null}
                          {gap.hard_fact ? (
                            <Badge variant="outline">硬事实</Badge>
                          ) : null}
                        </div>
                        {gap.h3_titles.length ? (
                          <p className="text-xs text-muted-foreground">
                            H3：{gap.h3_titles.join("；")}
                          </p>
                        ) : null}
                        <p className="text-xs text-muted-foreground">
                          缺口判断：{gap.reason}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          {(gap.query_variants.length
                            ? gap.query_variants
                            : [gap.query]
                          ).map((query) => (
                            <Badge
                              key={query}
                              variant="secondary"
                              className="max-w-full whitespace-normal text-left"
                            >
                              查询：{query}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    ))}
                    {displayedRepairRun?.gap_fill_attempts.length ? (
                      <div className="grid gap-2">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                          实际执行查询
                        </p>
                        {displayedRepairRun.gap_fill_attempts.map((attempt) => (
                          <div
                            key={attempt.attempt_id}
                            className="rounded-lg border bg-background p-3 text-sm"
                          >
                            <p className="break-words font-medium">{attempt.query}</p>
                            <p className="mt-1 text-xs text-muted-foreground">
                              {attempt.channel} · {attempt.result}
                            </p>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {displayedRepairRun?.review_candidates.length ? (
                      <div className="grid gap-2">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                          待人工确认的官网候选
                        </p>
                        {displayedRepairRun.review_candidates.map((candidate) => (
                          <a
                            key={candidate.candidate_id}
                            href={candidate.url}
                            target="_blank"
                            rel="noreferrer"
                            className="break-all rounded-lg border bg-background p-3 text-sm text-primary underline-offset-4 hover:underline"
                          >
                            {candidate.url}
                            <span className="mt-1 block text-xs text-muted-foreground no-underline">
                              {candidate.page_type} · 请在资料研究工作区完成批准或拒绝
                            </span>
                          </a>
                        ))}
                      </div>
                    ) : null}
                    {repairThreadId ? (
                      <Link
                        href={`${backHref.replace("step=review", "step=outline")}&thread=${encodeURIComponent(repairThreadId)}`}
                        className="text-sm text-primary underline-offset-4 hover:underline"
                      >
                        打开资料研究工作区查看完整运行与人工确认
                      </Link>
                    ) : null}
                  </div>
                ) : null}
              </CardContent>
            </Card>
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
