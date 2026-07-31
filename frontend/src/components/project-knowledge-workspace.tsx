"use client";

import { AlertCircle, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ProjectEvidenceWorkbench } from "@/components/project-evidence-workbench";
import { ProjectKnowledgeLibrary } from "@/components/project-knowledge-library";
import { ProjectResearchWorkspace } from "@/components/project-research-workspace";
import { ServerKnowledgeInbox } from "@/components/server-knowledge-inbox";
import { apiGet } from "@/lib/api";
import type { AuthStatus } from "@/types";

export function ProjectKnowledgeWorkspace({
  customer,
}: {
  customer: string;
}) {
  const [mode, setMode] = useState<"loading" | "local" | "server">("loading");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void apiGet<AuthStatus>("/api/auth/status")
      .then((status) => {
        if (!active) return;
        const isServer = status.data?.mode === "server";
        const isLocal =
          status.data?.mode === undefined &&
          typeof status.data?.enabled === "boolean";
        if (!isServer && !isLocal) {
          throw new Error("无法识别当前工作区模式。");
        }
        setMode(isServer ? "server" : "local");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(
          reason instanceof Error
            ? reason.message
            : "无法确认当前工作区模式。",
        );
      });
    return () => {
      active = false;
    };
  }, []);

  if (mode === "server") {
    return <ServerKnowledgeInbox customer={customer} />;
  }
  if (mode === "local") {
    return (
      <>
        <ProjectKnowledgeLibrary customer={customer} />
        <ProjectResearchWorkspace customer={customer} />
        <ProjectEvidenceWorkbench customer={customer} />
      </>
    );
  }
  if (error) {
    return (
      <main className="mx-auto max-w-3xl px-5 py-8">
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>无法打开知识工作区</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </main>
    );
  }
  return (
    <main className="flex min-h-[50dvh] items-center justify-center px-5">
      <div
        className="flex items-center gap-3 rounded-xl border bg-card px-5 py-4 text-sm text-muted-foreground"
        role="status"
        aria-live="polite"
      >
        <Loader2 className="size-4 animate-spin" />
        正在确认 Local / Server 知识工作区…
      </div>
    </main>
  );
}
