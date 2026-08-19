import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "织梦台｜AI 长篇小说生成器",
  description: "AI 主写、作者把控、作品状态持续记忆。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

