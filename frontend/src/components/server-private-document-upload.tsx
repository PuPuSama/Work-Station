"use client";

import { AlertCircle, FileText, Loader2, Upload } from "lucide-react";
import { FormEvent, useRef, useState } from "react";

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
import { apiUpload } from "@/lib/api";
import type { KnowledgeUploadResult } from "@/types";

type TrustTier =
  | "hard_fact"
  | "reference_material"
  | "writing_instruction";

type ServerPrivateDocumentUploadProps = {
  editable: boolean;
  projectPath: string;
  onUploaded: (result: KnowledgeUploadResult) => Promise<void>;
};

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "私有资料上传失败，请重试。";
}

function fileSizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ServerPrivateDocumentUpload({
  editable,
  projectPath,
  onUploaded,
}: ServerPrivateDocumentUploadProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [trustTier, setTrustTier] =
    useState<TrustTier>("reference_material");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  function selectFile(file: File | null) {
    setError("");
    if (file && file.size > MAX_UPLOAD_BYTES) {
      setSelectedFile(null);
      setError("单文件不能超过 25 MB。");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    setSelectedFile(file);
    if (file && !displayName.trim()) {
      setDisplayName(file.name.replace(/\.[^.]+$/, ""));
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile) {
      setError("请先选择 DOCX、PDF、XLSX 或 XLSM 文件。");
      return;
    }
    setUploading(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", selectedFile);
      body.append(
        "display_name",
        displayName.trim() || selectedFile.name,
      );
      body.append("trust_tier", trustTier);
      const result = await apiUpload<KnowledgeUploadResult>(
        `${projectPath}/sources/upload`,
        body,
      );
      await onUploaded(result);
      setSelectedFile(null);
      setDisplayName("");
      setTrustTier("reference_material");
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setUploading(false);
    }
  }

  const locked = !editable || uploading;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Upload className="size-4" />
          上传私有资料
        </CardTitle>
        <CardDescription className="leading-5">
          原始文件、标准化内容与内嵌图片会保存为项目私有对象；解析结果只进入
          Research Inbox，不会自动发布或参与检索。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid gap-4" onSubmit={submit}>
          <div className="grid gap-2">
            <Label htmlFor="server-knowledge-file">资料文件</Label>
            <Input
              ref={fileInputRef}
              id="server-knowledge-file"
              type="file"
              accept=".docx,.pdf,.xlsx,.xlsm"
              aria-describedby="server-knowledge-file-help"
              disabled={locked}
              onChange={(event) =>
                selectFile(event.target.files?.[0] ?? null)
              }
            />
            <p
              id="server-knowledge-file-help"
              className="text-xs leading-5 text-muted-foreground"
            >
              支持 DOCX、PDF、XLSX、XLSM，单文件最大 25 MB。
            </p>
          </div>

          {selectedFile ? (
            <div className="flex items-center gap-3 rounded-xl border bg-muted/20 px-3 py-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-background text-muted-foreground">
                <FileText className="size-4" />
              </span>
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">
                  {selectedFile.name}
                </div>
                <div className="text-xs text-muted-foreground">
                  {fileSizeLabel(selectedFile.size)}
                </div>
              </div>
            </div>
          ) : null}

          <div className="grid gap-2">
            <Label htmlFor="server-knowledge-display-name">显示名称</Label>
            <Input
              id="server-knowledge-display-name"
              value={displayName}
              maxLength={255}
              disabled={locked}
              placeholder="例如：2026 产品规格表"
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="server-knowledge-trust-tier">
              建议信任层级
            </Label>
            <select
              id="server-knowledge-trust-tier"
              value={trustTier}
              disabled={locked}
              className="min-h-11 rounded-lg border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
              onChange={(event) =>
                setTrustTier(event.target.value as TrustTier)
              }
            >
              <option value="reference_material">参考资料</option>
              <option value="hard_fact">硬事实</option>
              <option value="writing_instruction">写作指令</option>
            </select>
            <p className="text-xs leading-5 text-muted-foreground">
              这是运营建议；仍需人工审阅并显式发布当前 Snapshot。
            </p>
          </div>

          {error ? (
            <Alert variant="destructive" aria-live="polite">
              <AlertCircle />
              <AlertTitle>资料未上传</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}

          <Button
            type="submit"
            className="min-h-11"
            disabled={locked || !selectedFile}
          >
            {uploading ? (
              <Loader2 className="animate-spin" />
            ) : (
              <Upload />
            )}
            {uploading ? "解析并安全入库中…" : "解析并加入 Inbox"}
          </Button>

          {!editable ? (
            <p className="text-xs leading-5 text-muted-foreground">
              当前角色可以查看 Inbox，但没有 knowledge.edit
              权限，不能上传私有资料。
            </p>
          ) : null}
        </form>
      </CardContent>
    </Card>
  );
}
