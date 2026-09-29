import type { Metadata } from "next";
import Link from "next/link";
import { HardDrive } from "lucide-react";

import { VAULT_ANNOUNCEMENT } from "@/lib/copy";

import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL || "https://drvvault.example",
  ),
  title: {
    default: "DrvVault — Enterprise Recovery Vault for Legacy Hardware",
    template: "%s · DrvVault",
  },
  description:
    "Enterprise recovery vault for scarce industrial and legacy hardware drivers. 100% verified downloads, no registration, zero storage, USDT-TRC20 instant release.",
  openGraph: {
    siteName: "DrvVault",
    type: "website",
    locale: "en_US",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <head>
        <link rel="preconnect" href="https://fonts.bunny.net" />
        <link
          href="https://fonts.bunny.net/css?family=syne:600,700,800|ibm-plex-sans:400,500,600|ibm-plex-mono:400,500"
          rel="stylesheet"
        />
      </head>
      <body>
        <div className="border-b border-signal/20 bg-ink-900/90">
          <p className="site-shell !py-2.5 text-center text-[0.78rem] leading-relaxed text-mist-200 sm:text-[0.85rem]">
            {VAULT_ANNOUNCEMENT}
          </p>
        </div>
        <div className="site-shell !pt-5">
          <header className="mb-8 flex items-center justify-between gap-4 animate-rise">
            <Link href="/" className="group flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-signal/30 bg-signal/10 text-signal shadow-glow transition group-hover:bg-signal/20">
                <HardDrive className="h-5 w-5" aria-hidden />
              </span>
              <span>
                <span className="block font-display text-xl font-bold tracking-tight text-mist-100">
                  Drv<span className="text-signal">Vault</span>
                </span>
                <span className="block text-[0.72rem] tracking-wide text-mist-400">
                  Enterprise Recovery Vault
                </span>
              </span>
            </Link>
            <nav className="hidden items-center gap-5 text-sm text-mist-300 sm:flex">
              <Link href="/#catalog" className="hover:text-signal">
                Catalog
              </Link>
              <span className="text-xs text-mist-400">4.90 USDT · TRC20</span>
            </nav>
          </header>
          {children}
          <footer className="mt-16 border-t border-mist-400/15 pt-6 text-xs leading-relaxed text-mist-400 animate-rise">
            DrvVault · Enterprise Recovery Vault — verified download links only;
            packages are not hosted on our servers. Pay via USDT-TRC20 for
            instant release.
          </footer>
        </div>
      </body>
    </html>
  );
}
