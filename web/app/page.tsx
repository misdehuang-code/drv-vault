import type { Metadata } from "next";

import { HomeCatalog } from "@/components/HomeCatalog";

/** Catalog is loaded client-side from /data/* chunks — page shell is static. */
export const dynamic = "force-static";

export const metadata: Metadata = {
  title: "DrvVault — Enterprise Recovery Vault",
  description:
    "Enterprise recovery vault for industrial and legacy hardware. Legacy Driver Archive with 100% verified instant downloads. USDT-TRC20 release.",
  alternates: { canonical: "/" },
};

export default function HomePage() {
  return <HomeCatalog />;
}
