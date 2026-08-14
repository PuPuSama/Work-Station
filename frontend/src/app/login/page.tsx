"use client";

import { Loader2, LockKeyhole, PenLine, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { apiFileUrl, apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

function safeDestination() {
  const candidate = new URLSearchParams(window.location.search).get("next");
  return candidate?.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : "/";
}

export default function LoginPage() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<AuthStatus["data"] | null>();

  useEffect(() => {
    apiGet<AuthStatus>("/api/auth/status")
      .then((result) => {
        if (result.data?.authenticated) {
          window.location.replace(safeDestination());
          return;
        }
        setStatus(result.data ?? {});
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "无法检查登录状态。");
        setStatus(null);
      });
  }, []);

  return (
    <main className="grid min-h-dvh place-items-center bg-muted/35 px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-5 flex items-center justify-center gap-3">
          <span className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <PenLine className="size-5" />
          </span>
          <div>
            <div className="font-semibold tracking-tight">Article Agent</div>
            <div className="text-xs text-muted-foreground">SEO 内容运营台</div>
          </div>
        </div>
        <Card className="shadow-lg shadow-slate-950/5">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2">
              <LockKeyhole className="size-4 text-primary" />
              登录工作台
            </CardTitle>
            <CardDescription>使用组织身份提供方安全登录。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {status === undefined && (
              <Button type="button" size="lg" disabled>
                <Loader2 className="animate-spin" />正在检查登录状态
              </Button>
            )}
            {status?.login_available && (
              <Button
                type="button"
                size="lg"
                disabled={pending}
                onClick={() => {
                  setPending(true);
                  const next = encodeURIComponent(safeDestination());
                  window.location.assign(apiFileUrl(`/api/auth/oidc/start?next=${next}`));
                }}
              >
                {pending ? <Loader2 className="animate-spin" /> : <ShieldCheck />}
                {pending ? "正在前往身份提供方" : "使用组织账号登录"}
              </Button>
            )}
            {status && !status.login_available && (
              <Alert>
                <ShieldCheck />
                <AlertTitle>身份登录尚未配置</AlertTitle>
                <AlertDescription>请联系管理员完成 OIDC 配置。</AlertDescription>
              </Alert>
            )}
            {error && (
              <Alert variant="destructive">
                <LockKeyhole />
                <AlertTitle>无法登录</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
