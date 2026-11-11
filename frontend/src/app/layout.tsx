import type { Metadata } from "next";
import { IBM_Plex_Sans, IBM_Plex_Sans_Arabic, IBM_Plex_Sans_Devanagari, IBM_Plex_Mono } from "next/font/google";
import { SyntheticBanner } from "@/components/badges";
import "./globals.css";

// One family across Latin, Arabic and Devanagari so mixed-language residents render coherently.
const plex = IBM_Plex_Sans({ variable: "--font-plex", subsets: ["latin"], weight: ["400", "500", "600"] });
const plexArabic = IBM_Plex_Sans_Arabic({
  variable: "--font-plex-arabic",
  subsets: ["arabic"],
  weight: ["400", "500", "600"],
  preload: false,
});
const plexDeva = IBM_Plex_Sans_Devanagari({
  variable: "--font-plex-deva",
  subsets: ["devanagari"],
  weight: ["400", "500", "600"],
  preload: false,
});
const plexMono = IBM_Plex_Mono({ variable: "--font-plex-mono", subsets: ["latin"], weight: ["400", "500"] });

export const metadata: Metadata = {
  title: { default: "Anticipa Command Center", template: "%s - Anticipa" },
  description: "Population-level preventive-care coordination console. Synthetic data only.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${plex.variable} ${plexArabic.variable} ${plexDeva.variable} ${plexMono.variable}`}
    >
      <body className="min-h-dvh">
        <SyntheticBanner />
        {children}
      </body>
    </html>
  );
}
