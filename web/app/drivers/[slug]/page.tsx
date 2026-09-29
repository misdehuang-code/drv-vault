import type { Metadata } from "next";

import { DriverDetailClient } from "@/components/DriverDetailClient";

type DetailProps = {
  params: Promise<{ slug: string }>;
};

/**
 * Static export emits one SPA shell (`_`).
 * Cloudflare Pages public/_redirects rewrites /drivers/:slug → /drivers/_ .
 * The client reads the real slug from window.location.
 */
export function generateStaticParams() {
  return [{ slug: "_" }];
}

export const dynamic = "force-static";
export const dynamicParams = false;

export async function generateMetadata({
  params,
}: DetailProps): Promise<Metadata> {
  const { slug } = await params;
  if (!slug || slug === "_") {
    return { title: "Package · DrvVault" };
  }
  return {
    title: `${slug} · DrvVault`,
    description: "Legacy Driver Archive — verified direct download.",
  };
}

export default function DriverDetailPage() {
  return <DriverDetailClient />;
}
