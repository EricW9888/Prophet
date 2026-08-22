import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import JobCompletionNoticeHost from "@/components/JobCompletionNoticeHost";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Prophet",
  description: "Time-aware portfolio intelligence and research operating system",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-background font-sans text-foreground">
        <JobCompletionNoticeHost />
        {children}
      </body>
    </html>
  );
}
