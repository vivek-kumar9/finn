"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

export default function Search() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ symbol: string; name: string }[]>([]);
  const router = useRouter();

  useEffect(() => {
    if (q.trim().length < 2) return setHits([]);
    const ctl = new AbortController();
    const t = setTimeout(() => {
      fetch(`${API}/search?q=${encodeURIComponent(q)}`, { signal: ctl.signal })
        .then((r) => (r.ok ? r.json() : []))
        .then(setHits)
        .catch(() => {});
    }, 250);
    return () => {
      clearTimeout(t);
      ctl.abort();
    };
  }, [q]);

  return (
    <form
      className="search"
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        const pick = hits[0]?.symbol ?? q.trim().toUpperCase();
        if (pick) router.push(`/r/${pick}`);
      }}
    >
      <label htmlFor="q" className="sr">Research any listed company</label>
      <input id="q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Research any company, e.g. Kaynes"
             autoComplete="off" enterKeyHint="search" />
      {hits.length > 0 && (
        <ul className="hits">
          {hits.map((h) => (
            <li key={h.symbol}><button type="button" onClick={() => router.push(`/r/${h.symbol}`)}>
              <span className="sym">{h.symbol}</span> {h.name}</button></li>
          ))}
        </ul>
      )}
    </form>
  );
}
