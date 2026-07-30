"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowRight,
  Building2,
  Loader2,
  PackageCheck,
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
import { apiGet } from "@/lib/api";
import type { AccessibleProject } from "@/types";

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

  const loadProjects = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setProjects(await apiGet<AccessibleProject[]>("/api/projects"));
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

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
              当前只开放已迁移到 PostgreSQL 与私有对象存储的交付控制台。
              未迁移的本地文章、批量任务和设置入口不会在 Server 模式显示。
            </p>
          </div>
          <div className="flex items-center gap-2">
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
              const href = `/projects/${encodeURIComponent(project.project_id)}/deliveries`;
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
                          aria-label={`打开 ${project.customer_name || project.project_id} 的交付记录`}
                        />
                      }
                    >
                      <PackageCheck />
                      交付记录
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
    </main>
  );
}
