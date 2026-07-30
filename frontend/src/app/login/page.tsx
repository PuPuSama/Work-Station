"use client";

import {
  Eye,
  EyeOff,
  Loader2,
  LockKeyhole,
  PenLine,
  ShieldCheck,
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { apiFileUrl, apiGet, apiPost } from "@/lib/api";
import type { ApiMessage } from "@/types";

type AuthStatus = {
  message: string;
  data?: {
    enabled?: boolean;
    authenticated?: boolean;
    mode?: "server";
    login_available?: boolean;
  };
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "登录失败，请稍后重试。";
}

function safeDestination() {
  const candidate = new URLSearchParams(window.location.search).get("next");
  return candidate?.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : "/";
}

export default function LoginPage() {
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
      .catch((nextError) => {
        setError(errorMessage(nextError));
        setStatus(null);
      });
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!password || pending) return;
    setPending(true);
    setError("");
    try {
      await apiPost<ApiMessage>("/api/auth/login", { password });
      window.location.assign(safeDestination());
    } catch (nextError) {
      setError(errorMessage(nextError));
      setPending(false);
    }
  }

  const serverMode = status?.mode === "server";
  const statusLoading = status === undefined;
  const statusFailed = status === null;

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
            <CardDescription>
              {serverMode
                ? "使用组织身份提供方完成安全登录。"
                : statusFailed
                  ? "暂时无法确认当前登录方式。"
                : "输入管理员设置的访问密码。"}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-4">
              {!serverMode && !statusLoading && !statusFailed && (
                <form className="grid gap-4" onSubmit={submit}>
                  <div className="grid gap-2">
                    <Label htmlFor="application-password">访问密码</Label>
                    <div className="relative">
                      <Input
                        id="application-password"
                        type={showPassword ? "text" : "password"}
                        value={password}
                        autoComplete="current-password"
                        autoFocus
                        className="pr-10"
                        disabled={pending}
                        onChange={(event) => {
                          setPassword(event.target.value);
                          setError("");
                        }}
                      />
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        className="absolute right-1 top-1/2 -translate-y-1/2"
                        aria-label={showPassword ? "隐藏密码" : "显示密码"}
                        onClick={() =>
                          setShowPassword((current) => !current)
                        }
                      >
                        {showPassword ? <EyeOff /> : <Eye />}
                      </Button>
                    </div>
                  </div>

                  <Button
                    type="submit"
                    size="lg"
                    disabled={!password || pending}
                  >
                    {pending ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <LockKeyhole />
                    )}
                    {pending ? "正在验证" : "进入工作台"}
                  </Button>
                </form>
              )}

              {serverMode && status?.login_available && (
                <Button
                  type="button"
                  size="lg"
                  disabled={pending}
                  onClick={() => {
                    setPending(true);
                    setError("");
                    const next = encodeURIComponent(safeDestination());
                    window.location.assign(
                      apiFileUrl(`/api/auth/oidc/start?next=${next}`),
                    );
                  }}
                >
                  {pending ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <ShieldCheck />
                  )}
                  {pending ? "正在前往身份提供方" : "使用组织账号登录"}
                </Button>
              )}

              {serverMode && !status?.login_available && (
                <Alert>
                  <ShieldCheck />
                  <AlertTitle>身份登录尚未配置</AlertTitle>
                  <AlertDescription>
                    请联系管理员完成 OIDC 提供方配置后再试。
                  </AlertDescription>
                </Alert>
              )}

              {statusLoading && (
                <Button type="button" size="lg" disabled>
                  <Loader2 className="animate-spin" />
                  正在检查登录方式
                </Button>
              )}

              {error && (
                <Alert variant="destructive">
                  <LockKeyhole />
                  <AlertTitle>无法登录</AlertTitle>
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
