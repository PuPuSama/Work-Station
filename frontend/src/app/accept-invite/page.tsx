"use client";

import { ArrowLeft, KeyRound, PenLine } from "lucide-react";
import Link from "next/link";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function AcceptInvitePage() {
  return (
    <main className="grid min-h-dvh place-items-center bg-muted/35 px-4 py-10">
      <div className="w-full max-w-lg">
        <div className="mb-5 flex items-center justify-center gap-3">
          <span className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <PenLine className="size-5" />
          </span>
          <div>
            <div className="font-semibold tracking-tight">Article Agent</div>
            <div className="text-xs text-muted-foreground">组织账号</div>
          </div>
        </div>
        <Card className="shadow-lg shadow-slate-950/5">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="size-4 text-primary" />
              账号登录
            </CardTitle>
            <CardDescription>现在使用管理员提供的账号和密码登录，不需要 Google 或其他外部身份验证。</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <Alert>
              <KeyRound />
              <AlertTitle>邀请链接暂不可用</AlertTitle>
              <AlertDescription>请让组织管理员直接提供登录账号和密码。</AlertDescription>
            </Alert>
            <Button
              nativeButton={false}
              size="lg"
              className="min-h-11"
              render={<Link href="/login" />}
            >
              <ArrowLeft />
              返回登录
            </Button>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
