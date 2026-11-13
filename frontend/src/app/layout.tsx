import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans_Arabic, IBM_Plex_Sans_Devanagari, Inter, Source_Serif_4 } from "next/font/google";
import "./globals.css";

// Inter for Latin; IBM Plex Sans Arabic / Devanagari as fallbacks so mixed-language residents render coherently.
const inter = Inter({ variable: "--font-inter", subsets: ["latin"], weight: ["400", "500", "600", "700"] });
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
// Serif display face for headlines and large figures (owner references).
const displaySerif = Source_Serif_4({ variable: "--font-display-serif", subsets: ["latin"], weight: ["600", "700"] });

export const metadata: Metadata = {
  title: { default: "Anticipa Command Center", template: "%s | Anticipa" },
  description: "Preventive care coordination for whole populations.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" suppressHydrationWarning className={`${inter.variable} ${plexArabic.variable} ${plexDeva.variable} ${plexMono.variable} ${displaySerif.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: "try{var t=localStorage.getItem('anticipa-theme');if(t==='dark'||t==='clinical')document.documentElement.dataset.theme=t}catch(e){}" }} />
      </head>
      <body className="min-h-dvh">{children}</body>
    </html>
  );
}
