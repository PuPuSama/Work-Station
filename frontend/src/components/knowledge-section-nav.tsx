"use client";

import { Database, PackageSearch } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Button } from "@/components/ui/button";

export function KnowledgeSectionNav({ customer }: { customer: string }) {
  const pathname = usePathname().replace(/\/$/, "");
  const base = `/projects/${encodeURIComponent(customer)}/knowledge`;
  const products = `${base}/products`;

  return (
    <nav className="flex w-fit gap-1 rounded-xl border bg-muted/40 p-1" aria-label="知识库页面">
      <Button
        size="sm"
        variant={pathname === base ? "default" : "ghost"}
        nativeButton={false}
        render={<Link href={base} aria-current={pathname === base ? "page" : undefined} />}
      >
        <Database />
        知识来源
      </Button>
      <Button
        size="sm"
        variant={pathname.startsWith(products) ? "default" : "ghost"}
        nativeButton={false}
        render={<Link href={products} aria-current={pathname.startsWith(products) ? "page" : undefined} />}
      >
        <PackageSearch />
        产品库
      </Button>
    </nav>
  );
}
