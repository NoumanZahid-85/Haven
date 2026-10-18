import type { Metadata } from "next";
import { Atkinson_Hyperlegible, Newsreader } from "next/font/google";
import { AuthProvider } from "@/components/auth";
import "./globals.css";

// Newsreader: a humane serif for the voice of the product (headlines, the
// lamp's status line). Atkinson Hyperlegible: designed for low-vision
// readers — for someone tired or upset, every other word should be effortless.
const newsreader = Newsreader({
  subsets: ["latin"],
  variable: "--font-newsreader",
  display: "swap",
  weight: ["300", "400"],
});

const atkinson = Atkinson_Hyperlegible({
  subsets: ["latin"],
  variable: "--font-atkinson",
  display: "swap",
  weight: ["400", "700"],
});

export const metadata: Metadata = {
  title: "Haven — someone to talk to, right now",
  description:
    "A real-time voice companion for hard moments: sleepless nights, anxiety before work, a breakup. Talk out loud, interrupt anytime, and pick up where you left off.",
  openGraph: {
    title: "Haven",
    description: "A real-time voice companion for mental wellbeing.",
    type: "website",
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    // Font variables live on <html> because globals.css reads them on :root.
    <html lang="en" suppressHydrationWarning className={`${newsreader.variable} ${atkinson.variable}`}>
      <body suppressHydrationWarning>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
