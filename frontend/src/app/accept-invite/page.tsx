"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowLeft,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";

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
import { apiFileUrl, apiPost } from "@/lib/api";

type InvitationPrepareResponse = {
  start_path: string;
  expires_seconds: number;
};

function message(error: unknown) {
  return error instanceof Error && error.message
    ? error.message
    : "邀请暂时无法处理，请联系组织管理员重新签发。";
}

function fragmentToken() {
  const fragment = window.location.hash.slice(1);
  if (!fragment) return "";
  const params = new URLSearchParams(fragment);
  return params.get("token")?.trim() ?? "";
}

export default function AcceptInvitePage() {
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const prepare = useCallback(async (value: string) => {
    const normalized = value.trim();
    if (!normalized) return;
    setPending(true);
    setError("");
    try {
      const prepared = await apiPost<InvitationPrepareResponse>(
        "/api/auth/invitations/prepare",
        { invitation_token: normalized },
      );
      setToken("");
      window.location.assign(
        apiFileUrl(`${prepared.start_path}?next=${encodeURIComponent("/")}`),
      );
    } catch (nextError) {
      setError(message(nextError));
      setPending(false);
    }
  }, []);

  useEffect(() => {
    const supplied = fragmentToken();
    if (!supplied) return;
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}`,
    );
    setToken("");
    void prepare(supplied);
  }, [prepare]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pending) void prepare(token);
  }

  return (
    <main className="grid min-h-dvh place-items-center bg-muted/35 px-4 py-10">
      <div className="w-full max-w-lg">
        <div className="mb-5 flex items-center justify-center gap-3">
          <span className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <ShieldCheck className="size-5" />
          </span>
          <div>
            <div className="font-semibold tracking-tight">Article Agent</div>
            <div className="text-xs text-muted-foreground">组织邀请</div>
          </div>
        </div>

        <Card className="shadow-lg shadow-slate-950/5">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="size-4 text-primary" />
              接受工作区邀请
            </CardTitle>
            <CardDescription>
              Token 验证后会前往组织身份提供方。只有通过签名验证的 Issuer 与 Subject
              才能完成本地账号关联。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            {pending ? (
              <div
                className="grid min-h-40 place-items-center gap-3 text-center"
                role="status"
                aria-live="polite"
              >
                <Loader2 className="size-6 animate-spin text-primary" />
                <div>
                  <p className="font-medium">正在准备安全登录</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Token 已移出地址栏，正在绑定短期 OIDC State…
                  </p>
                </div>
              </div>
            ) : (
              <form className="grid gap-4" onSubmit={submit}>
                <div className="grid gap-2">
                  <Label htmlFor="invitation-token">一次性邀请 Token</Label>
                  <div className="relative">
                    <Input
                      id="invitation-token"
                      type={showToken ? "text" : "password"}
                      autoComplete="off"
                      autoFocus
                      className="min-h-11 pr-11 font-mono text-sm"
                      value={token}
                      aria-describedby="invitation-token-help"
                      onChange={(event) => {
                        setToken(event.target.value);
                        setError("");
                      }}
                    />
                    <Button
                      type="button"
                      size="icon-sm"
                      variant="ghost"
                      className="absolute right-1 top-1/2 min-h-9 min-w-9 -translate-y-1/2"
                      aria-label={showToken ? "隐藏 Token" : "显示 Token"}
                      onClick={() => setShowToken((current) => !current)}
                    >
                      {showToken ? <EyeOff /> : <Eye />}
                    </Button>
                  </div>
                  <p
                    id="invitation-token-help"
                    className="text-xs leading-5 text-muted-foreground"
                  >
                    Token 仅用于本次登录事务，不会进入外部身份提供方 URL，也不会以明文写入数据库。
                  </p>
                </div>

                <Button
                  type="submit"
                  size="lg"
                  className="min-h-11"
                  disabled={!token.trim()}
                >
                  <ShieldCheck />
                  使用组织账号验证并接受
                </Button>
              </form>
            )}

            {error && (
              <Alert variant="destructive" aria-live="assertive">
                <AlertCircle />
                <AlertTitle>无法接受邀请</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <div className="border-t pt-4">
              <Button
                nativeButton={false}
                variant="ghost"
                className="min-h-11"
                render={<Link href="/login" />}
              >
                <ArrowLeft />
                返回普通登录
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
