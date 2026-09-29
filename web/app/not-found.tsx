import Link from "next/link";

export default function NotFound() {
  return (
    <main className="panel py-16 text-center">
      <h1 className="font-display text-2xl font-bold text-mist-100">
        Package not found
      </h1>
      <p className="mt-2 text-mist-400">
        This SKU is missing or the verified link expired from the index.
      </p>
      <Link href="/" className="btn-primary mt-6 inline-flex">
        Back to catalog
      </Link>
    </main>
  );
}
