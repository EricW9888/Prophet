import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import JobCompletionNoticeHost from "@/components/JobCompletionNoticeHost";
import MobileRefreshControl from "@/components/MobileRefreshControl";
import PwaRegistration from "@/components/PwaRegistration";
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
  applicationName: "Prophet",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    title: "Prophet",
    statusBarStyle: "black-translucent",
  },
  formatDetection: {
    telephone: false,
  },
};

export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f8fafc" },
    { media: "(prefers-color-scheme: dark)", color: "#09090b" },
  ],
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
      <body className="flex min-h-full min-w-0 flex-col bg-background font-sans text-foreground">
        <PwaRegistration />
        <MobileRefreshControl />
        <JobCompletionNoticeHost />
        {children}
      </body>
    </html>
  );
}
