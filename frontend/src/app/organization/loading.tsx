import { Skeleton } from "@/components/ui/skeleton";

export default function OrganizationLoading() {
  return (
    <main className="mx-auto grid min-h-dvh max-w-6xl gap-4 px-5 py-6">
      <Skeleton className="h-12 w-72 max-w-full" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-64 w-full" />
    </main>
  );
}
