"use client";

/* eslint-disable @next/next/no-img-element, react-hooks/set-state-in-effect */

import {
  AlertCircle,
  ArrowUpRight,
  FileText,
  ImageIcon,
  Loader2,
  PackageSearch,
  Pencil,
  RefreshCw,
  Save,
  Search,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { KnowledgeSectionNav } from "@/components/knowledge-section-nav";
import {
  editableSpecificationTables,
  ProductSpecificationEditor,
  type SpecificationTable,
} from "@/components/product-specification-editor";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiGet, apiPut } from "@/lib/api";
import type {
  KnowledgeLibrary,
  KnowledgeProductSummary,
  ProjectAssetDownload,
  ServerCatalogImageAsset,
  ServerCatalogProduct,
  ServerProjectCatalog,
} from "@/types";

type ProductView = KnowledgeProductSummary & {
  asset_count: number;
  selected_asset_id: string;
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "产品库加载失败，请重试。";
}

function ProductSpecifications({ product }: { product: ProductView }) {
  if (!product.specification_tables.length) {
    return (
      <div className="rounded-xl border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
        当前官网页面没有解析出结构化规格表。
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      {product.specification_tables.map((table, tableIndex) => (
        <div key={`${table.caption || "spec"}-${tableIndex}`} className="overflow-hidden rounded-xl border">
          {table.caption ? (
            <div className="border-b bg-muted/50 px-4 py-2 text-sm font-medium">
              {table.caption}
            </div>
          ) : null}
          <div className="max-h-[420px] overflow-auto">
            <Table>
              {table.headers?.length ? (
                <TableHeader>
                  <TableRow>
                    {table.headers.map((header, index) => (
                      <TableHead key={`${header}-${index}`}>{header || "—"}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
              ) : null}
              <TableBody>
                {(table.rows || []).map((row, rowIndex) => (
                  <TableRow key={rowIndex}>
                    {row.map((value, cellIndex) => (
                      <TableCell
                        key={cellIndex}
                        className={cellIndex === 0 ? "min-w-48 font-medium" : "min-w-40"}
                      >
                        {value || "—"}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      ))}
    </div>
  );
}

export function ProjectProductLibrary({ customer }: { customer: string }) {
  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [catalog, setCatalog] = useState<ServerProjectCatalog | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [images, setImages] = useState<ServerCatalogImageAsset[]>([]);
  const [imageUrls, setImageUrls] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [imageLoading, setImageLoading] = useState(false);
  const [editingSpecifications, setEditingSpecifications] = useState(false);
  const [savingSpecifications, setSavingSpecifications] = useState(false);
  const [specificationDraft, setSpecificationDraft] = useState<SpecificationTable[]>([]);
  const [saveMessage, setSaveMessage] = useState("");
  const [error, setError] = useState("");
  const knowledgeApi = `/api/knowledge/${encodeURIComponent(customer)}`;
  const projectApi = `/api/projects/${encodeURIComponent(customer)}`;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextLibrary, nextCatalog] = await Promise.all([
        apiGet<KnowledgeLibrary>(knowledgeApi),
        apiGet<ServerProjectCatalog>(`${projectApi}/catalog?product_limit=200`),
      ]);
      setLibrary(nextLibrary);
      setCatalog(nextCatalog);
      setSelectedId((current) =>
        nextCatalog.products.some((item) => item.product_id === current)
          ? current
          : nextCatalog.products[0]?.product_id || "",
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [knowledgeApi, projectApi]);

  useEffect(() => {
    void load();
  }, [load]);

  const products = useMemo<ProductView[]>(() => {
    const details = new Map(
      (library?.products || []).map((product) => [product.product_id, product]),
    );
    return (catalog?.products || []).flatMap((product: ServerCatalogProduct) => {
      const detail = details.get(product.product_id);
      return detail ? [{ ...detail, ...product }] : [];
    });
  }, [catalog, library]);

  const filteredProducts = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return products;
    return products.filter((product) =>
      [product.name, ...product.category_path]
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [products, query]);

  const selected = products.find((product) => product.product_id === selectedId) || null;

  const startEditingSpecifications = () => {
    if (!selected) return;
    setError("");
    setSaveMessage("");
    setSpecificationDraft(editableSpecificationTables(selected.specification_tables));
    setEditingSpecifications(true);
  };

  const cancelEditingSpecifications = () => {
    setEditingSpecifications(false);
    setSpecificationDraft([]);
  };

  const saveSpecifications = async () => {
    if (!selected) return;
    setSavingSpecifications(true);
    setError("");
    setSaveMessage("");
    try {
      const updated = await apiPut<KnowledgeProductSummary>(
        `${knowledgeApi}/products/${encodeURIComponent(selected.product_id)}/specifications`,
        { specification_tables: specificationDraft },
      );
      setLibrary((current) =>
        current
          ? {
              ...current,
              products: current.products.map((product) =>
                product.product_id === updated.product_id ? updated : product,
              ),
            }
          : current,
      );
      setEditingSpecifications(false);
      setSpecificationDraft([]);
      setSaveMessage("规格参数已保存，后续文章选品会使用这份人工修正结果。");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSavingSpecifications(false);
    }
  };

  useEffect(() => {
    setEditingSpecifications(false);
    setSpecificationDraft([]);
    setSaveMessage("");
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setImages([]);
      setImageUrls({});
      return;
    }
    let active = true;
    setImageLoading(true);
    setImages([]);
    setImageUrls({});
    void apiGet<ServerProjectCatalog>(
      `${projectApi}/catalog?product_limit=200&image_limit=100&image_product_ids=${encodeURIComponent(selectedId)}`,
    )
      .then(async (result) => {
        if (!active) return;
        setImages(result.image_assets);
        const downloads = await Promise.allSettled(
          result.image_assets.map(async (asset) => {
            const download = await apiGet<ProjectAssetDownload>(
              `${projectApi}/assets/${encodeURIComponent(asset.asset_id)}/download?expires_seconds=900`,
            );
            return [asset.asset_id, download.url] as const;
          }),
        );
        if (active) {
          setImageUrls(
            Object.fromEntries(
              downloads.flatMap((download) =>
                download.status === "fulfilled" ? [download.value] : [],
              ),
            ),
          );
        }
      })
      .catch((reason) => {
        if (active) setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setImageLoading(false);
      });
    return () => {
      active = false;
    };
  }, [projectApi, selectedId]);

  const relatedSources = selected
    ? (library?.sources || []).filter(
        (source) =>
          source.status === "published" &&
          (source.canonical_url === selected.canonical_url ||
            source.display_name.toLocaleLowerCase().includes(selected.name.toLocaleLowerCase())),
      )
    : [];

  return (
    <main className="mx-auto grid w-full max-w-[1500px] gap-5 px-5 py-6">
      <KnowledgeSectionNav customer={customer} />

      <section className="flex flex-col gap-4 rounded-2xl border bg-card p-5 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">
            <PackageSearch className="size-4" />
            Published product knowledge
          </div>
          <h1 className="text-2xl font-semibold">产品库</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
            官网扫描成功后直接发布。点击产品即可核对结构化信息、规格表、关联资料和当前版本图片。
          </p>
        </div>
        <Button variant="outline" className="min-h-10" disabled={loading} onClick={() => void load()}>
          {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          刷新产品库
        </Button>
      </section>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>产品库读取失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <section className="grid min-h-[680px] gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Card className="h-fit xl:sticky xl:top-5">
          <CardHeader className="gap-3">
            <div className="flex items-center justify-between gap-3">
              <CardTitle className="text-base">全部产品</CardTitle>
              <Badge variant="secondary">{products.length}</Badge>
            </div>
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索产品名称或分类"
                className="pl-9"
              />
            </div>
          </CardHeader>
          <CardContent className="grid max-h-[620px] gap-2 overflow-auto">
            {loading ? (
              <div className="flex min-h-40 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 size-4 animate-spin" />
                正在读取产品…
              </div>
            ) : filteredProducts.length ? (
              filteredProducts.map((product) => (
                <button
                  key={product.product_id}
                  type="button"
                  onClick={() => setSelectedId(product.product_id)}
                  className={`grid gap-2 rounded-xl border p-3 text-left transition-colors hover:bg-muted/50 ${
                    selectedId === product.product_id
                      ? "border-primary bg-primary/5"
                      : "bg-background"
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <span className="font-medium leading-5">{product.name}</span>
                    <Badge variant="outline" className="shrink-0">
                      {product.asset_count} 图
                    </Badge>
                  </div>
                  <span className="line-clamp-1 text-xs text-muted-foreground">
                    {product.category_path.join(" / ") || "未分类"}
                  </span>
                </button>
              ))
            ) : (
              <div className="rounded-xl border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">
                {products.length ? "没有匹配的产品。" : "扫描官网后，已发布产品会出现在这里。"}
              </div>
            )}
          </CardContent>
        </Card>

        {selected ? (
          <div className="grid content-start gap-5">
            <Card>
              <CardContent className="grid gap-5 py-5 lg:grid-cols-[minmax(0,1fr)_280px]">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge>已发布</Badge>
                    {selected.category_path.map((category) => (
                      <Badge key={category} variant="outline">{category}</Badge>
                    ))}
                  </div>
                  <h2 className="mt-4 text-2xl font-semibold tracking-tight">{selected.name}</h2>
                  <p className="mt-3 max-w-3xl text-sm leading-6 text-muted-foreground">
                    {selected.description || "官网未提供独立产品摘要，可继续查看下方结构化事实与规格。"}
                  </p>
                  <div className="mt-5 flex flex-wrap gap-2">
                    {selected.canonical_url ? (
                      <Button variant="outline" size="sm" nativeButton={false} render={<a href={selected.canonical_url} target="_blank" rel="noreferrer" />}>
                        打开官网原页 <ArrowUpRight />
                      </Button>
                    ) : null}
                    <span className="inline-flex items-center gap-2 rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
                      <ImageIcon className="size-4" /> {selected.asset_count} 张当前图片
                    </span>
                    <span className="inline-flex items-center gap-2 rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
                      <FileText className="size-4" /> {relatedSources.length} 份关联资料
                    </span>
                  </div>
                </div>
                <div className="aspect-[4/3] overflow-hidden rounded-xl border bg-muted">
                  {imageLoading ? (
                    <div className="grid size-full place-items-center text-muted-foreground"><Loader2 className="animate-spin" /></div>
                  ) : images[0] && imageUrls[images[0].asset_id] ? (
                    <img className="size-full object-contain" src={imageUrls[images[0].asset_id]} alt={images[0].label || selected.name} />
                  ) : (
                    <div className="grid size-full place-items-center text-sm text-muted-foreground">暂无产品图片</div>
                  )}
                </div>
              </CardContent>
            </Card>

            {images.length > 1 ? (
              <Card>
                <CardHeader><CardTitle className="text-base">相关图片</CardTitle></CardHeader>
                <CardContent className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
                  {images.map((asset) => (
                    <figure key={asset.asset_id} className="overflow-hidden rounded-xl border bg-muted/30">
                      <div className="aspect-[4/3] bg-muted">
                        {imageUrls[asset.asset_id] ? (
                          <img className="size-full object-contain" src={imageUrls[asset.asset_id]} alt={asset.label} loading="lazy" />
                        ) : null}
                      </div>
                      <figcaption className="line-clamp-2 px-3 py-2 text-xs text-muted-foreground">{asset.label}</figcaption>
                    </figure>
                  ))}
                </CardContent>
              </Card>
            ) : null}

            {selected.main_content_facts.length ? (
              <Card>
                <CardHeader><CardTitle className="text-base">产品事实</CardTitle></CardHeader>
                <CardContent className="grid gap-2 sm:grid-cols-2">
                  {selected.main_content_facts.map((fact, index) => (
                    <div key={`${fact}-${index}`} className="rounded-xl border bg-muted/20 px-4 py-3 text-sm leading-6">{fact}</div>
                  ))}
                </CardContent>
              </Card>
            ) : null}

            <Card>
              <CardHeader className="flex-row items-start justify-between gap-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <CardTitle className="text-base">规格参数</CardTitle>
                    {selected.specification_tables_overridden ? (
                      <Badge variant="secondary">已人工修正</Badge>
                    ) : null}
                  </div>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    原始网页表格证据会保留；保存后以人工修正结果用于产品展示和文章写作。
                  </p>
                </div>
                {editingSpecifications ? (
                  <div className="flex shrink-0 gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={savingSpecifications}
                      onClick={cancelEditingSpecifications}
                    >
                      <X /> 取消
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      disabled={savingSpecifications}
                      onClick={() => void saveSpecifications()}
                    >
                      {savingSpecifications ? <Loader2 className="animate-spin" /> : <Save />}
                      保存修改
                    </Button>
                  </div>
                ) : (
                  <Button type="button" size="sm" variant="outline" onClick={startEditingSpecifications}>
                    <Pencil /> 编辑参数
                  </Button>
                )}
              </CardHeader>
              <CardContent className="grid gap-3">
                {saveMessage ? (
                  <Alert>
                    <AlertTitle>保存成功</AlertTitle>
                    <AlertDescription>{saveMessage}</AlertDescription>
                  </Alert>
                ) : null}
                {editingSpecifications ? (
                  <ProductSpecificationEditor
                    tables={specificationDraft}
                    disabled={savingSpecifications}
                    onChange={setSpecificationDraft}
                  />
                ) : (
                  <ProductSpecifications product={selected} />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader><CardTitle className="text-base">关联资料</CardTitle></CardHeader>
              <CardContent className="grid gap-2">
                {relatedSources.length ? relatedSources.map((source) => (
                  <a
                    key={source.source_id}
                    href={source.canonical_url || undefined}
                    target={source.canonical_url ? "_blank" : undefined}
                    rel={source.canonical_url ? "noreferrer" : undefined}
                    className="flex items-center justify-between gap-3 rounded-xl border px-4 py-3 text-sm hover:bg-muted/50"
                  >
                    <span className="min-w-0 truncate font-medium">{source.display_name}</span>
                    <Badge variant="outline">当前已发布</Badge>
                  </a>
                )) : (
                  <p className="rounded-xl border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">当前产品没有单独关联的资料卡。</p>
                )}
              </CardContent>
            </Card>
          </div>
        ) : (
          <div className="grid min-h-80 place-items-center rounded-2xl border border-dashed text-sm text-muted-foreground">
            从左侧选择一个已发布产品查看详情。
          </div>
        )}
      </section>
    </main>
  );
}
