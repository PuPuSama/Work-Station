"use client";

import { Globe2, Loader2, ScanSearch } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

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

type ServerProductRediscoveryPanelProps = {
  customer: string;
  pending: string;
  knowledgeEditAllowed: boolean;
  runJob: (
    label: string,
    endpoint: string,
    payload?: Record<string, unknown>,
  ) => Promise<void>;
};

export function ServerProductRediscoveryPanel({
  customer,
  pending,
  knowledgeEditAllowed,
  runJob,
}: ServerProductRediscoveryPanelProps) {
  const [categoryUrl, setCategoryUrl] = useState("");
  const [maxProducts, setMaxProducts] = useState("12");
  const label = "重新发现产品";
  const parsedLimit = Number(maxProducts);
  const limitValid =
    Number.isInteger(parsedLimit) && parsedLimit >= 1 && parsedLimit <= 50;

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>官网产品重新发现</CardTitle>
        <CardDescription>
          从项目官方域名下的分类页启动有界抓取；结果只进入知识库 Inbox，不会自动替换当前
          Task 产品。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <Alert>
          <Globe2 />
          <AlertTitle>候选与正式产品分离</AlertTitle>
          <AlertDescription>
            Worker 只保存可审阅证据。必须在知识库完成确认和发布后，产品才会出现在上方正式选择区。
          </AlertDescription>
        </Alert>
        <div className="grid gap-2">
          <Label htmlFor="server-product-category-url">官方分类页 URL</Label>
          <Input
            id="server-product-category-url"
            type="url"
            inputMode="url"
            value={categoryUrl}
            disabled={Boolean(pending) || !knowledgeEditAllowed}
            placeholder="https://www.example.com/products/category/"
            onChange={(event) => setCategoryUrl(event.target.value)}
          />
          <p className="text-xs leading-5 text-muted-foreground">
            服务端会重新校验 URL 属于当前 Project 的 Active Official Domain；浏览器不能指定
            Bucket、抓取器或对象路径。
          </p>
        </div>
        <div className="grid gap-2">
          <Label htmlFor="server-product-limit">最多抓取产品数（1–50）</Label>
          <Input
            id="server-product-limit"
            type="number"
            min="1"
            max="50"
            value={maxProducts}
            disabled={Boolean(pending) || !knowledgeEditAllowed}
            onChange={(event) => setMaxProducts(event.target.value)}
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            className="min-h-11"
            disabled={
              Boolean(pending) ||
              !knowledgeEditAllowed ||
              !categoryUrl.trim() ||
              !limitValid
            }
            onClick={() =>
              void runJob(label, "product-rediscovery", {
                category_url: categoryUrl.trim(),
                max_products: parsedLimit,
              })
            }
          >
            {pending === label ? (
              <Loader2 className="animate-spin" />
            ) : (
              <ScanSearch />
            )}
            启动重新发现
          </Button>
          <Button
            nativeButton={false}
            variant="outline"
            className="min-h-11"
            render={
              <Link
                href={`/projects/${encodeURIComponent(customer)}/knowledge`}
              />
            }
          >
            打开知识库审阅
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
