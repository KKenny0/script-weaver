import type { Metadata } from "next";
import "./globals.css";
import "./styles/artifact.css";
import ThemeProvider from "./components/ThemeProvider";

export const metadata: Metadata = {
  title: "Script-Weaver — Agent-Native 剧本+分镜生成系统",
  description: "从故事想法到完整剧本和专业分镜脚本",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body style={{ height: "100%", margin: 0 }}>
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
