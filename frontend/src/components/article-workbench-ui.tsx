"use client";

import { Clipboard, ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import type { TaskRecord } from "@/types";

export function TaskBrief({ task }: { task: TaskRecord }) {
  return (
    <div className="grid gap-2 rounded-lg border bg-muted/30 p-3 text-sm">
      <div>
        <span className="font-medium">话题：</span>
        <span>{task.topic}</span>
      </div>
      <div>
        <span className="font-medium">竞品：</span>
        <span className="text-muted-foreground">{task.competitor_keyword || "空"}</span>
      </div>
      {task.competitor_blog && (
        <a
          href={task.competitor_blog}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-1 break-all text-muted-foreground hover:text-primary hover:underline"
        >
          <ExternalLink className="size-3.5 shrink-0" />
          {task.competitor_blog}
        </a>
      )}
    </div>
  );
}

export function WorkbenchField({
  label,
  value,
  onChange,
  inputType = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  inputType?: "text" | "number";
}) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <Input
        type={inputType}
        min={inputType === "number" ? 0 : undefined}
        max={inputType === "number" ? 100 : undefined}
        step={inputType === "number" ? "0.1" : undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

export function WorkflowStep({
  number,
  title,
  description,
  done,
  children,
}: {
  number: string;
  title: string;
  description: string;
  done: boolean;
  children: ReactNode;
}) {
  return (
    <div className="grid gap-3 rounded-lg border p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold">
            {number}
          </div>
          <div className="min-w-0">
            <div className="font-medium">{title}</div>
            <div className="mt-1 text-sm text-muted-foreground">{description}</div>
          </div>
        </div>
        <Badge variant={done ? "default" : "outline"}>{done ? "已完成" : "待处理"}</Badge>
      </div>
      <div className="grid gap-3 pl-0 md:pl-10">{children}</div>
    </div>
  );
}

export function AiScreenshotInput({
  label,
  path,
  disabled,
  onImage,
}: {
  label: string;
  path: string;
  disabled: boolean;
  onImage: (file: File) => void;
}) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <div
        tabIndex={disabled ? -1 : 0}
        onPaste={(event) => {
          if (disabled) return;
          const item = Array.from(event.clipboardData.items).find((candidate) =>
            candidate.type.startsWith("image/"),
          );
          const blob = item?.getAsFile();
          if (!blob) return;
          event.preventDefault();
          onImage(
            new File([blob], `${label}-${Date.now()}.png`, {
              type: blob.type || "image/png",
            }),
          );
        }}
        className={cn(
          "rounded-lg border border-dashed bg-muted/20 p-3 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/20",
          disabled && "cursor-not-allowed opacity-60",
        )}
      >
        <div className="flex items-center gap-2 text-muted-foreground">
          <Clipboard className="size-4" />
          点击此区域后按 Ctrl+V 粘贴截图，或在下方选择图片文件。
        </div>
      </div>
      <Input
        type="file"
        accept="image/*"
        disabled={disabled}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onImage(file);
          event.currentTarget.value = "";
        }}
      />
      {path && <div className="break-all text-xs text-muted-foreground">{path}</div>}
    </div>
  );
}

export function EditorPanel({
  value,
  onChange,
  placeholder,
  height,
  meta,
  actions,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  height: string;
  meta?: ReactNode;
  actions: ReactNode;
}) {
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">{actions}</div>
      {meta}
      <Textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        wrap="soft"
        className={cn(
          "resize-none overflow-y-auto break-words font-mono text-sm leading-6",
          height,
        )}
      />
    </div>
  );
}

export function FileRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 rounded-lg border p-3">
      <span className="font-medium">{label}</span>
      <span className="break-all text-muted-foreground">{value}</span>
    </div>
  );
}
