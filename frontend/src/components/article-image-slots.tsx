"use client";

import { ExternalLink } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { apiFileUrl } from "@/lib/api";
import type { Product, TaskRecord } from "@/types";

function normalizeImagePath(value: string) {
  return value.trim().replaceAll("\\", "/").toLowerCase();
}

export function ArticleImageSlots({
  task,
  heroImage,
  products,
  onSelectHero,
  onSelectBody,
  onMoveBody,
}: {
  task: TaskRecord;
  heroImage: string;
  products: Product[];
  onSelectHero: (path: string) => void;
  onSelectBody: (product: Product, slotIndex: number) => void;
  onMoveBody: (slotIndex: number, direction: -1 | 1) => void;
}) {
  const assets = products
    .map((product, index) => ({ product, index }))
    .filter(({ product }) => product.image_path?.trim());
  const pathCounts = new Map<string, number>();
  for (const { product } of assets) {
    const key = normalizeImagePath(product.image_path);
    pathCounts.set(key, (pathCounts.get(key) || 0) + 1);
  }

  const prepared = (task.images || []).slice(0, 3);
  const preparedHero = prepared.find((image) => image.role === "hero");
  const preparedBody = prepared.filter((image) => image.role !== "hero");
  const usedPaths = new Set<string>();
  const slots: Array<{
    role: "hero" | "body";
    path: string;
    marker: string;
    productName: string;
    position: string;
  }> = [];

  const addSlot = (
    role: "hero" | "body",
    path: string,
    marker = "准备后生成",
    productName = "",
    position = "",
  ) => {
    const key = normalizeImagePath(path);
    if (!path.trim() || usedPaths.has(key) || slots.length >= 3) return;
    usedPaths.add(key);
    slots.push({ role, path, marker, productName, position });
  };

  if (preparedHero) {
    addSlot(
      "hero",
      preparedHero.prepared_path || preparedHero.source_path,
      preparedHero.marker,
      preparedHero.product_name || "",
      "固定放在第一个 H2 前",
    );
  } else {
    addSlot("hero", heroImage, "准备后按文章标题命名", "", "固定放在第一个 H2 前");
  }
  for (const image of preparedBody) {
    const context = image.anchor_text || image.anchor_after || "";
    addSlot(
      "body",
      image.prepared_path || image.source_path,
      image.marker,
      image.product_name || "",
      image.anchor_heading
        ? `${image.anchor_heading}${context ? ` · ${context.slice(0, 90)}` : ""}`
        : "尚未确定插入段落",
    );
  }
  if (preparedBody.length === 0) {
    for (const { product } of assets) {
      addSlot(
        "body",
        product.image_path,
        "准备后生成",
        product.name,
        "准备图片后显示目标标题和段落摘要",
      );
    }
  }
  while (slots.length < 3) {
    slots.push({
      role: slots.length === 0 ? "hero" : "body",
      path: "",
      marker: "空",
      productName: "",
      position: "尚未选择图片",
    });
  }

  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      <div className="grid content-start gap-2 rounded-lg border bg-muted/15 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="font-medium">官网资产库</div>
          <Badge variant="outline">{assets.length} 张候选</Badge>
        </div>
        <div className="grid max-h-[420px] gap-2 overflow-y-auto pr-1 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
          {assets.map(({ product, index }) => {
            const duplicate = (pathCounts.get(normalizeImagePath(product.image_path)) || 0) > 1;
            const preview = apiFileUrl(
              `/api/tasks/${task.id}/images/preview?path=${encodeURIComponent(product.image_path)}`,
            );
            return (
              <div key={`${product.image_path}-${index}`} className="overflow-hidden rounded-lg border bg-background">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={preview} alt={product.name || `产品图片 ${index + 1}`} className="h-32 w-full bg-white object-contain" />
                <div className="grid gap-1.5 p-2.5 text-xs">
                  <div className="line-clamp-2 font-medium">{product.name || `产品 ${index + 1}`}</div>
                  <div className="flex flex-wrap gap-1">
                    {product.detail_page_verified && <Badge variant="outline">详情页已核验</Badge>}
                    {duplicate && <Badge variant="destructive">重复候选</Badge>}
                  </div>
                  <div className="flex flex-wrap gap-1">
                    <Button type="button" size="xs" variant="outline" onClick={() => onSelectHero(product.image_path)}>
                      设为首图
                    </Button>
                    <Button type="button" size="xs" variant="outline" onClick={() => onSelectBody(product, 0)}>
                      正文槽位 2
                    </Button>
                    <Button type="button" size="xs" variant="outline" onClick={() => onSelectBody(product, 1)}>
                      正文槽位 3
                    </Button>
                    {product.url && (
                      <Button type="button" size="xs" variant="ghost" nativeButton={false} render={<a href={product.url} target="_blank" rel="noreferrer" />}>
                        <ExternalLink />产品页
                      </Button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          {!assets.length && (
            <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
              当前产品还没有可预览的官网图片资产，请先回到“产品”步骤抓取或核验资产。
            </div>
          )}
        </div>
      </div>

      <div className="grid content-start gap-2 rounded-lg border bg-muted/15 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="font-medium">本文图片槽位</div>
          <Badge variant="outline">最多 3 张，自动去重</Badge>
        </div>
        <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3">
          {slots.map((slot, index) => {
            const preview = slot.path
              ? apiFileUrl(`/api/tasks/${task.id}/images/preview?path=${encodeURIComponent(slot.path)}`)
              : "";
            return (
              <div key={`${index}-${slot.path}`} className="overflow-hidden rounded-lg border bg-background">
                {preview ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={preview} alt={`图片槽位 ${index + 1}`} className="h-32 w-full bg-white object-contain" />
                ) : (
                  <div className="flex h-32 items-center justify-center border-b bg-muted/30 text-sm text-muted-foreground">空槽位</div>
                )}
                <div className="grid gap-1.5 p-2.5 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{index === 0 ? "槽位 1 · 首图" : `槽位 ${index + 1} · 正文图`}</span>
                    <Badge variant="outline">{slot.marker}</Badge>
                  </div>
                  {slot.productName && <div className="line-clamp-1 text-muted-foreground">{slot.productName}</div>}
                  <div className="line-clamp-3 text-muted-foreground">插入位置：{slot.position}</div>
                  {index > 0 && index - 1 < preparedBody.length && (
                    <div className="flex flex-wrap gap-1 pt-1">
                      <Button type="button" size="xs" variant="ghost" disabled={index === 1} onClick={() => onMoveBody(index - 1, -1)}>
                        前移
                      </Button>
                      <Button type="button" size="xs" variant="ghost" disabled={index - 1 >= preparedBody.length - 1} onClick={() => onMoveBody(index - 1, 1)}>
                        后移
                      </Button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
