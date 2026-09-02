"use client";

/* eslint-disable react-hooks/set-state-in-effect */

import { AlertCircle, Loader2, Save, Settings2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { apiGet, apiPut } from "@/lib/api";
import type { ServerLlmSettings } from "@/types";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "模型设置加载失败。";
}

export function ServerLlmSettingsPanel() {
  const [settings, setSettings] = useState<ServerLlmSettings | null>(null);
  const [model, setModel] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await apiGet<ServerLlmSettings>("/api/settings/llm");
      setSettings(next);
      setModel(next.model);
      setReasoningEffort(next.reasoning_effort);
    } catch (nextError) {
      setSettings(null);
      setError(errorMessage(nextError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    if (!settings || !model || !reasoningEffort) return;
    setPending(true);
    setError("");
    try {
      const next = await apiPut<ServerLlmSettings>("/api/settings/llm", {
        model,
        reasoning_effort: reasoningEffort,
        revision: settings.revision,
      });
      setSettings(next);
      setModel(next.model);
      setReasoningEffort(next.reasoning_effort);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="grid gap-5">
      {error && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>模型设置不可用</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {loading ? (
        <div className="flex items-center gap-2 py-5 text-sm text-muted-foreground" role="status" aria-live="polite">
          <Loader2 className="size-4 animate-spin" />正在读取当前账号的模型设置…
        </div>
      ) : (
        <>
          <div className="grid gap-2">
            <Label htmlFor="global-settings-llm-model">模型</Label>
            <select
              id="global-settings-llm-model"
              className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm"
              value={model}
              disabled={!settings?.can_edit || pending}
              onChange={(event) => setModel(event.target.value)}
            >
              {(settings?.available_models || []).map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="global-settings-reasoning-effort">模型推理程度</Label>
            <select
              id="global-settings-reasoning-effort"
              className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm"
              value={reasoningEffort}
              disabled={!settings?.can_edit || pending}
              onChange={(event) => setReasoningEffort(event.target.value)}
            >
              {(settings?.available_reasoning_efforts || []).map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
            <p className="text-xs leading-5 text-muted-foreground">
              程度越高通常越适合复杂任务，但响应时间和用量也可能增加。标题生成仍保持系统设定的低推理档位。
            </p>
          </div>
          {!settings?.can_edit && (
            <Alert>
              <AlertTitle>仅供查看</AlertTitle>
              <AlertDescription>
                当前账号没有有效的组织成员身份，因此不能修改个人模型设置。
              </AlertDescription>
            </Alert>
          )}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-4">
            <p className="text-xs text-muted-foreground">
              当前配置只影响当前账号发起的文章工作流。
            </p>
            <Button
              type="button"
              onClick={() => void save()}
              disabled={!settings?.can_edit || !model || !reasoningEffort || pending}
            >
              {pending ? <Loader2 className="animate-spin" /> : <Save />}
              保存模型设置
            </Button>
          </div>
        </>
      )}
      {!loading && !settings && (
        <Button type="button" variant="outline" onClick={() => void load()}>
          <Settings2 />重试读取
        </Button>
      )}
    </div>
  );
}
