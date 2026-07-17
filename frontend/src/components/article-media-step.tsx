"use client";

import { ImageIcon, Save, Upload } from "lucide-react";

import { ArticleImageSlots } from "@/components/article-image-slots";
import { WorkbenchField, WorkflowStep } from "@/components/article-workbench-ui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { ArticleImage, Product, TaskRecord } from "@/types";

type AnchorCandidate = NonNullable<ArticleImage["anchor_candidates"]>[number];

export function ArticleMediaStep({
  task,
  heroImage,
  products,
  heroUpload,
  heroPreviewUrl,
  heroPreviewFailed,
  heroDirty,
  busy,
  hasActiveJob,
  canAction,
  onHeroChange,
  onHeroUploadChange,
  onHeroPreviewError,
  onSelectBody,
  onMoveBody,
  onSaveHero,
  onUploadHero,
  onPrepareImages,
  onSaveAnchor,
}: {
  task: TaskRecord;
  heroImage: string;
  products: Product[];
  heroUpload: File | null;
  heroPreviewUrl: string;
  heroPreviewFailed: boolean;
  heroDirty: boolean;
  busy: boolean;
  hasActiveJob: boolean;
  canAction: (action: string) => boolean;
  onHeroChange: (path: string) => void;
  onHeroUploadChange: (file: File | null) => void;
  onHeroPreviewError: () => void;
  onSelectBody: (product: Product, slotIndex: number) => void;
  onMoveBody: (slotIndex: number, direction: -1 | 1) => void;
  onSaveHero: () => void;
  onUploadHero: () => void;
  onPrepareImages: () => void;
  onSaveAnchor: (image: ArticleImage, candidate: AnchorCandidate) => void;
}) {
  return (
    <ScrollArea className="h-[570px] pr-3">
      <div className="grid gap-4">
        <WorkflowStep
          number="5"
          title="选择并准备首图"
          description="每篇文章最多 3 张不同图片（包含首图）。首图会转换为 WebP，以安全化文章标题命名，并固定放在第一个 H2 前；重复产品图会自动跳过。"
          done={task.status === "images_ready" || task.status === "docx_exported"}
        >
          <ArticleImageSlots
            task={task}
            heroImage={heroImage}
            products={products}
            onSelectHero={onHeroChange}
            onSelectBody={onSelectBody}
            onMoveBody={onMoveBody}
          />
          <details className="rounded-lg border bg-muted/15 p-3">
            <summary className="cursor-pointer text-sm font-medium">
              高级 / 手动选择图片
            </summary>
            <div className="mt-3 grid gap-3">
              <WorkbenchField label="首图路径" value={heroImage} onChange={onHeroChange} />
              <div className="grid gap-2">
                <Label htmlFor="hero-upload">或上传首图</Label>
                <Input
                  id="hero-upload"
                  type="file"
                  accept="image/*"
                  onChange={(event) => onHeroUploadChange(event.target.files?.[0] ?? null)}
                />
              </div>
            </div>
          </details>
          {heroPreviewUrl && (
            <div className="grid gap-2 rounded-lg border bg-muted/20 p-3">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="font-medium">首图预览</span>
                {heroUpload && <Badge variant="outline">本地文件，尚未上传</Badge>}
              </div>
              {heroPreviewFailed ? (
                <div className="flex min-h-36 items-center justify-center rounded-md border border-dashed px-4 text-center text-sm text-muted-foreground">
                  图片无法预览，请检查路径、文件格式，或重新选择图片。
                </div>
              ) : (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={heroPreviewUrl}
                  alt="当前选择的首图预览"
                  className="max-h-72 w-full rounded-md border bg-white object-contain"
                  onError={onHeroPreviewError}
                />
              )}
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={onSaveHero}
              disabled={busy || !heroImage.trim() || !heroDirty || !canAction("update_images")}
            >
              <Save />
              保存首图
            </Button>
            <Button
              variant="outline"
              onClick={onUploadHero}
              disabled={busy || !heroUpload || !canAction("update_images")}
            >
              <Upload />
              上传并设为首图
            </Button>
          </div>
          <Button
            onClick={onPrepareImages}
            disabled={
              busy ||
              hasActiveJob ||
              (!heroUpload && !heroImage.trim()) ||
              !canAction("prepare_images")
            }
          >
            <ImageIcon />
            {heroDirty ? "保存首图并准备图片" : "转换 WebP 并准备图片"}
          </Button>
          <div className="grid gap-2">
            {(task.images ?? []).map((item) => (
              <div key={item.id} className="rounded-lg border p-3 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{item.role === "hero" ? "首图" : "正文图"}</span>
                  <Badge variant="outline">{item.marker}</Badge>
                </div>
                <div className="mt-1 break-all text-xs text-muted-foreground">
                  {item.prepared_path}
                </div>
                {item.status === "needs_anchor" && item.anchor_candidates?.length ? (
                  <div className="mt-3 grid gap-2">
                    <div className="text-xs text-amber-700">
                      请选择标题；图片会放在该标题下第一段完整正文的末尾：
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {item.anchor_candidates.map((candidate) => (
                        <Button
                          key={candidate.id}
                          size="sm"
                          variant="outline"
                          onClick={() => onSaveAnchor(item, candidate)}
                        >
                          H{candidate.level} {candidate.heading}
                        </Button>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </WorkflowStep>
      </div>
    </ScrollArea>
  );
}
