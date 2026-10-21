import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Sans_Condensed } from "next/font/google";
import { Nav } from "@/components/Nav";
import "./globals.css";

const sans = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "600"], variable: "--font-plex-sans" });
const condensed = IBM_Plex_Sans_Condensed({ subsets: ["latin"], weight: ["600", "700"], variable: "--font-plex-condensed" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400"], variable: "--font-plex-mono" });

export const metadata: Metadata = {
  title: "RepoPilot",
  description: "Review what the agent changed, the evidence it gathered, and decide whether it becomes a pull request.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${condensed.variable} ${mono.variable}`}>
      <body>
        <div className="shell">
          <header className="masthead">
            <a className="wordmark" href="/">RepoPilot</a>
            <Nav />
          </header>
          <main className="page">{children}</main>
        </div>
      </body>
    </html>
  );
}
