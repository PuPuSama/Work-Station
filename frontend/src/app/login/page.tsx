"use client";

import { Loader2, LockKeyhole, LogIn, PenLine } from "lucide-react";
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
import { apiGet, apiPost } from "@/lib/api";
import type { AuthStatus } from "@/types";

function safeDestination() {
  const candidate = new URLSearchParams(window.location.search).get("next");
  return candidate?.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : "/";
}

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
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

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setError("");
    try {
      await apiPost("/api/auth/login", { username, password });
      window.location.replace(safeDestination());
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "登录失败，请检查账号和密码。");
      setPending(false);
    }
  }

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
            <CardDescription>使用管理员提供的账号和密码登录。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {status === undefined && (
              <Button type="button" size="lg" disabled>
                <Loader2 className="animate-spin" />正在检查登录状态
              </Button>
            )}
            {status?.password_login_available && (
              <form className="grid gap-4" onSubmit={submit}>
                <div className="grid gap-2">
                  <Label htmlFor="login-username">账号</Label>
                  <Input
                    id="login-username"
                    name="username"
                    autoComplete="username"
                    autoFocus
                    className="min-h-11"
                    value={username}
                    onChange={(event) => {
                      setUsername(event.target.value);
                      setError("");
                    }}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="login-password">密码</Label>
                  <Input
                    id="login-password"
                    name="password"
                    type="password"
                    autoComplete="current-password"
                    className="min-h-11"
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value);
                      setError("");
                    }}
                  />
                </div>
                <Button
                  type="submit"
                  size="lg"
                  className="min-h-11"
                  disabled={pending || !username.trim() || !password}
                >
                  {pending ? <Loader2 className="animate-spin" /> : <LogIn />}
                  {pending ? "正在登录" : "登录"}
                </Button>
              </form>
            )}
            {status && !status.password_login_available && (
              <Alert>
                <LockKeyhole />
                <AlertTitle>账号登录尚未配置</AlertTitle>
                <AlertDescription>请联系管理员配置登录账号和密码。</AlertDescription>
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
