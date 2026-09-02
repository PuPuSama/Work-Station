"use client";

import { ArrowLeft, Building2, Layers3, Loader2, Settings2, Sparkles, UserRound } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useState } from "react";

import { AccountProfileButton } from "@/components/account-profile-button";
import { LogoutButton } from "@/components/logout-button";
import { ServerLlmSettingsPanel } from "@/components/server-llm-settings-panel";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const OrganizationAdminEntry = dynamic(
  () => import("@/components/organization-admin-entry").then(
    (module) => module.OrganizationAdminEntry,
  ),
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground" role="status">
        <Loader2 className="size-4 animate-spin" />正在加载组织管理…
      </div>
    ),
  },
);

export function GlobalSettingsWorkspace() {
  const [organizationOpen, setOrganizationOpen] = useState(false);

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-5 py-5">
          <div className="flex min-w-0 items-center gap-3">
            <Link
              href="/"
              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground"
              aria-label="返回项目目录"
            >
              <ArrowLeft className="size-5" />
            </Link>
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-primary">
                Article Agent · Global Settings
              </p>
              <h1 className="truncate text-xl font-semibold">全局设置</h1>
              <p className="text-sm text-muted-foreground">
                统一管理当前账号的模型、账户资料和组织协作入口。
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              nativeButton={false}
              render={<Link href="/batch-writing" />}
            >
              <Layers3 />批量写作
            </Button>
            <LogoutButton />
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-6xl gap-4 px-5 py-5">
        <div className="grid gap-4 lg:grid-cols-2">
          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-5 py-5">
              <CardTitle className="flex items-center gap-2">
                <Settings2 className="size-5 text-primary" />模型设置
              </CardTitle>
              <CardDescription>
                当前账号发起的标题、产品分析、大纲、正文、复检和交付请求会使用这里的配置。
              </CardDescription>
            </CardHeader>
            <CardContent className="px-5 py-5">
              <ServerLlmSettingsPanel />
            </CardContent>
          </Card>

          <Card className="gap-0 py-0">
            <CardHeader className="border-b px-5 py-5">
              <CardTitle className="flex items-center gap-2">
                <UserRound className="size-5 text-primary" />账户资料
              </CardTitle>
              <CardDescription>
                更新显示名；登录身份本身不会被修改。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex items-center justify-between gap-4 px-5 py-5">
              <p className="text-sm text-muted-foreground">
                显示名会用于组织成员列表、项目协作和审计记录。
              </p>
              <AccountProfileButton />
            </CardContent>
          </Card>
        </div>

        <Card className="gap-0 py-0">
          <CardHeader className="border-b px-5 py-5">
            <CardTitle className="flex items-center gap-2">
              <Building2 className="size-5 text-primary" />组织管理
            </CardTitle>
            <CardDescription>
              管理组织账号、团队、Team Lead、邀请和外部登录身份。只有具备相应组织权限的账号可以操作。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 px-5 py-5">
            {!organizationOpen ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-dashed bg-muted/20 px-4 py-4">
                <p className="text-sm text-muted-foreground">
                  按需加载组织目录，避免打开设置页时额外等待成员和团队数据。
                </p>
                <Button type="button" variant="outline" onClick={() => setOrganizationOpen(true)}>
                  <Sparkles />打开组织管理
                </Button>
              </div>
            ) : (
              <OrganizationAdminEntry embedded />
            )}
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
