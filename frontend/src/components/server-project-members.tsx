"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, CheckCircle2, Loader2, RefreshCw, Save, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, apiGet, apiPut } from "@/lib/api";
import type { ProjectMembershipCandidate, ProjectMembershipCandidatePage, ServerProjectMetadata } from "@/types";

type ServerProjectMembersProps = {
  projectId: string;
  beforeMemberCards?: ReactNode;
  pageKind?: "members" | "settings";
};

type Feedback = { kind: "success" | "error"; message: string } | null;

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function ServerProjectMembers({
  projectId,
  beforeMemberCards,
}: ServerProjectMembersProps) {
  const encodedProject = encodeURIComponent(projectId);
  const [metadata, setMetadata] = useState<ServerProjectMetadata | null>(null);
  const [candidates, setCandidates] = useState<ProjectMembershipCandidate[]>([]);
  const [selectedOwner, setSelectedOwner] = useState("");
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFeedback(null);
    try {
      const nextMetadata = await apiGet<ServerProjectMetadata>(
        `/api/projects/${encodedProject}/metadata`,
      );
      setMetadata(nextMetadata);
      setSelectedOwner(nextMetadata.owner_user_id ?? "");
      try {
        const candidatePage = await apiGet<ProjectMembershipCandidatePage>(
          `/api/projects/${encodedProject}/members/candidates?limit=100`,
        );
        setCandidates(candidatePage.items);
        setCanManage(true);
      } catch (candidateError) {
        if (candidateError instanceof ApiError && candidateError.status === 403) {
          setCandidates([]);
          setCanManage(false);
        } else {
          throw candidateError;
        }
      }
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error, "项目负责人信息加载失败。") });
    } finally {
      setLoading(false);
    }
  }, [encodedProject]);

  useEffect(() => {
    void load();
  }, [load]);

  async function saveOwner() {
    if (!metadata || !canManage) return;
    setSaving(true);
    setFeedback(null);
    try {
      const next = await apiPut<{ owner_user_id: string | null }>(
        `/api/projects/${encodedProject}/owner`,
        { owner_user_id: selectedOwner || null },
      );
      setMetadata((current) => current ? { ...current, owner_user_id: next.owner_user_id } : current);
      setFeedback({ kind: "success", message: next.owner_user_id ? "项目负责人已更新。" : "项目已回到待分配状态。" });
    } catch (error) {
      setFeedback({ kind: "error", message: errorMessage(error, "项目负责人更新失败。") });
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="min-h-dvh bg-background text-foreground">
      <div className="border-b bg-card">
        <div className="mx-auto grid max-w-5xl gap-3 px-5 py-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground"><ShieldCheck className="size-4" /></span>
                <h1 className="text-xl font-semibold">项目负责人</h1>
              </div>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">每个项目只有一名负责人。负责人可以编辑和运行自己的项目；Team Lead 负责分配和删除团队项目，但不会自动获得编辑权限。</p>
            </div>
            <Button type="button" variant="outline" className="min-h-11" disabled={loading || saving} onClick={() => void load()}>{loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}刷新</Button>
          </div>
        </div>
      </div>
      <div className="mx-auto grid max-w-5xl gap-4 px-5 py-5">
        {beforeMemberCards}
        {feedback && <Alert variant={feedback.kind === "error" ? "destructive" : "default"}><>{feedback.kind === "error" ? <AlertCircle /> : <CheckCircle2 />}</><AlertTitle>{feedback.kind === "error" ? "操作失败" : "操作完成"}</AlertTitle><AlertDescription>{feedback.message}</AlertDescription></Alert>}
        {loading && !metadata ? (
          <Card><CardHeader><Skeleton className="h-5 w-44" /><Skeleton className="h-4 w-full max-w-xl" /></CardHeader><CardContent><Skeleton className="h-11 w-full" /></CardContent></Card>
        ) : metadata ? (
          <Card>
            <CardHeader className="border-b">
              <div className="flex flex-wrap items-center justify-between gap-2"><CardTitle>负责人分配</CardTitle><Badge variant={metadata.owner_user_id ? "outline" : "secondary"}>{metadata.owner_user_id ? "已分配" : "待分配"}</Badge></div>
              <CardDescription>项目所属团队：{metadata.owning_team_id ?? "未知"}。普通成员不能访问其他成员负责的项目。</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 pt-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
              {!canManage && <p className="text-sm text-muted-foreground sm:col-span-2">当前账号只能查看负责人；负责人分配由 Team Lead 或组织管理员完成。</p>}
              <div className="grid gap-2">
                <label htmlFor="project-owner" className="text-sm font-medium">项目负责人</label>
                <select id="project-owner" className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm" value={selectedOwner} disabled={saving} onChange={(event) => setSelectedOwner(event.target.value)}>
                  <option value="">待分配</option>
                  {candidates.map((candidate) => <option key={candidate.user_id} value={candidate.user_id}>{candidate.display_name} · {candidate.user_id}</option>)}
                  {metadata.owner_user_id && !candidates.some((candidate) => candidate.user_id === metadata.owner_user_id) && <option value={metadata.owner_user_id}>{metadata.owner_user_id}（当前负责人）</option>}
                </select>
                <p className="text-xs text-muted-foreground">只能选择所属团队中的 Active 成员；负责人可以负责多个项目。</p>
              </div>
              <Button type="button" className="min-h-11" disabled={!canManage || saving || selectedOwner === (metadata.owner_user_id ?? "")} onClick={() => void saveOwner()}>{saving ? <Loader2 className="animate-spin" /> : <Save />}保存分配</Button>
            </CardContent>
          </Card>
        ) : null}
      </div>
    </main>
  );
}
