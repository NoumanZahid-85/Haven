import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import { MotionProvider } from "./components/motion-provider";
import { AuthProvider } from "@/components/auth";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Haven — a voice companion for the hard moments",
  description:
    "Someone to talk to at 2:37 in the morning. A real-time voice AI companion for mental wellbeing and personal growth, with safety guardrails and memory.",
  openGraph: {
    title: "Haven",
    description: "Real-time voice AI companion for mental wellbeing",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        suppressHydrationWarning
        className={`${inter.variable} ${jetbrains.variable}`}
      >
        <div className="stage" aria-hidden="true" />
        <MotionProvider>
          <AuthProvider>{children}</AuthProvider>
        </MotionProvider>
      </body>
    </html>
  );
}
