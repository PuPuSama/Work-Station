import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Article Workflow Agent",
  description: "Local workflow for topic-based article generation",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
