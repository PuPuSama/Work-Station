"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowRight,
  Building2,
  FileText,
  Loader2,
  Plus,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { LogoutButton } from "@/components/logout-button";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiGet, apiPost } from "@/lib/api";
import type {
  AccessibleProject,
  AuthStatus,
  WorkspaceTeam,
  WorkspaceTeamPage,
} from "@/types";

const ROLE_LABELS: Record<AccessibleProject["effective_role"], string> = {
  org_admin: "组织管理员",
  team_lead: "团队负责人",
  editor: "编辑",
  reviewer: "复核员",
  viewer: "只读成员",
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "项目目录加载失败。";
}

export function ServerProjectSelector() {
  const [projects, setProjects] = useState<AccessibleProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [createPending, setCreatePending] = useState(false);
  const [createError, setCreateError] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [officialDomain, setOfficialDomain] = useState("");
  const [owningTeamId, setOwningTeamId] = useState("");
  const [teams, setTeams] = useState<WorkspaceTeam[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [canOpenOrganization, setCanOpenOrganization] = useState(false);

  const loadProjects = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setProjects(await apiGet<AccessibleProject[]>("/api/projects"));
      try {
        const status = await apiGet<AuthStatus>("/api/auth/status");
        const currentOrganizationId = status.data?.organization_id || "";
        if (!currentOrganizationId) {
          setCanOpenOrganization(false);
        } else {
          await apiGet<WorkspaceTeamPage>(
            `/api/organizations/${encodeURIComponent(currentOrganizationId)}/teams?limit=1`,
          );
          setCanOpenOrganization(true);
        }
      } catch {
        setCanOpenOrganization(false);
      }
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);
  const canManageOrganization = projects.some(
    (project) => project.effective_role === "org_admin",
  ) || canOpenOrganization;

  async function openCreateProject() {
    setCreateOpen(true);
    setCreateError("");
    setOwningTeamId("");
    try {
      const status = await apiGet<AuthStatus>("/api/auth/status");
      const nextOrganizationId = status.data?.organization_id || "";
      if (!nextOrganizationId) {
        throw new Error("当前组织身份不可用，请重新登录。");
      }
      setOrganizationId(nextOrganizationId);
      try {
        const page = await apiGet<WorkspaceTeamPage>(
          `/api/organizations/${encodeURIComponent(nextOrganizationId)}/teams?limit=100`,
        );
        setTeams(page.items.filter((team) => team.status === "active"));
      } catch {
        // Members and Team Leads do not need the organization-wide team
        // directory; the server derives their single team from membership.
        setTeams([]);
      }
    } catch (nextError) {
      setCreateError(errorMessage(nextError));
    }
  }

  async function createProject() {
    setCreatePending(true);
    setCreateError("");
    try {
      const created = await apiPost<AccessibleProject>("/api/projects", {
        customer_name: customerName,
        official_domain: officialDomain,
        owning_team_id: owningTeamId || null,
      });
      setCreateOpen(false);
      window.location.assign(
        `/projects/${encodeURIComponent(created.project_id)}/articles`,
      );
    } catch (nextError) {
      setCreateError(errorMessage(nextError));
    } finally {
      setCreatePending(false);
    }
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-[1180px] flex-col gap-4 px-5 py-6 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.14em] text-primary">
              <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <ShieldCheck className="size-3.5" />
              </span>
              Article Agent · Server Workspace
            </div>
            <h1 className="text-xl font-semibold">可访问项目</h1>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
              当前开放已迁移到 PostgreSQL 的文章主链与私有对象交付控制台。
              未迁移的本地批量任务和本地设置入口不会在 Server 模式显示。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <>
                <Button
                  type="button"
                  onClick={() => void openCreateProject()}
                >
                  <Plus />
                  新建项目
                </Button>
                {canManageOrganization && <Button
                  nativeButton={false}
                  variant="outline"
                  render={<Link href="/organization" />}
                >
                  <Building2 />
                  组织管理
                </Button>}
            </>
            <Button
              type="button"
              variant="outline"
              onClick={() => void loadProjects()}
              disabled={loading}
            >
              {loading ? (
                <Loader2 className="animate-spin" />
              ) : (
                <RefreshCw />
              )}
              刷新
            </Button>
            <LogoutButton />
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1180px] gap-4 px-5 py-6">
        {error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>项目目录加载失败</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {loading && !projects.length ? (
          <div
            className="flex min-h-56 items-center justify-center gap-2 rounded-xl border bg-card text-sm text-muted-foreground"
            role="status"
            aria-live="polite"
          >
            <Loader2 className="size-4 animate-spin" />
            正在读取当前账号可访问的项目…
          </div>
        ) : projects.length ? (
          <section
            aria-label="可访问项目"
            className="grid gap-3 md:grid-cols-2"
          >
            {projects.map((project) => {
              const href = `/projects/${encodeURIComponent(project.project_id)}/articles`;
              return (
                <Card key={project.project_id} className="gap-0 py-0">
                  <CardHeader className="border-b px-4 py-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <CardTitle className="truncate">
                          {project.customer_name || project.project_id}
                        </CardTitle>
                        <CardDescription className="mt-1 truncate">
                          {project.official_domain || project.project_id}
                        </CardDescription>
                      </div>
                      <Badge variant="outline">
                        {ROLE_LABELS[project.effective_role]}
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="flex items-center justify-between gap-3 px-4 py-4">
                    <div className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
                      <Building2 className="size-4 shrink-0" />
                      <span className="truncate font-mono text-xs">
                        {project.project_id}
                      </span>
                    </div>
                    <Button
                      nativeButton={false}
                      render={
                        <Link
                          href={href}
                          aria-label={`打开 ${project.customer_name || project.project_id} 的文章任务`}
                        />
                      }
                    >
                      <FileText />
                      文章任务
                      <ArrowRight />
                    </Button>
                  </CardContent>
                </Card>
              );
            })}
          </section>
        ) : (
          !error && (
            <div className="flex min-h-56 flex-col items-center justify-center gap-2 rounded-xl border bg-card text-center">
              <Building2 className="size-6 text-muted-foreground" />
              <p className="font-medium">当前账号没有可访问项目</p>
              <p className="text-sm text-muted-foreground">
                请联系组织管理员检查项目成员关系与账号状态。
              </p>
            </div>
          )
        )}
      </div>
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建项目</DialogTitle>
            <DialogDescription>
              项目 ID 使用官网域名。创建后会建立空的文章工作区和知识库，不会自动导入旧数据。
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4">
            {createError && (
              <Alert variant="destructive">
                <AlertCircle />
                <AlertTitle>无法创建项目</AlertTitle>
                <AlertDescription>{createError}</AlertDescription>
              </Alert>
            )}
            <div className="grid gap-2">
              <Label htmlFor="project-customer-name">客户名称</Label>
              <Input
                id="project-customer-name"
                value={customerName}
                maxLength={120}
                placeholder="例如 Acme Fasteners"
                disabled={createPending}
                onChange={(event) => setCustomerName(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="project-official-domain">官网域名</Label>
              <Input
                id="project-official-domain"
                value={officialDomain}
                maxLength={253}
                placeholder="www.example.com（不要填写 https:// 或路径）"
                disabled={createPending}
                onChange={(event) => setOfficialDomain(event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                域名会作为稳定的 Project ID，创建后不可直接改名。
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="project-owning-team">所属团队（可选）</Label>
              <select
                id="project-owning-team"
                value={owningTeamId}
                disabled={createPending || !organizationId}
                className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm"
                onChange={(event) => setOwningTeamId(event.target.value)}
              >
                <option value="">不指定团队（仅组织管理员默认可见）</option>
                {teams.map((team) => (
                  <option key={team.team_id} value={team.team_id}>
                    {team.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <DialogFooter>
            <DialogClose
              render={
                <Button type="button" variant="outline" disabled={createPending} />
              }
            >
              取消
            </DialogClose>
            <Button
              type="button"
              disabled={
                createPending ||
                !customerName.trim() ||
                !officialDomain.trim() ||
                (teams.length > 0 && !owningTeamId.trim()) ||
                Boolean(createError && !organizationId)
              }
              onClick={() => void createProject()}
            >
              {createPending ? <Loader2 className="animate-spin" /> : <Plus />}
              创建并进入项目
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
