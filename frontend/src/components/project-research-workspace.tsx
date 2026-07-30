"use client";

import {
  AlertCircle,
  BookOpenCheck,
  CheckCircle2,
  ExternalLink,
  Loader2,
  Play,
  Radio,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ResearchRunTimeline } from "@/components/research-run-timeline";
import { ResearchAssistantSheet } from "@/components/research-assistant-sheet";
import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiFileUrl, apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  KnowledgeEvidencePack,
  KnowledgeRetrievalPlan,
  ResearchRun,
  ResearchRunDetail,
  ResearchRunQueued,
  ResearchRunStatus,
} from "@/types";

type ProjectResearchWorkspaceProps = {
  customer: string;
  articleId?: string;
  taskId?: string;
  embedded?: boolean;
};

type Transport = "idle" | "sse" | "polling";

const terminalStatuses = new Set<ResearchRunStatus>([
  "completed",
  "completed_with_warnings",
  "failed",
  "cancelled",
]);

const statusLabels: Record<ResearchRunStatus, string> = {
  queued: "排队中",
  running: "研究中",
  waiting_for_review: "等待人工复核",
  completed: "已完成",
  completed_with_warnings: "完成但有提醒",
  failed: "失败",
  cancelled: "已取消",
};

function statusVariant(status: ResearchRunStatus) {
  if (status === "failed" || status === "cancelled") return "destructive";
  if (status === "completed") return "default";
  return status === "waiting_for_review" ? "secondary" : "outline";
}

function formatTime(value: string | null) {
  if (!value) return "尚未更新";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "研究工作台发生未知错误";
}

function candidateUrl(candidate: Record<string, unknown>) {
  return typeof candidate.url === "string" ? candidate.url : "";
}

export function ProjectResearchWorkspace({
  customer,
  articleId,
  taskId,
  embedded = false,
}: ProjectResearchWorkspaceProps) {
  const [plans, setPlans] = useState<KnowledgeRetrievalPlan[]>([]);
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [detail, setDetail] = useState<ResearchRunDetail | null>(null);
  const [packs, setPacks] = useState<Record<string, KnowledgeEvidencePack>>({});
  const [approvedUrls, setApprovedUrls] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [transport, setTransport] = useState<Transport>("idle");
  const [streamRetry, setStreamRetry] = useState(0);
  const lastSequence = useRef(0);

  const projectPath = `/api/knowledge/${encodeURIComponent(customer)}`;
  const selectedPlan = useMemo(
    () => plans.find((plan) => plan.retrieval_plan_id === selectedPlanId) ?? null,
    [plans, selectedPlanId],
  );
  const selectedRun = useMemo(
    () => runs.find((run) => run.thread_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  );
  const selectedRunStatus = selectedRun?.status;

  const refreshDetail = useCallback(
    async (threadId: string) => {
      const next = await apiGet<ResearchRunDetail>(
        `${projectPath}/research-runs/${encodeURIComponent(threadId)}`,
      );
      setDetail(next);
      lastSequence.current = Math.max(
        0,
        ...next.events.map((event) => event.sequence),
      );
      setRuns((current) => {
        const existing = current.some((run) => run.thread_id === next.thread_id);
        return existing
          ? current.map((run) => (run.thread_id === next.thread_id ? next : run))
          : [next, ...current];
      });
      const loadedPacks = await Promise.all(
        next.evidence_pack_ids.map(async (packId) => {
          const pack = await apiGet<KnowledgeEvidencePack>(
            `${projectPath}/evidence-packs/${encodeURIComponent(packId)}`,
          );
          return [packId, pack] as const;
        }),
      );
      setPacks(Object.fromEntries(loadedPacks));
      return next;
    },
    [projectPath],
  );

  const refreshWorkspace = useCallback(async () => {
    const query = articleId
      ? `?article_id=${encodeURIComponent(articleId)}`
      : "";
    const [nextPlans, nextRuns] = await Promise.all([
      apiGet<KnowledgeRetrievalPlan[]>(`${projectPath}/retrieval-plans${query}`),
      apiGet<ResearchRun[]>(`${projectPath}/research-runs${query}`),
    ]);
    setPlans(nextPlans);
    setRuns(nextRuns);
    setSelectedPlanId((current) =>
      nextPlans.some((plan) => plan.retrieval_plan_id === current)
        ? current
        : (nextPlans[0]?.retrieval_plan_id ?? ""),
    );
    setSelectedRunId((current) =>
      nextRuns.some((run) => run.thread_id === current)
        ? current
        : (nextRuns[0]?.thread_id ?? ""),
    );
    if (!nextRuns.length) {
      setDetail(null);
      setPacks({});
    }
    return { nextPlans, nextRuns };
  }, [articleId, projectPath]);

  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      void refreshWorkspace()
        .catch((loadError) => {
          if (active) setError(errorMessage(loadError));
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [refreshWorkspace]);

  useEffect(() => {
    if (!selectedRunId) return;
    const timer = window.setTimeout(() => {
      void refreshDetail(selectedRunId).catch((loadError) =>
        setError(errorMessage(loadError)),
      );
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshDetail, selectedRunId]);

  useEffect(() => {
    if (
      !selectedRunId ||
      !selectedRunStatus ||
      terminalStatuses.has(selectedRunStatus)
    ) {
      return;
    }
    const streamUrl = apiFileUrl(
      `${projectPath}/research-runs/${encodeURIComponent(selectedRunId)}` +
        `/events/stream?after_sequence=${lastSequence.current}`,
    );
    const source = new EventSource(streamUrl, { withCredentials: true });
    source.onopen = () => setTransport("sse");
    let refreshTimer: number | undefined;
    const update = () => {
      if (refreshTimer !== undefined) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        void refreshDetail(selectedRunId).catch(() => setTransport("polling"));
      }, 150);
    };
    source.addEventListener("research_event", update);
    source.addEventListener("run_state", update);
    source.addEventListener("done", () => {
      update();
      source.close();
      setTransport("idle");
    });
    source.onerror = () => {
      source.close();
      setTransport("polling");
    };
    return () => {
      source.close();
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    };
  }, [
    projectPath,
    refreshDetail,
    selectedRunStatus,
    selectedRunId,
    streamRetry,
  ]);

  useEffect(() => {
    if (
      transport !== "polling" ||
      !selectedRunId ||
      !selectedRunStatus ||
      terminalStatuses.has(selectedRunStatus)
    ) {
      return;
    }
    const poll = window.setInterval(() => {
      void refreshDetail(selectedRunId).catch(() => undefined);
    }, 3_000);
    const retry = window.setTimeout(() => {
      setStreamRetry((value) => value + 1);
    }, 15_000);
    return () => {
      window.clearInterval(poll);
      window.clearTimeout(retry);
    };
  }, [refreshDetail, selectedRunId, selectedRunStatus, transport]);

  async function generatePlan() {
    if (!taskId) return;
    setBusy("plan");
    setError("");
    try {
      const plan = await apiPost<KnowledgeRetrievalPlan>(
        `${projectPath}/tasks/${encodeURIComponent(taskId)}/retrieval-plan`,
      );
      await refreshWorkspace();
      setSelectedPlanId(plan.retrieval_plan_id);
    } catch (runError) {
      setError(errorMessage(runError));
    } finally {
      setBusy("");
    }
  }

  async function startRun() {
    if (!selectedPlanId) return;
    setBusy("start");
    setError("");
    try {
      const queued = await apiPost<ResearchRunQueued>(
        `${projectPath}/research-runs`,
        {
          organization_id: `org:${customer.toLowerCase()}`,
          retrieval_plan_id: selectedPlanId,
          max_discovery_queries: 2,
        },
      );
      await refreshWorkspace();
      setSelectedRunId(queued.run.thread_id);
    } catch (runError) {
      setError(errorMessage(runError));
    } finally {
      setBusy("");
    }
  }

  async function resumeRun() {
    if (!detail) return;
    setBusy("resume");
    setError("");
    try {
      await apiPost<ResearchRunQueued>(
        `${projectPath}/research-runs/${encodeURIComponent(detail.thread_id)}/resume`,
        { approved_urls: Array.from(approvedUrls) },
      );
      setApprovedUrls(new Set());
      await refreshDetail(detail.thread_id);
    } catch (runError) {
      setError(errorMessage(runError));
    } finally {
      setBusy("");
    }
  }

  return (
    <section
      className={cn(
        "grid gap-4",
        !embedded && "mx-auto max-w-[1480px] px-5 pb-8",
      )}
    >
      <Card className="overflow-hidden">
        <CardHeader className="border-b bg-muted/25">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
                <BookOpenCheck className="size-4" />
                M5 research workspace
              </div>
              <CardTitle>资料研究</CardTitle>
              <CardDescription className="mt-1 max-w-3xl">
                按已确认大纲逐节检索，只读取当前项目已发布的知识，并保留每次研究的证据和执行轨迹。
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <ResearchAssistantSheet
                customer={customer}
                articleId={articleId}
              />
              {transport !== "idle" &&
              selectedRunStatus &&
              !terminalStatuses.has(selectedRunStatus) ? (
                <Badge variant="outline" className="gap-1">
                  <Radio className="size-3" />
                  {transport === "sse" ? "实时更新" : "轮询恢复"}
                </Badge>
              ) : null}
              <Badge variant="outline">只读资料，不改正文</Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4 py-5">
          {error ? (
            <Alert variant="destructive">
              <AlertCircle />
              <AlertTitle>研究工作台暂时不可用</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
              <AlertAction>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setError("");
                    setLoading(true);
                    void refreshWorkspace()
                      .catch((loadError) => setError(errorMessage(loadError)))
                      .finally(() => setLoading(false));
                  }}
                >
                  重试
                </Button>
              </AlertAction>
            </Alert>
          ) : null}

          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-end">
            <div className="grid gap-2">
              <Label htmlFor={`research-plan-${taskId ?? "project"}`}>
                检索计划
              </Label>
              <select
                id={`research-plan-${taskId ?? "project"}`}
                value={selectedPlanId}
                onChange={(event) => setSelectedPlanId(event.target.value)}
                className="min-h-11 rounded-lg border border-input bg-background px-3 text-sm"
                disabled={loading || busy !== ""}
              >
                <option value="">尚未创建检索计划</option>
                {plans.map((plan) => (
                  <option
                    key={plan.retrieval_plan_id}
                    value={plan.retrieval_plan_id}
                  >
                    {plan.article_id} · outline v{plan.outline_version} ·{" "}
                    {plan.scopes.length} scopes
                  </option>
                ))}
              </select>
            </div>
            {taskId ? (
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={() => void generatePlan()}
                disabled={busy !== ""}
              >
                {busy === "plan" ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <RefreshCw />
                )}
                从已确认大纲生成
              </Button>
            ) : null}
            <Button
              type="button"
              className="min-h-11"
              onClick={() => void startRun()}
              disabled={!selectedPlanId || busy !== ""}
            >
              {busy === "start" ? <Loader2 className="animate-spin" /> : <Play />}
              启动研究
            </Button>
          </div>

          {loading ? (
            <div className="grid gap-3 md:grid-cols-[260px_minmax(0,1fr)]">
              <Skeleton className="h-64" />
              <Skeleton className="h-64" />
            </div>
          ) : (
            <div className="grid min-w-0 gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
              <aside className="min-w-0 rounded-xl border bg-muted/10 p-2">
                <div className="flex items-center justify-between px-2 py-2">
                  <h3 className="text-sm font-semibold">运行记录</h3>
                  <Badge variant="outline">{runs.length}</Badge>
                </div>
                <div className="grid max-h-[680px] gap-1 overflow-y-auto">
                  {runs.map((run) => (
                    <button
                      key={run.thread_id}
                      type="button"
                      onClick={() => {
                        setApprovedUrls(new Set());
                        setSelectedRunId(run.thread_id);
                      }}
                      className={cn(
                        "min-h-14 rounded-lg px-3 py-2 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        selectedRunId === run.thread_id && "bg-accent",
                      )}
                    >
                      <span className="flex items-center gap-2">
                        <Badge variant={statusVariant(run.status)}>
                          {statusLabels[run.status]}
                        </Badge>
                        <span className="ml-auto text-[11px] text-muted-foreground">
                          {formatTime(run.updated_at)}
                        </span>
                      </span>
                      <span className="mt-1 block truncate font-mono text-xs">
                        {run.article_id} · v{run.outline_version}
                      </span>
                    </button>
                  ))}
                  {!runs.length ? (
                    <p className="p-3 text-sm text-muted-foreground">
                      当前范围还没有研究记录。
                    </p>
                  ) : null}
                </div>
              </aside>

              <div className="min-w-0">
                {detail ? (
                  <div className="grid gap-4">
                    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                      <Metric label="状态" value={statusLabels[detail.status]} />
                      <Metric label="当前节点" value={detail.current_node} mono />
                      <Metric
                        label="补证轮次"
                        value={`${detail.gap_fill_round} / ${detail.max_gap_fill_rounds}`}
                      />
                      <Metric
                        label="发现查询"
                        value={`${detail.discovery_queries_used} / ${detail.max_discovery_queries}`}
                      />
                    </div>

                    {detail.status === "waiting_for_review" ? (
                      <Alert>
                        <ShieldCheck />
                        <AlertTitle>需要人工确认候选来源</AlertTitle>
                        <AlertDescription>
                          只可勾选系统发现且属于当前官网的 URL；未勾选项不会摄入或发布。
                        </AlertDescription>
                        <div className="col-span-full mt-2 grid gap-2">
                          {detail.review_candidates.map((candidate) => {
                            const url = candidateUrl(candidate);
                            return (
                              <label
                                key={String(candidate.candidate_id ?? url)}
                                className="flex min-h-12 items-start gap-3 rounded-lg border bg-background p-3"
                              >
                                <input
                                  type="checkbox"
                                  className="mt-1 size-4"
                                  checked={approvedUrls.has(url)}
                                  onChange={(event) =>
                                    setApprovedUrls((current) => {
                                      const next = new Set(current);
                                      if (event.target.checked) next.add(url);
                                      else next.delete(url);
                                      return next;
                                    })
                                  }
                                />
                                <span className="min-w-0">
                                  <span className="block text-sm font-medium">
                                    {String(candidate.page_type ?? "official_page")}
                                  </span>
                                  <span className="block break-all text-xs text-muted-foreground">
                                    {url}
                                  </span>
                                </span>
                              </label>
                            );
                          })}
                          <Button
                            type="button"
                            className="min-h-11 justify-self-start"
                            onClick={() => void resumeRun()}
                            disabled={busy !== ""}
                          >
                            {busy === "resume" ? (
                              <Loader2 className="animate-spin" />
                            ) : (
                              <CheckCircle2 />
                            )}
                            确认并继续
                          </Button>
                        </div>
                      </Alert>
                    ) : null}

                    {detail.error_message ? (
                      <Alert variant="destructive">
                        <AlertCircle />
                        <AlertTitle>{detail.error_code ?? "研究失败"}</AlertTitle>
                        <AlertDescription>{detail.error_message}</AlertDescription>
                      </Alert>
                    ) : null}

                    <ScopeEvidence
                      plan={
                        plans.find(
                          (plan) =>
                            plan.retrieval_plan_id === detail.retrieval_plan_id,
                        ) ?? selectedPlan
                      }
                      packs={packs}
                    />

                    <div className="rounded-xl border p-4">
                      <ResearchRunTimeline
                        events={detail.events}
                        attempts={detail.gap_fill_attempts}
                      />
                    </div>
                  </div>
                ) : selectedPlan ? (
                  <ScopeEvidence plan={selectedPlan} packs={{}} />
                ) : (
                  <div className="flex min-h-64 items-center justify-center rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">
                    先从文章的已确认大纲生成检索计划，或选择已有计划。
                  </div>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

function Metric({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="rounded-lg border bg-muted/15 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-1 truncate text-sm font-semibold", mono && "font-mono")}>
        {value}
      </p>
    </div>
  );
}

function ScopeEvidence({
  plan,
  packs,
}: {
  plan: KnowledgeRetrievalPlan | null;
  packs: Record<string, KnowledgeEvidencePack>;
}) {
  if (!plan) return null;
  const packsByScope = new Map(
    Object.values(packs).map((pack) => [pack.scope_id, pack]),
  );
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">Scope 与 Evidence Pack</h3>
        <Badge variant="outline">outline v{plan.outline_version}</Badge>
        <span className="font-mono text-[11px] text-muted-foreground">
          {plan.retrieval_plan_id}
        </span>
      </div>
      {plan.scopes.map((scope) => {
        const pack = packsByScope.get(scope.scope_id);
        return (
          <article key={scope.scope_id} className="rounded-xl border p-4">
            <div className="flex flex-wrap items-start gap-2">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold">{scope.title}</p>
                <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                  {scope.scope_type} · {scope.scope_id}
                </p>
              </div>
              <Badge
                variant={
                  pack?.sufficiency === "sufficient"
                    ? "default"
                    : pack?.sufficiency === "missing"
                      ? "destructive"
                      : "outline"
                }
              >
                {pack
                  ? `${pack.sufficiency} · ${pack.hits.length} chunks`
                  : "等待研究"}
              </Badge>
            </div>
            {pack?.gap_reasons.length ? (
              <p className="mt-3 text-sm text-muted-foreground">
                {pack.gap_reasons.join("；")}
              </p>
            ) : null}
            {pack?.hits.length ? (
              <div className="mt-3 grid gap-2 xl:grid-cols-2">
                {pack.hits.map((hit) => (
                  <div
                    key={hit.chunk_id}
                    className="min-w-0 rounded-lg border bg-muted/10 p-3"
                  >
                    <div className="flex items-center gap-2">
                      <Badge variant="secondary">
                        {(hit.score * 100).toFixed(1)}
                      </Badge>
                      <span className="truncate text-xs font-medium">
                        {hit.provenance?.display_name ?? hit.chunk_id}
                      </span>
                    </div>
                    <p className="mt-2 line-clamp-4 whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
                      {hit.text}
                    </p>
                    <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{hit.provenance?.trust_tier ?? "来源待标记"}</span>
                      {hit.provenance?.canonical_url ? (
                        <a
                          href={hit.provenance.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                          className="ml-auto inline-flex min-h-8 items-center gap-1 hover:text-foreground"
                        >
                          查看来源
                          <ExternalLink className="size-3" />
                        </a>
                      ) : (
                        <span className="ml-auto">私有来源</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
