"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, Loader2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ProjectSettings } from "@/components/project-settings";
import { ServerProjectMembers } from "@/components/server-project-members";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

type ProjectSettingsEntryProps = {
  customer: string;
};

type SettingsMode = "loading" | "local" | "server" | "error";

export function ProjectSettingsEntry({
  customer,
}: ProjectSettingsEntryProps) {
  const [mode, setMode] = useState<SettingsMode>("loading");
  const [error, setError] = useState("");

  const resolveMode = useCallback(async () => {
    setMode("loading");
    setError("");
    try {
      const status = await apiGet<AuthStatus>("/api/auth/status");
      if (status.data?.mode === "server") {
        setMode("server");
      } else if (typeof status.data?.enabled === "boolean") {
        setMode("local");
      } else {
        throw new Error("服务器返回了无法识别的运行模式。");
      }
    } catch (nextError) {
      setError(
        nextError instanceof Error
          ? nextError.message
          : "无法确认当前运行模式。",
      );
      setMode("error");
    }
  }, []);

  useEffect(() => {
    void resolveMode();
  }, [resolveMode]);

  if (mode === "server") {
    return <ServerProjectMembers projectId={customer} />;
  }
  if (mode === "local") {
    return <ProjectSettings customer={customer} />;
  }
  if (mode === "error") {
    return (
      <main className="min-h-dvh bg-background px-5 py-5 text-foreground">
        <Alert variant="destructive" className="mx-auto max-w-5xl">
          <AlertCircle />
          <AlertTitle>无法打开项目设置</AlertTitle>
          <AlertDescription>
            {error || "无法确认当前运行模式，请重试。"}
          </AlertDescription>
          <div className="col-start-2 mt-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              onClick={() => void resolveMode()}
            >
              <RefreshCw />
              重试
            </Button>
          </div>
        </Alert>
      </main>
    );
  }
  return (
    <main
      className="flex min-h-56 items-center justify-center gap-2 bg-background text-sm text-muted-foreground"
      role="status"
      aria-live="polite"
    >
      <Loader2 className="size-4 animate-spin" />
      正在确认项目运行模式…
    </main>
  );
}
