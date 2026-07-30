"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, Loader2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ProjectSelector } from "@/components/project-selector";
import { ServerProjectSelector } from "@/components/server-project-selector";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "无法确认当前运行模式。";
}

export function ProjectDirectory() {
  const [mode, setMode] = useState<"loading" | "local" | "server">("loading");
  const [error, setError] = useState("");

  const loadMode = useCallback(async () => {
    setMode("loading");
    setError("");
    try {
      const status = await apiGet<AuthStatus>("/api/auth/status");
      setMode(status.data?.mode === "server" ? "server" : "local");
    } catch (nextError) {
      setError(errorMessage(nextError));
    }
  }, []);

  useEffect(() => {
    void loadMode();
  }, [loadMode]);

  if (mode === "server") {
    return <ServerProjectSelector />;
  }
  if (mode === "local") {
    return <ProjectSelector />;
  }
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-5 text-foreground">
      {error ? (
        <Alert variant="destructive" className="max-w-lg">
          <AlertCircle />
          <AlertTitle>无法打开项目目录</AlertTitle>
          <AlertDescription className="grid gap-3">
            <span>{error}</span>
            <Button
              type="button"
              variant="outline"
              className="w-fit"
              onClick={() => void loadMode()}
            >
              <RefreshCw />
              重试
            </Button>
          </AlertDescription>
        </Alert>
      ) : (
        <div
          className="flex items-center gap-2 text-sm text-muted-foreground"
          role="status"
          aria-live="polite"
        >
          <Loader2 className="size-4 animate-spin" />
          正在确认工作区模式…
        </div>
      )}
    </main>
  );
}
