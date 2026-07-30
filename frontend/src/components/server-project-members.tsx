"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  UserPlus,
  UsersRound,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

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
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { apiDelete, apiGet, apiPut } from "@/lib/api";
import type {
  ProjectMembership,
  ProjectMembershipCandidate,
  ProjectMembershipCandidatePage,
  ProjectMembershipMutation,
  ProjectMembershipPage,
  ProjectMembershipRevocation,
  ProjectMembershipRole,
} from "@/types";

type ServerProjectMembersProps = {
  projectId: string;
};

type Feedback = {
  kind: "success" | "error";
  message: string;
} | null;

const ROLE_OPTIONS: Array<{
  value: ProjectMembershipRole;
  label: string;
  description: string;
}> = [
  {
    value: "editor",
    label: "编辑",
    description: "编辑知识与文章，并完成交付。",
  },
  {
    value: "reviewer",
    label: "复核员",
    description: "查看项目并执行质量复核。",
  },
  {
    value: "viewer",
    label: "只读成员",
    description: "只查看项目内容。",
  },
];

const ROLE_LABELS: Record<ProjectMembershipRole, string> = {
  editor: "编辑",
  reviewer: "复核员",
  viewer: "只读成员",
};

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function mergeByUserId<T extends { user_id: string }>(
  current: T[],
  next: T[],
): T[] {
  const merged = new Map(current.map((item) => [item.user_id, item]));
  next.forEach((item) => merged.set(item.user_id, item));
  return Array.from(merged.values()).sort((a, b) =>
    a.user_id.localeCompare(b.user_id),
  );
}

function FeedbackAlert({
  feedback,
  successTitle,
}: {
  feedback: Feedback;
  successTitle: string;
}) {
  if (!feedback) return null;
  const failed = feedback.kind === "error";
  return (
    <Alert variant={failed ? "destructive" : "default"}>
      {failed ? <AlertCircle /> : <CheckCircle2 />}
      <AlertTitle>{failed ? "操作失败" : successTitle}</AlertTitle>
      <AlertDescription>{feedback.message}</AlertDescription>
    </Alert>
  );
}

export function ServerProjectMembers({
  projectId,
}: ServerProjectMembersProps) {
  const encodedProject = encodeURIComponent(projectId);
  const [members, setMembers] = useState<ProjectMembership[]>([]);
  const [memberCursor, setMemberCursor] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<
    ProjectMembershipCandidate[]
  >([]);
  const [candidateCursor, setCandidateCursor] = useState<string | null>(null);
  const [memberRoles, setMemberRoles] = useState<
    Record<string, ProjectMembershipRole>
  >({});
  const [candidateRoles, setCandidateRoles] = useState<
    Record<string, ProjectMembershipRole>
  >({});
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState<
    "members" | "candidates" | null
  >(null);
  const [pendingUserId, setPendingUserId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [memberFeedback, setMemberFeedback] = useState<Feedback>(null);
  const [candidateFeedback, setCandidateFeedback] =
    useState<Feedback>(null);
  const [revokeTarget, setRevokeTarget] =
    useState<ProjectMembership | null>(null);

  const applyPages = useCallback(
    (
      memberPage: ProjectMembershipPage,
      candidatePage: ProjectMembershipCandidatePage,
    ) => {
      setMembers(memberPage.items);
      setMemberCursor(memberPage.next_after_user_id);
      setCandidates(candidatePage.items);
      setCandidateCursor(candidatePage.next_after_user_id);
      setMemberRoles(
        Object.fromEntries(
          memberPage.items.map((member) => [member.user_id, member.role]),
        ),
      );
      setCandidateRoles((current) =>
        Object.fromEntries(
          candidatePage.items.map((candidate) => [
            candidate.user_id,
            current[candidate.user_id] ?? "viewer",
          ]),
        ),
      );
    },
    [],
  );

  const loadLists = useCallback(
    async (showInitialLoader: boolean) => {
      if (showInitialLoader) setLoading(true);
      setLoadError("");
      try {
        const [memberPage, candidatePage] = await Promise.all([
          apiGet<ProjectMembershipPage>(
            `/api/projects/${encodedProject}/members?limit=50`,
          ),
          apiGet<ProjectMembershipCandidatePage>(
            `/api/projects/${encodedProject}/members/candidates?limit=50`,
          ),
        ]);
        applyPages(memberPage, candidatePage);
      } catch (error) {
        setLoadError(
          errorMessage(error, "项目成员与候选用户加载失败，请重试。"),
        );
      } finally {
        if (showInitialLoader) setLoading(false);
      }
    },
    [applyPages, encodedProject],
  );

  useEffect(() => {
    void loadLists(true);
  }, [loadLists]);

  async function loadMoreMembers() {
    if (!memberCursor) return;
    setLoadingMore("members");
    setMemberFeedback(null);
    try {
      const page = await apiGet<ProjectMembershipPage>(
        `/api/projects/${encodedProject}/members?limit=50&after_user_id=${encodeURIComponent(memberCursor)}`,
      );
      setMembers((current) => mergeByUserId(current, page.items));
      setMemberRoles((current) => ({
        ...current,
        ...Object.fromEntries(
          page.items.map((member) => [member.user_id, member.role]),
        ),
      }));
      setMemberCursor(page.next_after_user_id);
    } catch (error) {
      setMemberFeedback({
        kind: "error",
        message: errorMessage(error, "更多项目成员加载失败。"),
      });
    } finally {
      setLoadingMore(null);
    }
  }

  async function loadMoreCandidates() {
    if (!candidateCursor) return;
    setLoadingMore("candidates");
    setCandidateFeedback(null);
    try {
      const page = await apiGet<ProjectMembershipCandidatePage>(
        `/api/projects/${encodedProject}/members/candidates?limit=50&after_user_id=${encodeURIComponent(candidateCursor)}`,
      );
      setCandidates((current) => mergeByUserId(current, page.items));
      setCandidateRoles((current) => ({
        ...current,
        ...Object.fromEntries(
          page.items.map((candidate) => [
            candidate.user_id,
            current[candidate.user_id] ?? "viewer",
          ]),
        ),
      }));
      setCandidateCursor(page.next_after_user_id);
    } catch (error) {
      setCandidateFeedback({
        kind: "error",
        message: errorMessage(error, "更多候选用户加载失败。"),
      });
    } finally {
      setLoadingMore(null);
    }
  }

  async function grantMembership(candidate: ProjectMembershipCandidate) {
    const role = candidateRoles[candidate.user_id] ?? "viewer";
    setPendingUserId(candidate.user_id);
    setCandidateFeedback(null);
    try {
      await apiPut<ProjectMembershipMutation>(
        `/api/projects/${encodedProject}/members/${encodeURIComponent(candidate.user_id)}`,
        { role },
      );
      await loadLists(false);
      setCandidateFeedback({
        kind: "success",
        message: `已将 ${candidate.display_name} 添加为${ROLE_LABELS[role]}。`,
      });
    } catch (error) {
      setCandidateFeedback({
        kind: "error",
        message: errorMessage(error, "添加项目成员失败。"),
      });
    } finally {
      setPendingUserId(null);
    }
  }

  async function updateMembership(member: ProjectMembership) {
    const role = memberRoles[member.user_id] ?? member.role;
    setPendingUserId(member.user_id);
    setMemberFeedback(null);
    try {
      await apiPut<ProjectMembershipMutation>(
        `/api/projects/${encodedProject}/members/${encodeURIComponent(member.user_id)}`,
        { role },
      );
      await loadLists(false);
      setMemberFeedback({
        kind: "success",
        message: `已将 ${member.display_name} 的角色更新为${ROLE_LABELS[role]}。`,
      });
    } catch (error) {
      setMemberFeedback({
        kind: "error",
        message: errorMessage(error, "更新项目角色失败。"),
      });
    } finally {
      setPendingUserId(null);
    }
  }

  async function revokeMembership() {
    if (!revokeTarget) return;
    const target = revokeTarget;
    setPendingUserId(target.user_id);
    setMemberFeedback(null);
    try {
      const result = await apiDelete<ProjectMembershipRevocation>(
        `/api/projects/${encodedProject}/members/${encodeURIComponent(target.user_id)}`,
      );
      setRevokeTarget(null);
      await loadLists(false);
      setMemberFeedback({
        kind: "success",
        message: result.revoked
          ? `已撤销 ${target.display_name} 的显式项目角色。`
          : "该成员关系已经不存在，列表已刷新。",
      });
    } catch (error) {
      setMemberFeedback({
        kind: "error",
        message: errorMessage(error, "撤销项目成员失败。"),
      });
    } finally {
      setPendingUserId(null);
    }
  }

  return (
    <main className="min-h-dvh bg-background text-foreground">
      <div className="border-b bg-card">
        <div className="mx-auto grid max-w-5xl gap-3 px-5 py-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                  <ShieldCheck className="size-4" />
                </span>
                <h1 className="text-xl font-semibold">项目成员</h1>
              </div>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
                管理本项目的显式编辑、复核与只读角色。组织管理员和当前负责团队的
                Team Lead 由上级权限事实决定，不会重复出现在成员表中。
              </p>
            </div>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              onClick={() => void loadLists(true)}
              disabled={loading || pendingUserId !== null}
            >
              {loading ? (
                <Loader2 className="animate-spin" />
              ) : (
                <RefreshCw />
              )}
              刷新
            </Button>
          </div>
          <div
            className="truncate font-mono text-xs text-muted-foreground"
            title={projectId}
          >
            {projectId}
          </div>
        </div>
      </div>

      <div className="mx-auto grid max-w-5xl gap-4 px-5 py-5">
        {loadError && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>成员数据加载失败</AlertTitle>
            <AlertDescription>{loadError}</AlertDescription>
            <div className="col-start-2 mt-2">
              <Button
                type="button"
                variant="outline"
                className="min-h-11"
                onClick={() => void loadLists(true)}
              >
                <RefreshCw />
                重试
              </Button>
            </div>
          </Alert>
        )}

        {loading && !members.length && !candidates.length ? (
          <div
            className="grid gap-4"
            role="status"
            aria-live="polite"
          >
            <span className="sr-only">正在读取项目成员…</span>
            {[0, 1].map((card) => (
              <Card key={card} className="rounded-xl">
                <CardHeader className="border-b">
                  <Skeleton className="h-5 w-36" />
                  <Skeleton className="h-4 w-full max-w-md" />
                </CardHeader>
                <CardContent className="grid gap-3 pt-4">
                  {[0, 1].map((row) => (
                    <div
                      key={row}
                      className="grid gap-3 rounded-lg border p-3 md:grid-cols-[minmax(0,1fr)_180px_auto]"
                    >
                      <div className="grid gap-2">
                        <Skeleton className="h-4 w-32" />
                        <Skeleton className="h-3 w-48 max-w-full" />
                      </div>
                      <Skeleton className="h-11 w-full" />
                      <Skeleton className="h-11 w-28 max-w-full" />
                    </div>
                  ))}
                </CardContent>
              </Card>
            ))}
          </div>
        ) : !loadError || members.length || candidates.length ? (
          <>
            <Card className="rounded-xl">
              <CardHeader className="border-b">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      <UsersRound className="size-4 text-primary" />
                      显式项目成员
                    </CardTitle>
                    <CardDescription className="mt-1">
                      Disabled 账号仍会保留在这里，方便撤销旧成员关系。
                    </CardDescription>
                  </div>
                  <Badge variant="outline">{members.length} 位已加载</Badge>
                </div>
              </CardHeader>
              <CardContent className="grid gap-3 pt-4">
                <FeedbackAlert
                  feedback={memberFeedback}
                  successTitle="成员已更新"
                />
                {members.length ? (
                  <div className="grid gap-3">
                    {members.map((member) => {
                      const selectedRole =
                        memberRoles[member.user_id] ?? member.role;
                      const pending = pendingUserId === member.user_id;
                      const disabled = member.status === "disabled";
                      const roleChanged = selectedRole !== member.role;
                      const selectId = `member-role-${encodeURIComponent(member.user_id)}`;
                      return (
                        <div
                          key={member.user_id}
                          className="grid gap-3 rounded-lg border bg-background p-3 md:grid-cols-[minmax(0,1fr)_180px_auto] md:items-end"
                        >
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium">
                                {member.display_name}
                              </span>
                              <Badge
                                variant={disabled ? "secondary" : "outline"}
                              >
                                {disabled ? "账号已停用" : "Active"}
                              </Badge>
                            </div>
                            <div
                              className="mt-1 truncate font-mono text-xs text-muted-foreground"
                              title={member.user_id}
                            >
                              {member.user_id}
                            </div>
                          </div>
                          <div className="grid gap-1.5">
                            <Label htmlFor={selectId}>项目角色</Label>
                            <select
                              id={selectId}
                              className="h-11 w-full cursor-pointer rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
                              value={selectedRole}
                              disabled={disabled || pending}
                              onChange={(event) =>
                                setMemberRoles((current) => ({
                                  ...current,
                                  [member.user_id]: event.target
                                    .value as ProjectMembershipRole,
                                }))
                              }
                            >
                              {ROLE_OPTIONS.map((option) => (
                                <option
                                  key={option.value}
                                  value={option.value}
                                >
                                  {option.label}
                                </option>
                              ))}
                            </select>
                          </div>
                          <div className="flex flex-wrap gap-2">
                            <Button
                              type="button"
                              className="min-h-11 flex-1 md:flex-none"
                              onClick={() => void updateMembership(member)}
                              disabled={
                                disabled ||
                                !roleChanged ||
                                pendingUserId !== null
                              }
                            >
                              {pending ? (
                                <Loader2 className="animate-spin" />
                              ) : (
                                <Save />
                              )}
                              保存角色
                            </Button>
                            <Button
                              type="button"
                              variant="destructive"
                              className="min-h-11 flex-1 md:flex-none"
                              onClick={() => setRevokeTarget(member)}
                              disabled={pendingUserId !== null}
                            >
                              <Trash2 />
                              撤销
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="rounded-lg border border-dashed px-4 py-8 text-center">
                    <p className="font-medium">暂无显式项目成员</p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      组织管理员与 Team Lead 的继承权限不会显示在这里。
                    </p>
                  </div>
                )}
                {memberCursor && (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11 justify-self-start"
                    onClick={() => void loadMoreMembers()}
                    disabled={loadingMore !== null || pendingUserId !== null}
                  >
                    {loadingMore === "members" && (
                      <Loader2 className="animate-spin" />
                    )}
                    加载更多成员
                  </Button>
                )}
              </CardContent>
            </Card>

            <Card className="rounded-xl">
              <CardHeader className="border-b">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      <UserPlus className="size-4 text-primary" />
                      添加项目成员
                    </CardTitle>
                    <CardDescription className="mt-1">
                      这里只显示尚未拥有本项目访问权的 Active 同组织用户。
                    </CardDescription>
                  </div>
                  <Badge variant="outline">
                    {candidates.length} 位候选已加载
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="grid gap-3 pt-4">
                <FeedbackAlert
                  feedback={candidateFeedback}
                  successTitle="成员已添加"
                />
                {candidates.length ? (
                  <div className="grid gap-3">
                    {candidates.map((candidate) => {
                      const selectedRole =
                        candidateRoles[candidate.user_id] ?? "viewer";
                      const pending = pendingUserId === candidate.user_id;
                      const selectId = `candidate-role-${encodeURIComponent(candidate.user_id)}`;
                      return (
                        <div
                          key={candidate.user_id}
                          className="grid gap-3 rounded-lg border bg-background p-3 md:grid-cols-[minmax(0,1fr)_180px_auto] md:items-end"
                        >
                          <div className="min-w-0">
                            <div className="font-medium">
                              {candidate.display_name}
                            </div>
                            <div
                              className="mt-1 truncate font-mono text-xs text-muted-foreground"
                              title={candidate.user_id}
                            >
                              {candidate.user_id}
                            </div>
                          </div>
                          <div className="grid gap-1.5">
                            <Label htmlFor={selectId}>授予角色</Label>
                            <select
                              id={selectId}
                              className="h-11 w-full cursor-pointer rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
                              value={selectedRole}
                              disabled={pendingUserId !== null}
                              onChange={(event) =>
                                setCandidateRoles((current) => ({
                                  ...current,
                                  [candidate.user_id]: event.target
                                    .value as ProjectMembershipRole,
                                }))
                              }
                            >
                              {ROLE_OPTIONS.map((option) => (
                                <option
                                  key={option.value}
                                  value={option.value}
                                  title={option.description}
                                >
                                  {option.label}
                                </option>
                              ))}
                            </select>
                          </div>
                          <Button
                            type="button"
                            className="min-h-11"
                            onClick={() => void grantMembership(candidate)}
                            disabled={pendingUserId !== null}
                          >
                            {pending ? (
                              <Loader2 className="animate-spin" />
                            ) : (
                              <UserPlus />
                            )}
                            添加成员
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="rounded-lg border border-dashed px-4 py-8 text-center">
                    <p className="font-medium">暂无可添加用户</p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      邀请、账号创建和团队调整仍由独立的组织管理流程负责。
                    </p>
                  </div>
                )}
                {candidateCursor && (
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-11 justify-self-start"
                    onClick={() => void loadMoreCandidates()}
                    disabled={loadingMore !== null || pendingUserId !== null}
                  >
                    {loadingMore === "candidates" && (
                      <Loader2 className="animate-spin" />
                    )}
                    加载更多候选
                  </Button>
                )}
              </CardContent>
            </Card>
          </>
        ) : null}
      </div>

      <Dialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open && pendingUserId === null) setRevokeTarget(null);
        }}
      >
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>撤销显式项目角色？</DialogTitle>
            <DialogDescription>
              {revokeTarget
                ? `将撤销 ${revokeTarget.display_name} 的${ROLE_LABELS[revokeTarget.role]}角色。若该用户同时是组织管理员或当前 Team Lead，继承访问仍由上级权限事实决定。`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose
              render={
                <Button
                  type="button"
                  variant="outline"
                  className="min-h-11"
                  disabled={pendingUserId !== null}
                />
              }
            >
              取消
            </DialogClose>
            <Button
              type="button"
              variant="destructive"
              className="min-h-11"
              onClick={() => void revokeMembership()}
              disabled={pendingUserId !== null}
            >
              {pendingUserId === revokeTarget?.user_id ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Trash2 />
              )}
              确认撤销
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
