import Research from "@/components/Research";

export default async function Page({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  return <Research symbol={decodeURIComponent(symbol).toUpperCase()} />;
}
