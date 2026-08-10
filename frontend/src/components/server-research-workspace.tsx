"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  CircleDot,
  ExternalLink,
  FileSearch,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

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
import { Label } from "@/components/ui/label";
import { apiGet } from "@/lib/api";
import {
  createTaskResearchPlan,
  getEvidencePack,
  getResearchRun,
  listResearchPlans,
  listResearchRuns,
  researchRunEventsUrl,
  resumeResearchRun,
  startResearchRun,
} from "@/lib/research-api";
import {
  formatResearchDate,
  newResearchRequestId,
  researchStatusLabel,
  researchStatusVariant,
  TERMINAL_RESEARCH_STATUSES,
} from "@/lib/research-view-model";
import type {
  AccessibleProject,
  KnowledgeEvidencePack,
  KnowledgeRetrievalPlan,
  ResearchRun,
  ResearchRunDetail,
  TaskRecord,
} from "@/types";

type StreamState = "idle" | "connected" | "polling" | "done";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "资料研究操作失败，请重试。";
}

function canPublishKnowledge(
  role: AccessibleProject["effective_role"] | null,
) {
  return role === "org_admin" || role === "team_lead" || role === "editor";
}

function taskLabel(task: TaskRecord) {
  return `topic_${String(task.topic_index).padStart(3, "0")} · ${task.topic}`;
}

export function ServerResearchWorkspace({
  customer,
  embedded = false,
  taskId = "",
}: {
  customer: string;
  embedded?: boolean;
  taskId?: string;
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryString = searchParams.toString();
  const [plans, setPlans] = useState<KnowledgeRetrievalPlan[]>([]);
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [role, setRole] = useState<AccessibleProject["effective_role"] | null>(
    null,
  );
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState(taskId);
  const [selectedThreadId, setSelectedThreadId] = useState(
    searchParams.get("thread") || "",
  );
  const [detail, setDetail] = useState<ResearchRunDetail | null>(null);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>(
    [],
  );
  const [evidencePack, setEvidencePack] =
    useState<KnowledgeEvidencePack | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [planPending, setPlanPending] = useState(false);
  const [startPending, setStartPending] = useState(false);
  const [resumePending, setResumePending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const cursors = useRef<Record<string, number>>({});
  const startRequestId = useRef("");
  const resumeRequestId = useRef("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const projectPath = encodeURIComponent(customer);
      const [nextPlans, nextRuns, nextTasks, projects] = await Promise.all([
        listResearchPlans(customer),
        listResearchRuns(customer),
        apiGet<TaskRecord[]>(`/api/projects/${projectPath}/tasks`),
        apiGet<AccessibleProject[]>("/api/projects"),
      ]);
      const scopedTask = taskId
        ? nextTasks.find((task) => task.id === taskId)
        : null;
      const scopedArticleId = scopedTask
        ? `topic_${String(scopedTask.topic_index).padStart(3, "0")}`
        : "";
      const visiblePlans = scopedArticleId
        ? nextPlans.filter((plan) => plan.article_id === scopedArticleId)
        : nextPlans;
      const visibleRuns = scopedArticleId
        ? nextRuns.filter((run) => run.article_id === scopedArticleId)
        : nextRuns;
      setPlans(visiblePlans);
      setRuns(visibleRuns);
      setTasks(nextTasks);
      setRole(
        projects.find((project) => project.project_id === customer)
          ?.effective_role ?? null,
      );
      setSelectedPlanId((current) =>
        visiblePlans.some((plan) => plan.retrieval_plan_id === current)
          ? current
          : visiblePlans[0]?.retrieval_plan_id || "",
      );
      setSelectedTaskId((current) => taskId || current || nextTasks[0]?.id || "");
      setSelectedThreadId((current) => {
        if (current && visibleRuns.some((run) => run.thread_id === current)) {
          return current;
        }
        return visibleRuns[0]?.thread_id || "";
      });
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [customer, taskId]);

  const scopedArticleId = useMemo(() => {
    const scopedTask = taskId
      ? tasks.find((task) => task.id === taskId)
      : null;
    return scopedTask
      ? `topic_${String(scopedTask.topic_index).padStart(3, "0")}`
      : "";
  }, [taskId, tasks]);

  const refreshRuns = useCallback(async () => {
    const nextRuns = await listResearchRuns(customer);
    setRuns(
      scopedArticleId
        ? nextRuns.filter((run) => run.article_id === scopedArticleId)
        : nextRuns,
    );
  }, [customer, scopedArticleId]);

  const refreshDetail = useCallback(
    async (threadId: string, showSpinner = false) => {
      if (!threadId) {
        setDetail(null);
        return;
      }
      if (showSpinner) setDetailLoading(true);
      try {
        const next = await getResearchRun(customer, threadId);
        setDetail(next);
      } catch (reason) {
        setError(errorMessage(reason));
      } finally {
        if (showSpinner) setDetailLoading(false);
      }
    },
    [customer],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setSelectedCandidateIds([]);
    resumeRequestId.current = "";
    setEvidencePack(null);
    void refreshDetail(selectedThreadId, true);
  }, [refreshDetail, selectedThreadId]);

  useEffect(() => {
    if (embedded) return;
    if (!selectedThreadId) return;
    const next = new URLSearchParams(queryString);
    next.set("tab", "research");
    next.set("thread", selectedThreadId);
    const nextQueryString = next.toString();
    if (nextQueryString !== queryString) {
      router.replace(`?${nextQueryString}`, { scroll: false });
    }
  }, [embedded, queryString, router, selectedThreadId]);

  const detailStatus = detail?.status;

  useEffect(() => {
    if (
      !selectedThreadId ||
      (detailStatus && TERMINAL_RESEARCH_STATUSES.has(detailStatus))
    ) {
      setStreamState(
        detailStatus && TERMINAL_RESEARCH_STATUSES.has(detailStatus)
          ? "done"
          : "idle",
      );
      return;
    }

    let active = true;
    let pollTimer: number | undefined;
    const cursor = cursors.current[selectedThreadId] || 0;
    const source = new EventSource(
      researchRunEventsUrl(customer, selectedThreadId, cursor),
      { withCredentials: true },
    );

    const refresh = (event?: Event) => {
      if (!active) return;
      if (event instanceof MessageEvent && event.lastEventId) {
        const sequence = Number(event.lastEventId);
        if (Number.isFinite(sequence)) {
          cursors.current[selectedThreadId] = sequence;
        }
      }
      void Promise.all([
        refreshDetail(selectedThreadId),
        refreshRuns(),
      ]);
    };

    source.onopen = () => {
      if (active) setStreamState("connected");
    };
    source.addEventListener("research_event", refresh);
    source.addEventListener("run_state", refresh);
    source.addEventListener("done", (event) => {
      refresh(event);
      if (active) setStreamState("done");
      source.close();
    });
    source.onerror = () => {
      source.close();
      if (!active) return;
      setStreamState("polling");
      pollTimer = window.setInterval(() => {
        void Promise.all([
          refreshDetail(selectedThreadId),
          refreshRuns(),
        ]);
      }, 3_000);
    };

    return () => {
      active = false;
      source.close();
      if (pollTimer !== undefined) window.clearInterval(pollTimer);
    };
  }, [
    customer,
    detailStatus,
    refreshDetail,
    refreshRuns,
    selectedThreadId,
  ]);

  const eligibleTasks = useMemo(
    () =>
      tasks.filter(
        (task) =>
          Boolean(task.outline?.trim()) &&
          task.article_versions?.some(
            (version) =>
              version.kind === "outline" &&
              version.source_kind === "manual_confirmed",
          ),
      ),
    [tasks],
  );
  const canRun = canPublishKnowledge(role);

  async function generatePlan() {
    if (!selectedTaskId) return;
    setPlanPending(true);
    setError("");
    setMessage("");
    try {
      const plan = await createTaskResearchPlan(customer, selectedTaskId);
      const nextPlans = await listResearchPlans(customer);
      setPlans(
        taskId
          ? nextPlans.filter((item) => item.article_id === plan.article_id)
          : nextPlans,
      );
      startRequestId.current = "";
      setSelectedPlanId(plan.retrieval_plan_id);
      setMessage("已从 PostgreSQL 中的已确认大纲生成不可变 Retrieval Plan。");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPlanPending(false);
    }
  }

  async function startRun() {
    if (!selectedPlanId) return;
    setStartPending(true);
    setError("");
    setMessage("");
    try {
      const queued = await startResearchRun(customer, {
        request_id:
          startRequestId.current ||
          (startRequestId.current = newResearchRequestId("start")),
        retrieval_plan_id: selectedPlanId,
        max_discovery_queries: 2,
      });
      startRequestId.current = "";
      setMessage("Research Run 已进入 PostgreSQL 队列。");
      await refreshRuns();
      setSelectedThreadId(queued.run.thread_id);
      await refreshDetail(queued.run.thread_id);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setStartPending(false);
    }
  }

  async function resumeRun() {
    if (!detail) return;
    setResumePending(true);
    setError("");
    setMessage("");
    try {
      await resumeResearchRun(customer, detail.thread_id, {
        request_id:
          resumeRequestId.current ||
          (resumeRequestId.current = newResearchRequestId("resume")),
        approved_candidate_ids: selectedCandidateIds,
      });
      resumeRequestId.current = "";
      setMessage(
        selectedCandidateIds.length
          ? `已批准 ${selectedCandidateIds.length} 个候选并继续。`
          : "已明确不采纳本轮候选并继续。",
      );
      setSelectedCandidateIds([]);
      await Promise.all([
        refreshDetail(detail.thread_id),
        refreshRuns(),
      ]);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setResumePending(false);
    }
  }

  async function openEvidencePack(evidencePackId: string) {
    setError("");
    try {
      setEvidencePack(await getEvidencePack(customer, evidencePackId));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  return (
    <main
      className={
        embedded
          ? "grid w-full gap-4"
          : "mx-auto grid w-full max-w-[1480px] gap-5 px-5 pb-8"
      }
    >
      <div className="flex flex-col gap-4 rounded-2xl border bg-card p-5 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
            <FileSearch className="size-4" />
            Server Research
          </div>
          <h1 className={embedded ? "text-lg font-semibold" : "text-2xl font-semibold"}>
            {embedded ? "大纲资料研究" : "项目资料研究"}
          </h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            {embedded
              ? "确认大纲后运行研究；完成的 Evidence Pack 会固定到后续正文生成任务。"
              : "从已确认文章大纲生成检索计划，运行项目隔离的资料检索，并只批准服务端发现的官网候选。Organization、Task、Plan、Run 与 Job 身份全部由服务端校验。"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="min-h-7 gap-1">
            <CircleDot className="size-3" />
            {streamState === "connected"
              ? "实时连接"
              : streamState === "polling"
                ? "轮询恢复"
                : streamState === "done"
                  ? "运行终态"
                  : "等待运行"}
          </Badge>
          <Button
            type="button"
            variant="outline"
            className="min-h-11"
            disabled={loading}
            onClick={() => void load()}
          >
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            刷新
          </Button>
        </div>
      </div>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>资料研究操作失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      {message ? (
        <Alert>
          <CheckCircle2 />
          <AlertTitle>服务端状态已更新</AlertTitle>
          <AlertDescription aria-live="polite">{message}</AlertDescription>
        </Alert>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>1. 从文章大纲生成 Plan</CardTitle>
            <CardDescription>
              只发送 Task ID；Scope、Outline Hash 和版本由服务端生成并锁定。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            {taskId ? (
              <p className="rounded-lg border bg-muted/20 px-3 py-2 text-sm">
                {eligibleTasks.find((task) => task.id === taskId)
                  ? taskLabel(eligibleTasks.find((task) => task.id === taskId)!)
                  : "请先确认当前文章大纲"}
              </p>
            ) : (
              <>
                <Label htmlFor="research-task">已确认大纲的文章任务</Label>
                <select
                  id="research-task"
                  className="min-h-11 w-full rounded-md border bg-background px-3 text-sm"
                  value={selectedTaskId}
                  onChange={(event) => setSelectedTaskId(event.target.value)}
                  disabled={!canRun || planPending}
                >
                  <option value="">选择文章任务</option>
                  {eligibleTasks.map((task) => (
                    <option key={task.id} value={task.id}>
                      {taskLabel(task)}
                    </option>
                  ))}
                </select>
              </>
            )}
            <Button
              type="button"
              className="min-h-11 justify-self-start"
              disabled={!canRun || !selectedTaskId || planPending}
              onClick={() => void generatePlan()}
            >
              {planPending ? <Loader2 className="animate-spin" /> : <ShieldCheck />}
              生成不可变 Plan
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>2. 启动 Research Run</CardTitle>
            <CardDescription>
              Run 使用真实 Task 进入 PostgreSQL Job Queue；不会启动 Local SQLite Worker。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <Label htmlFor="research-plan">Retrieval Plan</Label>
            <select
              id="research-plan"
              className="min-h-11 w-full rounded-md border bg-background px-3 text-sm"
              value={selectedPlanId}
              onChange={(event) => {
                startRequestId.current = "";
                setSelectedPlanId(event.target.value);
              }}
              disabled={!canRun || startPending}
            >
              <option value="">选择 Retrieval Plan</option>
              {plans.map((plan) => (
                <option
                  key={plan.retrieval_plan_id}
                  value={plan.retrieval_plan_id}
                >
                  {plan.article_id} · 大纲 v{plan.outline_version} ·{" "}
                  {plan.scopes.length} scopes
                </option>
              ))}
            </select>
            <Button
              type="button"
              className="min-h-11 justify-self-start"
              disabled={!canRun || !selectedPlanId || startPending}
              onClick={() => void startRun()}
            >
              {startPending ? <Loader2 className="animate-spin" /> : <Play />}
              启动资料研究
            </Button>
            {!canRun && !loading ? (
              <p className="text-sm text-muted-foreground">
                当前角色可以查看 Run，但不能启动或批准会自动发布的候选。
              </p>
            ) : null}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
        <Card className="h-fit">
          <CardHeader>
            <CardTitle>最近 Runs</CardTitle>
            <CardDescription>选择后可通过 URL 恢复到同一个 Thread。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-2">
            {runs.length ? (
              runs.map((run) => (
                <button
                  key={run.thread_id}
                  type="button"
                  className={`min-h-11 rounded-lg border p-3 text-left transition-colors hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    selectedThreadId === run.thread_id ? "bg-muted" : ""
                  }`}
                  onClick={() => setSelectedThreadId(run.thread_id)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{run.article_id}</span>
                    <Badge variant={researchStatusVariant(run.status)}>
                      {researchStatusLabel(run.status)}
                    </Badge>
                  </div>
                  <div className="mt-2 text-xs text-muted-foreground">
                    大纲 v{run.outline_version} · {formatResearchDate(run.updated_at)}
                  </div>
                </button>
              ))
            ) : (
              <p className="text-sm text-muted-foreground">
                还没有 Research Run。先生成 Plan，再启动第一条 Run。
              </p>
            )}
          </CardContent>
        </Card>

        <div className="grid min-w-0 gap-4">
          <Card>
            <CardHeader className="flex-row items-start justify-between gap-3">
              <div>
                <CardTitle>Run 详情</CardTitle>
                <CardDescription>
                  {detail?.thread_id || "选择左侧 Run 查看执行状态。"}
                </CardDescription>
              </div>
              {detailLoading ? <Loader2 className="animate-spin" /> : null}
            </CardHeader>
            {detail ? (
              <CardContent className="grid gap-4">
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  <div className="rounded-lg border p-3">
                    <div className="text-xs text-muted-foreground">状态</div>
                    <div className="mt-1 font-medium">
                      {researchStatusLabel(detail.status)}
                    </div>
                  </div>
                  <div className="rounded-lg border p-3">
                    <div className="text-xs text-muted-foreground">当前节点</div>
                    <div className="mt-1 break-all font-medium">
                      {detail.current_node}
                    </div>
                  </div>
                  <div className="rounded-lg border p-3">
                    <div className="text-xs text-muted-foreground">Gap Round</div>
                    <div className="mt-1 font-medium">
                      {detail.gap_fill_round}/{detail.max_gap_fill_rounds}
                    </div>
                  </div>
                  <div className="rounded-lg border p-3">
                    <div className="text-xs text-muted-foreground">Discovery</div>
                    <div className="mt-1 font-medium">
                      {detail.discovery_queries_used}/
                      {detail.max_discovery_queries}
                    </div>
                  </div>
                </div>

                {detail.warnings.length ? (
                  <Alert>
                    <AlertCircle />
                    <AlertTitle>运行提醒</AlertTitle>
                    <AlertDescription>
                      {detail.warnings.join("；")}
                    </AlertDescription>
                  </Alert>
                ) : null}
                {detail.error_message ? (
                  <Alert variant="destructive">
                    <AlertCircle />
                    <AlertTitle>{detail.error_code || "运行失败"}</AlertTitle>
                    <AlertDescription>{detail.error_message}</AlertDescription>
                  </Alert>
                ) : null}

                {detail.status === "waiting_for_review" ? (
                  <div className="grid gap-3 rounded-xl border p-4">
                    <div>
                      <h3 className="font-semibold">人工候选复核</h3>
                      <p className="mt-1 text-sm text-muted-foreground">
                        只能选择当前 Thread 中由服务端发现的候选，不能手工输入 URL。
                      </p>
                    </div>
                    {detail.review_candidates.map((candidate) => {
                      const checked = selectedCandidateIds.includes(
                        candidate.candidate_id,
                      );
                      return (
                        <label
                          key={candidate.candidate_id}
                          className="flex min-h-11 cursor-pointer gap-3 rounded-lg border p-3"
                        >
                          <input
                            type="checkbox"
                            className="mt-1 size-4"
                            checked={checked}
                            disabled={!canRun || resumePending}
                            onChange={(event) => {
                              resumeRequestId.current = "";
                              setSelectedCandidateIds((current) =>
                                event.target.checked
                                  ? [...current, candidate.candidate_id]
                                  : current.filter(
                                      (id) => id !== candidate.candidate_id,
                                    ),
                              );
                            }}
                          />
                          <span className="min-w-0">
                            <span className="block break-all text-sm font-medium">
                              {candidate.url}
                            </span>
                            <span className="mt-1 block text-xs text-muted-foreground">
                              {candidate.page_type} ·{" "}
                              {candidate.evidence.channel || "official discovery"}
                              {candidate.evidence.score !== null
                                ? ` · score ${candidate.evidence.score.toFixed(3)}`
                                : ""}
                            </span>
                          </span>
                        </label>
                      );
                    })}
                    <Button
                      type="button"
                      className="min-h-11 justify-self-start"
                      disabled={!canRun || resumePending}
                      onClick={() => void resumeRun()}
                    >
                      {resumePending ? (
                        <Loader2 className="animate-spin" />
                      ) : (
                        <RotateCcw />
                      )}
                      {selectedCandidateIds.length
                        ? `批准 ${selectedCandidateIds.length} 个并继续`
                        : "不采纳候选并继续"}
                    </Button>
                  </div>
                ) : null}
              </CardContent>
            ) : null}
          </Card>

          {detail ? (
            <section className="grid gap-4 xl:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Evidence Packs</CardTitle>
                  <CardDescription>
                    每个 Pack 固定到 Plan、Scope、Outline 和 Chunk 身份。
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-2">
                  {detail.evidence_pack_ids.length ? (
                    detail.evidence_pack_ids.map((id) => (
                      <Button
                        key={id}
                        type="button"
                        variant="outline"
                        className="min-h-11 justify-start overflow-hidden"
                        onClick={() => void openEvidencePack(id)}
                      >
                        <FileSearch />
                        <span className="truncate">{id}</span>
                      </Button>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      当前 Run 尚未生成 Evidence Pack。
                    </p>
                  )}
                  {evidencePack ? (
                    <div className="mt-2 rounded-lg border p-3 text-sm">
                      <div className="font-medium">
                        {evidencePack.scope_type} · {evidencePack.sufficiency}
                      </div>
                      <div className="mt-1 text-muted-foreground">
                        {evidencePack.hits.length} chunks ·{" "}
                        {evidencePack.public_citation_urls.length} citations
                      </div>
                      {evidencePack.public_citation_urls.map((url) => (
                        <a
                          key={url}
                          href={url}
                          target="_blank"
                          rel="noreferrer"
                          className="mt-2 flex min-h-11 items-center gap-2 break-all text-primary underline-offset-4 hover:underline"
                        >
                          <ExternalLink className="size-4 shrink-0" />
                          {url}
                        </a>
                      ))}
                      <div className="mt-3 grid gap-2">
                        {evidencePack.hits.map((hit, index) => (
                          <div
                            key={hit.chunk_id}
                            className="rounded-lg border bg-muted/20 p-3"
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span className="font-medium">
                                #{index + 1} {hit.heading_path.join(" / ") || "未命名知识块"}
                              </span>
                              <Badge variant="outline">
                                score {hit.score.toFixed(3)}
                              </Badge>
                            </div>
                            <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted-foreground">
                              {hit.text}
                            </p>
                            {hit.provenance?.canonical_url ? (
                              <a
                                href={hit.provenance.canonical_url}
                                target="_blank"
                                rel="noreferrer"
                                className="mt-2 inline-flex min-h-11 items-center gap-2 break-all text-primary underline-offset-4 hover:underline"
                              >
                                <ExternalLink className="size-4 shrink-0" />
                                {hit.provenance.display_name}
                              </a>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>执行时间线</CardTitle>
                  <CardDescription>
                    Server 响应只保留白名单事件字段，不返回私有 Job Request。
                  </CardDescription>
                </CardHeader>
                <CardContent className="grid gap-2">
                  {detail.events.length ? (
                    detail.events.map((event) => (
                      <div
                        key={event.sequence}
                        className="rounded-lg border p-3 text-sm"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium">
                            #{event.sequence} · {event.event_type}
                          </span>
                          <span className="text-xs text-muted-foreground">
                            {formatResearchDate(event.created_at)}
                          </span>
                        </div>
                        <div className="mt-1 text-muted-foreground">
                          {event.node_name}
                          {event.scope_id ? ` · ${event.scope_id}` : ""}
                        </div>
                      </div>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      暂无时间线事件。
                    </p>
                  )}
                </CardContent>
              </Card>
            </section>
          ) : null}
        </div>
      </section>
    </main>
  );
}
